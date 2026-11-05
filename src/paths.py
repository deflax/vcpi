"""Path helpers for runtime defaults."""

from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "vcpi"

DEFAULT_PID_PATH = Path(f"~/.config/{APP_NAME}/{APP_NAME}.pid").expanduser()


def default_socket_path() -> Path:
    """Return a writable default Unix socket path for this user."""
    xdg_runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if xdg_runtime_dir:
        return Path(xdg_runtime_dir) / APP_NAME / f"{APP_NAME}.sock"

    if os.geteuid() == 0:
        return Path("/run") / APP_NAME / f"{APP_NAME}.sock"

    return Path("/tmp") / f"{APP_NAME}-{os.getuid()}" / f"{APP_NAME}.sock"


DEFAULT_SOCK_PATH = default_socket_path()


def is_pid_alive(pid: int) -> bool:
    """Check whether a process with the given PID is still running."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we can't signal it — still alive.
        return True


def check_pidfile(pid_path: Path = DEFAULT_PID_PATH) -> int | None:
    """Return the PID of a running vcpi server, or None if no live instance."""
    if not pid_path.exists():
        return None
    try:
        pid = int(pid_path.read_text().strip())
    except (ValueError, OSError):
        return None
    if is_pid_alive(pid):
        return pid
    # Stale PID file — process is gone.
    return None


def write_pidfile(pid_path: Path = DEFAULT_PID_PATH):
    """Write the current process PID to the PID file."""
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(os.getpid()) + "\n")


def remove_pidfile(pid_path: Path = DEFAULT_PID_PATH):
    """Remove the PID file if it exists."""
    try:
        pid_path.unlink()
    except OSError:
        pass
