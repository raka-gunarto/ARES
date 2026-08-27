from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from ares.core.event import EventBus, Priority
from ares.core.memory.base import BaseMemory
from ares.core.source import BaseSource
from ares.core.tasks.store import TaskStore, _parse_iso_utc
from ares.core.utils.logging import get_logger

log = get_logger(__name__)


class SchedulerSource(BaseSource):
    """Event source driving the two scheduler jobs from spec §7.2.

    Job 1: every 60s, check for due `reminder_pending` tasks and emit a
    HIGH `task_due` event for each (once per task, guarded by
    `data.fired`).
    Job 2: once a day at a configured local time, prune short-term memory
    and emit a LOW `housekeeping` event with the prune count.
    """

    name = "scheduler"

    def __init__(
        self,
        bus: EventBus,
        config: dict,
        tasks: TaskStore,
        memory: BaseMemory,
        retention_days: int,
    ) -> None:
        """Initialize the scheduler source.

        Args:
            bus: The EventBus to publish events to.
            config: Configuration dictionary for this source.
            tasks: TaskStore used to find and update due tasks.
            memory: Memory backend used for short-term pruning.
            retention_days: Number of days of short-term memory to retain.
        """
        super().__init__(bus, config)
        self.tasks = tasks
        self.memory = memory
        self.retention_days = retention_days
        self.housekeeping_time = str(config.get("housekeeping_time", "03:30"))

    async def start(self) -> None:
        """Run both scheduler jobs concurrently until stopped.

        Propagates CancelledError so the supervisor can treat it as an
        intentional shutdown signal.
        """
        await asyncio.gather(self._due_loop(), self._housekeeping_loop())

    async def _due_loop(self) -> None:
        """Check for due reminder tasks every 60 seconds (check-then-sleep)."""
        while not self._stopping:
            try:
                now = datetime.now(timezone.utc)
                due = await self.tasks.list_due(now)
                for task in due:
                    if task.data.get("fired"):
                        # Already emitted once; wait for the agent to close
                        # or reschedule it.
                        continue
                    await self.emit(
                        type="task_due",
                        payload={
                            "task_id": task.id,
                            "title": task.title,
                            "detail": task.detail,
                        },
                        priority=Priority.HIGH,
                        user_id=task.user_id,
                    )
                    new_data = {**task.data, "fired": True}
                    await self.tasks.update(task.id, data=new_data)

                await self._run_due_checks(now)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("scheduler due-loop iteration failed")
            await asyncio.sleep(60)

    async def _run_due_checks(self, now: datetime) -> None:
        """Emit `task_check` for every armed task whose interval has elapsed.

        This is what gives a `monitoring` task a clock. Unlike a reminder it
        re-arms rather than firing once, and it carries `trigger`/`detail` in the
        payload — neither appears in the rendered system prompt, so without them
        the agent would be told to check something without being told what.

        A monitoring task's `due_at` is read as "stop checking after this", which
        is how ARES already phrases these ("until 9am"). The last check before
        that bound is flagged `final` so the agent can wrap up and close rather
        than the task going quietly dormant.
        """
        for task in await self.tasks.list_checks_due(now):
            until = _parse_iso_utc(task.due_at)
            final = until is not None and now >= until
            await self.emit(
                type="task_check",
                payload={
                    "task_id": task.id,
                    "task_type": task.type,
                    "title": task.title,
                    "trigger": task.trigger,
                    "detail": task.detail,
                    "final": final,
                },
                priority=Priority.NORMAL,
                user_id=task.user_id,
            )
            data = {**task.data}
            if final:
                # Stop re-arming; the agent was just told this was the last one.
                data.pop("check_interval_s", None)
                data.pop("next_check_at", None)
            else:
                interval = int(data.get("check_interval_s") or 0)
                data["next_check_at"] = (
                    now + timedelta(seconds=interval)
                ).isoformat()
            await self.tasks.update(task.id, data=data)

    async def _housekeeping_loop(self) -> None:
        """Run memory pruning once a day at the configured local time."""
        while not self._stopping:
            delay = self._seconds_until_next_run()
            try:
                await asyncio.sleep(delay)
                if self._stopping:
                    break
                count = await self.memory.prune_short_term(self.retention_days)
                await self.emit(
                    type="housekeeping",
                    payload={"pruned": count},
                    priority=Priority.LOW,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("scheduler housekeeping iteration failed")

    def _seconds_until_next_run(self) -> float:
        """Compute seconds until the next local HH:MM occurrence.

        Returns:
            A positive float number of seconds. Falls back to 86400 (24h)
            if `housekeeping_time` is not parseable as "HH:MM".
        """
        try:
            hour_str, minute_str = self.housekeeping_time.split(":")
            hour, minute = int(hour_str), int(minute_str)
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError("hour/minute out of range")
        except (ValueError, AttributeError):
            log.warning(
                "scheduler: invalid housekeeping_time=%r, defaulting to 24h",
                self.housekeeping_time,
            )
            return 86400.0

        now = datetime.now().astimezone()
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return (target - now).total_seconds()
