"""ARES daemon entrypoint — the only file that performs instance wiring (spec §8).

Builds the core (event bus, sessions, router, TaskStore, FilesystemMemory,
tool registry, tracer, Agent, Dispatcher) and then wires each plugin only when
its `plugins.<name>.enabled` flag is set: console channel, CLI and scheduler
sources, ntfy push, Home Assistant (+ the speaker channel and safety-critical
handlers, §4.9/§7.7), voice rooms (§7.4), SIP (§7.5), time tools, the
sandboxed shell (§15), fetch_page and the stateful browser (§6), privilege
requests (§16), self-edit (§18), subagents (§20) and the dashboard (§17).

Prod tripwires (`enforce_prod_tripwires`) run before anything is started, so a
missing separation fails fast. Privilege approve/deny is never called from
here: those are dashboard-operator actions only (§14).
"""
from __future__ import annotations

import asyncio
import os
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
from ares.core.subagents import SubagentManager
from ares.core.tasks.store import TaskStore
from ares.core.tool import ToolRegistry
from ares.core.utils.ids import new_id
from ares.core.utils.logging import get_logger, setup_logging
from ares.plugins.channels.console import ConsoleChannel
from ares.plugins.channels.push_ntfy import NtfyChannel
from ares.plugins.channels.sip_call import SIPCallChannel
from ares.plugins.channels.sip_message import SIPMessageChannel
from ares.plugins.channels.speaker import SpeakerChannel
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
from ares.plugins.tools.browser_session import BrowserSession
from ares.plugins.tools.browser_tool import Browser
from ares.plugins.tools.browser_tools import build_browser_tools
from ares.plugins.tools.shell_tools import build_shell_tools
from ares.plugins.tools.subagent_tools import SUBAGENT_TOOLS
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


def resolve_secret_env_path() -> Path:
    """Return the dotenv path the daemon reads secrets from.

    Dev falls back to the committed ``instance/.env.example`` for convenience.
    In prod, secrets come exclusively from ``os.environ`` (systemd
    ``EnvironmentFile=/etc/ares/.env``, 0600 root:root), so we never point at a
    readable dotenv in the app tree: an updater-checked-out release still
    contains the committed ``instance/.env.example``, and reading it would
    (correctly) trip the §14.4 secret-file tripwire and refuse to start.
    """
    env_path = Path("instance/.env")
    if not env_path.exists() and os.environ.get("ARES_ENV", "dev") != "prod":
        env_path = Path("instance/.env.example")
    return env_path


async def main(config_path: str) -> None:
    """Load config, build core objects, and run the daemon until shutdown."""
    env_path = resolve_secret_env_path()
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
    if config.plugins.get("console_channel", {}).get("enabled", True):
        router.register(ConsoleChannel())

    push_config = config.plugins.get("push_ntfy", {})
    ntfy: NtfyChannel | None = None
    if push_config.get("enabled"):
        topics = {uid: u.ntfy_topic for uid, u in config.users.items() if u.ntfy_topic}
        ntfy = NtfyChannel(
            server=push_config.get("server", ""),
            token=push_config.get("token"),
            topics=topics,
        )
        router.register(ntfy)

    llm = LLMClient(
        base_url=config.llm.base_url,
        api_key=config.llm.api_key,
        model=config.llm.model,
        max_tokens=config.llm.max_tokens,
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

    # The browser reuses the shell plugin's sandbox identity: one sandbox user,
    # one audited sudo entry point (§15). Enabled via its own `browser` block so
    # it can be turned off without giving up the shell.
    browser_config = config.plugins.get("browser", {})
    browser_session: BrowserSession | None = None
    if browser_config.get("enabled"):
        merged = {**shell_config, **browser_config}
        for t in build_browser_tools(merged):
            registry.register(t)
        # The stateful browser (§6.6) needs its own uid for the login profile;
        # in prod it is only offered once that user is configured.
        browser_user = browser_config.get("browser_user", "")
        if browser_user or os.environ.get("ARES_ENV", "dev") != "prod":
            browser_session = BrowserSession(
                browser_user=browser_user,
                workdir=merged.get("workdir", ""),
                binary=browser_config.get("browser_binary", ""),
                profile_dir=browser_config.get("session_profile_dir", ".ares-browser"),
                idle_close_s=int(browser_config.get("session_idle_close_s", 1800)),
                sandbox_user=shell_config.get("sandbox_user", ""),
            )
            registry.register(Browser(browser_session))

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
            # Entities allowed individually still belong in the snapshot; without
            # this a thermostat added via allowed_entities generates events but
            # never appears in the house summary pasted into the prompt.
            snapshot_entities=ha_config.get("allowed_entities", []),
        )
        services["home_assistant"] = ha_service
        for t in HOME_TOOLS:
            registry.register(t)
        ha_source = HomeAssistantSource(bus, ha_config, ha_service, sessions)
        sources.append(ha_source)

    # SPEAKER delivery: announce ARES's speech on an HA media_player when the
    # user is home but off the active channel. Needs Home Assistant, so it is
    # wired only when HA is up (ha_service set above).
    speaker_config = config.plugins.get("speaker", {})
    speaker_channel: SpeakerChannel | None = None
    if speaker_config.get("enabled") and ha_service is not None:
        speaker_channel = SpeakerChannel(
            ha_service=ha_service,
            presence_entities=speaker_config.get("presence_entities", []),
            service_entity=speaker_config.get("service_entity", ""),
            service_data=speaker_config.get("service_data", {}),
            tts_domain=speaker_config.get("tts_domain", "tts"),
            tts_service=speaker_config.get("tts_service", "speak"),
            tts_field=speaker_config.get("tts_field", "message"),
            language=speaker_config.get("language"),
        )
        router.register(speaker_channel)
        # Safety alerts (§7.7) announce on the speaker and skip the phone call
        # when someone is home.
        services.setdefault("announcers", []).append(speaker_channel.announce)
        services["presence"] = speaker_channel.anyone_home

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

        voice_channel = VoiceTTSChannel(
            rooms,
            voice_config.get("default_room", ""),
            voice_config.get("piper_model", ""),
            mute_events,
        )
        router.register(voice_channel)
        services.setdefault("announcers", []).append(voice_channel.broadcast)

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
            silence_seconds=sip_config.get("silence_seconds", 1.0),
            silence_rms_threshold=sip_config.get("silence_rms_threshold", 500),
            post_speech_guard_seconds=sip_config.get("post_speech_guard_seconds", 0.3),
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

    # Background subagents (§20). Constructed after every tool plugin has
    # registered, because a run's allowlist is resolved against the registry.
    subagent_manager: SubagentManager | None = None
    sub_config = config.plugins.get("subagents", {})
    if sub_config.get("enabled"):
        subagent_manager = SubagentManager(
            bus=bus,
            llm=llm,
            registry=registry,
            tasks=tasks,
            memory=memory,
            services=services,
            max_iterations=sub_config.get("max_iterations", 25),
            timeout_s=sub_config.get("timeout_s", 900),
            max_concurrent=sub_config.get("max_concurrent", 3),
            max_result_chars=sub_config.get("max_result_chars", 4000),
        )
        services["subagents"] = subagent_manager
        for t in SUBAGENT_TOOLS:
            registry.register(t)
        # A run's asyncio task died with the previous process; never leave a row
        # claiming to be in flight that nothing will finish (§20.1).
        await subagent_manager.recover_orphans()

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
                router=router,
                subagent_manager=subagent_manager,
                home_provider=(
                    speaker_channel.anyone_home
                    if speaker_channel is not None
                    else None
                ),
                browser=browser_session,
                # §17.4: requests without a valid token are pushed to the
                # operator when they have an ntfy topic; logged regardless.
                auth_alert=(
                    (lambda title, msg: ntfy.notify("primary", msg, title=title, tags="warning"))
                    if ntfy is not None and ntfy.topics.get("primary")
                    else None
                ),
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
        context_window=config.llm.context_window,
        max_output_tokens=config.llm.max_tokens,
        tracer=tracer,
    )
    dispatcher = Dispatcher(bus, agent, critical)

    dispatcher_task = asyncio.create_task(dispatcher.run())
    supervisor_tasks = [asyncio.create_task(supervise(s, bus)) for s in sources]

    log.info("ARES daemon started (persona=%s)", config.persona.strip().splitlines()[0])

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

    if subagent_manager is not None:
        await subagent_manager.shutdown()

    if browser_session is not None:
        await browser_session.aclose()

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
