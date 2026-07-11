"""ARES daemon entrypoint (M5): wires the event bus, CLI and scheduler
sources, console channel, push notification channel, memory storage, real
TaskStore, and the real Agent (spec §4.10) together and runs until shutdown.

This is the ONLY file in the repo that performs instance wiring (spec §8).
Per M5 scope, CLI source, scheduler source, console channel, push channel
(when enabled), FilesystemMemory, and real TaskStore are wired. Memory tools
and task tools (update_task, get_task_history) are registered as discoverable
tools so search_tools can locate them (§4.10).

The push notification channel (NtfyChannel) is registered when the push_ntfy
plugin is enabled in config (§7.6).

Other plugins (home_assistant, voice, sip, dashboard, etc.) do not exist yet
and are wired in later milestones — their config sections are ignored without
error.

The Agent's §4.10 signature hard-requires a TaskStore and a BaseMemory.
Both are now real (M4): TaskStore backed by SQLite, FilesystemMemory on disk.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from ares.core.agent import Agent
from ares.core.config import load_config
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
from ares.plugins.sources.cli import CLISource
from ares.plugins.sources.scheduler import SchedulerSource
from ares.plugins.tools.core_tools import CORE_TOOLS
from ares.plugins.tools.memory_tools import MEMORY_TOOLS
from ares.plugins.tools.task_tools import TASK_TOOLS

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
    for t in CORE_TOOLS:
        registry.register(t)
    for t in MEMORY_TOOLS:
        registry.register(t)
    for t in TASK_TOOLS:
        registry.register(t)

    critical = CriticalHandlerRegistry(router)
    agent = Agent(
        llm=llm,
        registry=registry,
        sessions=sessions,
        tasks=tasks,
        memory=memory,
        router=router,
        services={},
        persona=config.persona,
        max_tool_iterations=config.llm.max_tool_iterations,
    )
    dispatcher = Dispatcher(bus, agent, critical)

    shutdown_event = asyncio.Event()

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

    dispatcher_task = asyncio.create_task(dispatcher.run())
    supervisor_tasks = [asyncio.create_task(supervise(s, bus)) for s in sources]

    log.info("ARES M5 daemon started (persona=%s)", config.persona.strip().splitlines()[0])

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


if __name__ == "__main__":
    config_path = sys.argv[1] if len(sys.argv) > 1 else "instance/config.yaml"
    try:
        asyncio.run(main(config_path))
    except KeyboardInterrupt:
        pass
