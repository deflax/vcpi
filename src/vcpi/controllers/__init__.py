"""Controller modules for external MIDI hardware."""

from vcpi.controllers.akai_midimix import MidiMixController
from vcpi.controllers.midi_input import MidiInputController

__all__ = ["MidiInputController", "MidiMixController"]
