"""Path helpers for runtime defaults."""

from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "vcpi"


def default_socket_path() -> Path:
    """Return a writable default Unix socket path for this user."""
    xdg_runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if xdg_runtime_dir:
        return Path(xdg_runtime_dir) / APP_NAME / f"{APP_NAME}.sock"

    if os.geteuid() == 0:
        return Path("/run") / APP_NAME / f"{APP_NAME}.sock"

    return Path("/tmp") / f"{APP_NAME}-{os.getuid()}" / f"{APP_NAME}.sock"


DEFAULT_SOCK_PATH = default_socket_path()
