from __future__ import annotations

import uuid


def new_id() -> str:
    """Generate a new unique ID as a hex string."""
    return uuid.uuid4().hex
