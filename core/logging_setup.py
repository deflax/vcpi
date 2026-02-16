"""Logging configuration helpers for vcpi."""

from __future__ import annotations

import logging
import os
import sys


def configure_logging(default_level: str = "WARNING") -> int:
    """Configure process-wide logging and return resolved log level.

    The level is read from ``LOG_LEVEL``. If unset, ``default_level`` is used.
    """
    level_name = os.environ.get("LOG_LEVEL", default_level).upper()
    level = getattr(logging, level_name, None)
    if not isinstance(level, int):
        level = getattr(logging, default_level.upper(), logging.WARNING)
        invalid_level = level_name
    else:
        invalid_level = None

    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
        force=True,
    )

    if invalid_level is not None:
        logging.getLogger(__name__).warning(
            "Invalid LOG_LEVEL '%s'; using %s", invalid_level, logging.getLevelName(level)
        )

    return level
