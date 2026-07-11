"""ARES daemon entrypoint (M1): wires the event bus, CLI source, console
channel, and a temporary echo-agent stub together and runs until shutdown.

This is the ONLY file in the repo that performs instance wiring (spec §8).
Per M1 scope, only the CLI source and console channel are constructed here.
Other plugins (scheduler, home_assistant, voice, sip, dashboard, etc.) do not
exist yet and are wired in later milestones — their config sections are
ignored without error.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from ares.core.config import load_config
from ares.core.critical import CriticalHandlerRegistry
from ares.core.dispatcher import Dispatcher
from ares.core.event import Event, EventBus, Priority
from ares.core.router import ResponseRouter
from ares.core.secrets import EnvSecretStore
from ares.core.session import SessionManager
from ares.core.source import BaseSource
from ares.core.utils.ids import new_id
from ares.core.utils.logging import get_logger, setup_logging
from ares.plugins.channels.console import ConsoleChannel
from ares.plugins.sources.cli import CLISource

log = get_logger(__name__)

MAX_SOURCE_RESTARTS = 10
SOURCE_RESTART_DELAY_S = 5


class EchoAgent:
    """Temporary M1 stub standing in for the real Agent (arrives in M2).

    Simply echoes the inbound text back to the user via the ResponseRouter.
    Replace this class wholesale once ares/core/agent.py exists.
    """

    def __init__(self, sessions: SessionManager, router: ResponseRouter) -> None:
        """
        Args:
            sessions: The session manager (the Dispatcher reads
                `agent.sessions` to touch sessions on each event).
            router: The response router used to deliver the echo reply.
        """
        self.sessions = sessions
        self.router = router

    async def handle(self, event: Event) -> None:
        """Echo the event's text payload back to the user's active channel.

        Never raises: any failure is logged and swallowed so a single bad
        event cannot take down the per-user worker loop.
        """
        try:
            text = event.payload.get("text", "")
            await self.router.speak(event.user_id, f"echo: {text}")
        except Exception:
            log.exception("EchoAgent.handle failed for event %s", event.id)


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

    critical = CriticalHandlerRegistry(router)
    agent = EchoAgent(sessions, router)
    dispatcher = Dispatcher(bus, agent, critical)

    shutdown_event = asyncio.Event()

    sources: list[BaseSource] = []
    cli_config = config.plugins.get("cli", {})
    if cli_config.get("enabled"):
        cli = CLISource(bus, cli_config)
        cli.shutdown_event = shutdown_event
        sources.append(cli)

    dispatcher_task = asyncio.create_task(dispatcher.run())
    supervisor_tasks = [asyncio.create_task(supervise(s, bus)) for s in sources]

    log.info("ARES M1 daemon started (persona=%s)", config.persona.strip().splitlines()[0])

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


if __name__ == "__main__":
    config_path = sys.argv[1] if len(sys.argv) > 1 else "instance/config.yaml"
    try:
        asyncio.run(main(config_path))
    except KeyboardInterrupt:
        pass
