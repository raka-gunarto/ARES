from __future__ import annotations

import logging


def setup_logging(level: int = logging.INFO) -> None:
    """Configure the root logger with a standard format and level."""
    logging.basicConfig(
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        level=level,
    )


def get_logger(name: str) -> logging.Logger:
    """Get a logger instance for the given name."""
    return logging.getLogger(name)
