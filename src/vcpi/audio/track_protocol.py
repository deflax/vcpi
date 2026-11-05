"""Per-track IPC limits, message encoding, and terminal errors."""

from __future__ import annotations

import pickle

MIDI_CAPACITY = 4096
MIDI_MESSAGE_LIMIT = 1024
CONTROL_LIMIT = 64 * 1024
PARAMETER_BATCH_LIMIT = 64
STATE_LIMIT = 16 * 1024 * 1024
STARTUP_TIMEOUT = 90.0  # Leave cleanup time inside the outer 120-second RPC.
REQUEST_TIMEOUT = 90.0
PARAMETER_TIMEOUT = 5.0
RENDER_TIMEOUT = 2.0
SHUTDOWN_TIMEOUT = 0.5
_REMOTE_ERRORS = {cls.__name__: cls for cls in (
    ValueError, TypeError, IndexError, KeyError, FileNotFoundError,
    PermissionError, RuntimeError,
)}
_LOAD_OPERATIONS = frozenset((
    "load_instrument", "load_wav", "load_vcv", "load_vcv_patch",
    "load_pd", "load_pd_patch", "load_deck",
))


class TrackProcessError(RuntimeError):
    """A terminal failure confined to a track, not the output device owner."""


def _encode(value, limit):
    data = pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
    if len(data) > limit:
        raise TrackProcessError(f"track protocol message exceeds {limit} bytes")
    return data
