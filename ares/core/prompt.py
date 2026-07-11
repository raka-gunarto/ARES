"""System prompt builder for ARES assistant."""
from __future__ import annotations

import typing
from datetime import datetime

if typing.TYPE_CHECKING:
    from ares.core.tasks.store import Task

from ares.core.session import Session


def build_system_prompt(
    persona: str, now: datetime, session: Session, open_tasks: list[Task]
) -> dict:
    """
    Build a system prompt for the ARES assistant.

    Assembles a multi-part system prompt containing:
    - The persona text
    - Current date/time and timezone
    - Active channel and current room
    - List of open tasks
    - Fixed instruction block

    Args:
        persona: The persona text to include.
        now: Current datetime (may be tz-aware).
        session: The user's current session.
        open_tasks: List of open tasks (or empty list).

    Returns:
        A dict with role "system" and assembled content string.
    """
    parts = []

    # 1. Persona text
    parts.append(persona)

    # 2. Current date/time and timezone
    parts.append(f"Current time: {now:%Y-%m-%d %H:%M %Z}")

    # 3. Active channel and current room
    room = session.current_room or "unknown"
    parts.append(f"Active channel: {session.active_channel}. User's current room: {room}.")

    # 4. Open tasks or "No open tasks."
    if open_tasks:
        for task in open_tasks:
            parts.append(f"- [{task.type}] {task.title} (id={task.id})")
    else:
        parts.append("No open tasks.")

    # 5. Fixed instruction block
    instruction_block = (
        "You act through tools. Use `speak` to talk to the user; plain text replies are "
        "not delivered. Use `search_tools` to find capabilities you don't currently "
        "have — memory, home control, calendar, weather, communications. Check memory "
        "before claiming you don't know something about the user or the home. Create a "
        "task whenever you are waiting on something or someone. Keep spoken replies "
        "brief and natural. If an ambient event needs no action, reply with the single "
        "word: IGNORE."
    )
    parts.append(instruction_block)

    content = "\n".join(parts)

    return {"role": "system", "content": content}
