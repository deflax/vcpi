"""Logging configuration helpers for vcpi."""

from __future__ import annotations

import logging
import os

from core.native_output import logging_stream, set_debug_output


def configure_logging(default_level: str = "INFO") -> int:
    """Configure process-wide logging and return resolved log level.

    The level is read from ``LOG_LEVEL``. If unset, ``default_level`` is used.
    """
    level_name = os.environ.get("LOG_LEVEL", default_level).upper()
    level = getattr(logging, level_name, None)
    if not isinstance(level, int):
        level = getattr(logging, default_level.upper(), logging.INFO)
        invalid_level = level_name
    else:
        invalid_level = None

    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=logging_stream(),
        force=True,
    )
    set_debug_output(level <= logging.DEBUG)

    if invalid_level is not None:
        logging.getLogger(__name__).warning(
            "Invalid LOG_LEVEL '%s'; using %s", invalid_level, logging.getLevelName(level)
        )

    return level


def set_log_level(level: str) -> str:
    """Toggle runtime logging and let subsequently spawned workers inherit it."""
    if level not in ("DEBUG", "INFO"):
        raise ValueError("log level must be DEBUG or INFO")
    logging.getLogger().setLevel(level)
    os.environ["LOG_LEVEL"] = level
    set_debug_output(level == "DEBUG")
    return logging.getLevelName(logging.getLogger().getEffectiveLevel())
