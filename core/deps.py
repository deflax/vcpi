"""Control-process dependencies; native audio imports live in audio_deps."""

from __future__ import annotations

# -- aalink (Ableton Link) --------------------------------------------------

try:
    import aalink
    HAS_LINK = True
except ImportError:
    aalink = None  # type: ignore[assignment]
    HAS_LINK = False

# -- python-rtmidi -----------------------------------------------------------

try:
    import rtmidi
    HAS_RTMIDI = True
except ImportError:
    rtmidi = None  # type: ignore[assignment]
    HAS_RTMIDI = False

# -- mido (MIDI message construction) ---------------------------------------

try:
    import mido
    HAS_MIDO = True
except ImportError:
    mido = None  # type: ignore[assignment]
    HAS_MIDO = False

