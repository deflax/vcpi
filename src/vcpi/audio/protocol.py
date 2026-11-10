"""Bounded audio-owner messages, limits, and remote errors."""

from __future__ import annotations

import pickle

INPUT_CAPACITY = 4096
CONTROL_LIMIT = 64 * 1024
STATE_LIMIT = 16 * 1024 * 1024
INPUT_LIMIT = 16 * 1024
UPDATE_LIMIT = 64 * 1024
CALLBACK_STALL_TIMEOUT = 2.0
OWNER_SHUTDOWN_TIMEOUT = 10.0
LIFECYCLE_OPERATIONS = frozenset({
    "load_instrument", "remove_instrument", "load_effect",
    "remove_effect", "load_vcv", "load_vcv_patch", "load_pd", "load_pd_patch", "load_deck",
})
REMOTE_ERRORS = {
    cls.__name__: cls for cls in
    (ValueError, TypeError, IndexError, KeyError, FileNotFoundError,
     PermissionError, RuntimeError, BufferError, TimeoutError)
}


class AudioProcessError(RuntimeError):
    """The audio owner is unavailable; no command was silently accepted."""


def _encode(message, limit):
    data = pickle.dumps(message, protocol=pickle.HIGHEST_PROTOCOL)
    if len(data) > limit:
        raise BufferError(f"audio protocol message exceeds {limit} bytes")
    return data
