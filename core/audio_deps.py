"""Native dependencies imported only by the audio owner and sampler tools."""

from __future__ import annotations

import numpy as np

try:
    from pedalboard import Pedalboard, load_plugin
    HAS_PEDALBOARD = True
except ImportError:
    Pedalboard = None
    load_plugin = None
    HAS_PEDALBOARD = False

try:
    import sounddevice as sd
    HAS_SOUNDDEVICE = True
except (ImportError, OSError):
    sd = None
    HAS_SOUNDDEVICE = False
