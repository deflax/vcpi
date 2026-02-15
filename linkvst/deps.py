"""Graceful optional dependency imports.

Every other module imports availability flags from here so the try/except
blocks live in exactly one place.
"""

from __future__ import annotations

# -- pedalboard (VST3 hosting) ----------------------------------------------

try:
    from pedalboard import Pedalboard, load_plugin
    HAS_PEDALBOARD = True
except ImportError:
    Pedalboard = None  # type: ignore[assignment,misc]
    load_plugin = None  # type: ignore[assignment]
    HAS_PEDALBOARD = False

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

# -- sounddevice (real-time audio I/O) --------------------------------------

try:
    import sounddevice as sd
    HAS_SOUNDDEVICE = True
except ImportError:
    sd = None  # type: ignore[assignment]
    HAS_SOUNDDEVICE = False

# -- numpy (always required) ------------------------------------------------

import numpy as np
