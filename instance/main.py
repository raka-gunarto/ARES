"""ARES daemon entrypoint (M10): wires the event bus, CLI and scheduler
sources, console channel, push notification channel, memory storage, real
TaskStore, the real Agent (spec §4.10), voice pipeline (spec §7.4), SIP
service/source/channels (spec §7.5), time tools (spec §8), sandboxed shell
tool (spec §15), and privilege request store/tools/source (spec §16) together.

This is the ONLY file in the repo that performs instance wiring (spec §8).
Per M5 scope, CLI source, scheduler source, console channel, push channel
(when enabled), FilesystemMemory, and real TaskStore are wired. Memory tools
and task tools (update_task, get_task_history) are registered as discoverable
tools so search_tools can locate them (§4.10).

The push notification channel (NtfyChannel) is registered when the push_ntfy
plugin is enabled in config (§7.6).

Per M6, when `home_assistant` is enabled in config, HAService is placed in
`services["home_assistant"]`, HOME_TOOLS are registered, and a
HomeAssistantSource is added to the supervised sources list (its `start()`
idles — the live WS transport is a recorded Blocker). When `safety_critical`
is enabled, FireHandler and IntruderHandler are registered on the critical
handler registry so fire/intrusion events bypass the LLM entirely (§4.9).
With both disabled (the shipped default), the daemon behaves exactly as M5.

Per M7, when `voice` is enabled in config, one VoiceSource per configured
room is added to supervised sources, and a VoiceTTSChannel is registered on
the router. Voice wiring is fully guarded by the enabled flag; with the default
config (voice disabled), no voice classes are instantiated and no voice extra
dependencies are required.

Per M8, when `sip` is enabled in config, SIPService is created (raises
RuntimeError if pjsua2 is missing — correct fail-fast), placed in
`services["sip"]`, COMMS_TOOLS are registered, SIPSource is added to
supervised sources, and SIPMessageChannel/SIPCallChannel are registered on
the router. SIP wiring is fully guarded by the enabled flag; with the default
config (sip disabled), no SIP classes are instantiated and pjsua2 is never
required.

Per M9, when `time_tools` is enabled in config, time tools (get_weather,
get_calendar, add_calendar_event) are registered on the tool registry. Time
tools are enabled by default; this wiring is guarded by the enabled flag
and only constructs tools without network access at startup.

Per M10, when `shell` is enabled in config, the sandboxed RunShell tool is
registered via build_shell_tools(). When `privileges` is enabled, PrivStore
is initialized and placed in services["privileges"], privilege tools
(request_privilege, get_privilege_requests) are registered, and PrivilegeSource
is added to supervised sources. Both plugings are wired only when enabled;
they are fully optional and disabled by default. Spec §14 security boundaries
are enforced: PrivStore.approve()/deny() are NEVER called from main.py
(dashboard-only operations).

The dashboard and other plugins do not exist yet and are wired in later
milestones — their config sections are ignored without error.

The Agent's §4.10 signature hard-requires a TaskStore and a BaseMemory.
Both are now real (M4): TaskStore backed by SQLite, FilesystemMemory on disk.
"""
from __future__ import annotations

import asyncio
import signal
import sys
from pathlib import Path

from ares.core.agent import Agent
from ares.core.config import enforce_prod_tripwires, load_config
from ares.core.trace import Tracer
from ares.core.critical import CriticalHandlerRegistry
from ares.core.dispatcher import Dispatcher
from ares.core.event import Event, EventBus, Priority
from ares.core.llm.client import LLMClient
from ares.core.memory.filesystem import FilesystemMemory
from ares.core.router import ResponseRouter
from ares.core.secrets import EnvSecretStore
from ares.core.session import SessionManager
from ares.core.source import BaseSource
from ares.core.tasks.store import TaskStore
from ares.core.tool import ToolRegistry
from ares.core.utils.ids import new_id
from ares.core.utils.logging import get_logger, setup_logging
from ares.plugins.channels.console import ConsoleChannel
from ares.plugins.channels.push_ntfy import NtfyChannel
from ares.plugins.channels.sip_call import SIPCallChannel
from ares.plugins.channels.sip_message import SIPMessageChannel
from ares.plugins.channels.voice_tts import VoiceTTSChannel
from ares.plugins.critical.safety import FireHandler, IntruderHandler
from ares.plugins.dashboard.channel import WebChannel
from ares.plugins.dashboard.server import DashboardSource
from ares.plugins.sip.client import SIPService
from ares.plugins.sip.source import SIPSource
from ares.plugins.sources.cli import CLISource
from ares.plugins.sources.home_assistant import HAService, HomeAssistantSource
from ares.plugins.sources.scheduler import SchedulerSource
from ares.plugins.sources.voice.intent import IntentFilter
from ares.plugins.sources.voice.stt import WhisperSTT
from ares.plugins.sources.voice.source import VoiceSource
from ares.plugins.sources.voice.vad import SileroVAD
from ares.plugins.privileges.source import PrivilegeSource
from ares.plugins.privileges.store import PrivStore
from ares.plugins.privileges.tools import PRIV_TOOLS
from ares.plugins.tools.comms_tools import COMMS_TOOLS
from ares.plugins.tools.core_tools import CORE_TOOLS
from ares.plugins.tools.home_tools import HOME_TOOLS
from ares.plugins.tools.memory_tools import MEMORY_TOOLS
from ares.plugins.tools.selfedit_tools import PRCache, build_selfedit_tools
from ares.plugins.tools.shell_tools import build_shell_tools
from ares.plugins.tools.task_tools import TASK_TOOLS
from ares.plugins.tools.time_tools import build_time_tools

log = get_logger(__name__)

MAX_SOURCE_RESTARTS = 10
SOURCE_RESTART_DELAY_S = 5


async def supervise(source: BaseSource, bus) -> None:
    """Supervise a single source per spec §4.2.

    Runs `source.start()`; if it returns normally the source stopped
    intentionally (e.g. CLI `!quit`) and supervision ends. If it raises,
    log the error, wait 5 s, and restart, up to 10 restarts. After the 10th
    failed restart, disable the source and emit a `source_failed` NORMAL
    event.
    """
    restarts = 0
    while True:
        try:
            await source.start()
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("source %s crashed", source.name)
            restarts += 1
            if restarts > MAX_SOURCE_RESTARTS:
                log.error(
                    "source %s exceeded %d restarts; disabling",
                    source.name,
                    MAX_SOURCE_RESTARTS,
                )
                await bus.publish(
                    Event(
                        id=new_id(),
                        source="supervisor",
                        type="source_failed",
                        payload={"source": source.name},
                        priority=Priority.NORMAL,
                    )
                )
                return
            log.info(
                "restarting source %s in %d s (attempt %d/%d)",
                source.name,
                SOURCE_RESTART_DELAY_S,
                restarts,
                MAX_SOURCE_RESTARTS,
            )
            await asyncio.sleep(SOURCE_RESTART_DELAY_S)


async def main(config_path: str) -> None:
    """Load config, build core objects, and run the daemon until shutdown."""
    env_path = Path("instance/.env")
    if not env_path.exists():
        env_path = Path("instance/.env.example")
    secrets = EnvSecretStore(env_path)
    config = load_config(Path(config_path), secrets)
    enforce_prod_tripwires(config, secret_file=env_path)

    setup_logging()

    bus = EventBus()
    sessions = SessionManager(
        history_limit=config.session.history_limit,
        timeout_minutes=config.session.timeout_minutes,
    )
    router = ResponseRouter(sessions)
    router.register(ConsoleChannel())

    push_config = config.plugins.get("push_ntfy", {})
    if push_config.get("enabled"):
        topics = {uid: u.ntfy_topic for uid, u in config.users.items() if u.ntfy_topic}
        router.register(
            NtfyChannel(
                server=push_config.get("server", ""),
                token=push_config.get("token"),
                topics=topics,
            )
        )

    llm = LLMClient(
        base_url=config.llm.base_url,
        api_key=config.llm.api_key,
        model=config.llm.model,
    )
    memory = FilesystemMemory(Path(config.memory.root))

    tasks = TaskStore(Path(config.tasks.db_path))
    await tasks.init()

    registry = ToolRegistry()
    for t in (*CORE_TOOLS, *MEMORY_TOOLS, *TASK_TOOLS):
        registry.register(t)

    time_config = config.plugins.get("time_tools", {})
    if time_config.get("enabled"):
        for t in build_time_tools(time_config):
            registry.register(t)

    shell_config = config.plugins.get("shell", {})
    if shell_config.get("enabled"):
        for t in build_shell_tools(shell_config):
            registry.register(t)

    pr_cache = PRCache()
    selfedit_config = config.plugins.get("selfedit", {})
    if selfedit_config.get("enabled"):
        for t in build_selfedit_tools(selfedit_config, pr_cache):
            registry.register(t)

    services: dict[str, object] = {}
    shutdown_event = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, shutdown_event.set)
        except NotImplementedError:
            # Signal handlers unsupported here; KeyboardInterrupt path handles it.
            log.warning("cannot install handler for %s on this platform", sig)

    sources: list[BaseSource] = []
    cli_config = config.plugins.get("cli", {})
    if cli_config.get("enabled"):
        cli = CLISource(bus, cli_config)
        cli.shutdown_event = shutdown_event
        sources.append(cli)

    sched_config = config.plugins.get("scheduler", {})
    if sched_config.get("enabled"):
        scheduler = SchedulerSource(
            bus, sched_config, tasks, memory, config.memory.retention_days
        )
        sources.append(scheduler)

    ha_service: HAService | None = None
    ha_config = config.plugins.get("home_assistant", {})
    if ha_config.get("enabled"):
        ha_service = HAService(
            rest_url=ha_config.get("rest_url", ""),
            token=ha_config.get("token", ""),
            allowed_domains=ha_config.get(
                "allowed_domains", ["binary_sensor", "person", "alarm_control_panel"]
            ),
            blocked_controls=ha_config.get("blocked_controls"),
        )
        services["home_assistant"] = ha_service
        for t in HOME_TOOLS:
            registry.register(t)
        ha_source = HomeAssistantSource(bus, ha_config, ha_service, sessions)
        sources.append(ha_source)

    voice_config = config.plugins.get("voice", {})
    if voice_config.get("enabled"):
        vad = SileroVAD()
        stt = WhisperSTT(model_size=voice_config.get("whisper_model", "small"))
        intent = IntentFilter(
            voice_config.get("intent_strategy", "hybrid"),
            voice_config.get("wake_word", "hey_ares"),
            llm=llm,
        )

        rooms = voice_config.get("rooms", {})
        mute_events: dict[str, asyncio.Event] = {}
        for room, room_cfg in rooms.items():
            ev = asyncio.Event()
            mute_events[room] = ev
            voice_source = VoiceSource(
                bus,
                voice_config,
                room,
                room_cfg.get("input_device"),
                vad,
                stt,
                intent,
                ev,
            )
            sources.append(voice_source)

        router.register(
            VoiceTTSChannel(
                rooms,
                voice_config.get("default_room", ""),
                voice_config.get("piper_model", ""),
                mute_events,
            )
        )

    sip_service: object | None = None
    sip_config = config.plugins.get("sip", {})
    if sip_config.get("enabled"):
        user_uris = {uid: u.sip_uri for uid, u in config.users.items() if u.sip_uri}

        sip_service = SIPService(
            server=sip_config.get("server", ""),
            username=sip_config.get("username", ""),
            password=sip_config.get("password", ""),
            user_uris=user_uris,
            greeting=sip_config.get("greeting", ""),
            piper_model=sip_config.get("piper_model", ""),
            whisper_model=sip_config.get("whisper_model", "small"),
            record_seconds=sip_config.get("record_seconds", 8),
            port=sip_config.get("port", 0),
            answer_settle_seconds=sip_config.get("answer_settle_seconds", 1.2),
        )
        services["sip"] = sip_service

        for t in COMMS_TOOLS:
            registry.register(t)

        sip_source = SIPSource(bus, sip_config, sip_service)
        sources.append(sip_source)
        router.register(SIPMessageChannel(sip_service))
        router.register(SIPCallChannel(sip_service))

    critical = CriticalHandlerRegistry(router)
    safety_config = config.plugins.get("safety_critical", {})
    if safety_config.get("enabled"):
        critical.register(
            FireHandler(safety_config.get("fire_entities", []), tasks, services)
        )
        critical.register(
            IntruderHandler(safety_config.get("alarm_entities", []), tasks, services)
        )

    priv_store: PrivStore | None = None
    priv_config = config.plugins.get("privileges", {})
    if priv_config.get("enabled"):
        priv_store = PrivStore(Path(priv_config.get("db_path", "instance/privq.db")))
        await priv_store.init()
        services["privileges"] = priv_store
        for t in PRIV_TOOLS:
            registry.register(t)
        priv_source = PrivilegeSource(bus, priv_config, priv_store)
        sources.append(priv_source)

    tracer = Tracer(
        config.trace.path,
        max_bytes=config.trace.max_mb * 1024 * 1024,
        backups=config.trace.backups,
        enabled=config.trace.enabled,
    )
    trace_file = str(tracer.path) if tracer.enabled else None

    dash_config = config.plugins.get("dashboard", {})
    if dash_config.get("enabled"):
        web_channel = WebChannel()
        router.register(web_channel)
        sources.append(
            DashboardSource(
                bus, dash_config, web_channel, memory, tasks, priv_store,
                pr_cache.all, trace_file,
            )
        )

    agent = Agent(
        llm=llm,
        registry=registry,
        sessions=sessions,
        tasks=tasks,
        memory=memory,
        router=router,
        services=services,
        persona=config.persona,
        max_tool_iterations=config.llm.max_tool_iterations,
        tracer=tracer,
    )
    dispatcher = Dispatcher(bus, agent, critical)

    dispatcher_task = asyncio.create_task(dispatcher.run())
    supervisor_tasks = [asyncio.create_task(supervise(s, bus)) for s in sources]

    log.info("ARES M11 daemon started (persona=%s)", config.persona.strip().splitlines()[0])

    await shutdown_event.wait()

    log.info("shutting down")
    for source in sources:
        await source.stop()

    for task in supervisor_tasks:
        task.cancel()
    dispatcher_task.cancel()

    await asyncio.gather(
        *supervisor_tasks, dispatcher_task, return_exceptions=True
    )

    await llm.aclose()
    await tasks.aclose()
    if ha_service is not None:
        await ha_service.aclose()
    if sip_service is not None:
        await sip_service.aclose()
    if priv_store is not None:
        await priv_store.aclose()


if __name__ == "__main__":
    config_path = sys.argv[1] if len(sys.argv) > 1 else "instance/config.yaml"
    try:
        asyncio.run(main(config_path))
    except KeyboardInterrupt:
        pass
