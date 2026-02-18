"""Controller modules for external MIDI hardware."""

from controllers.akai_midimix import MidiMixController
from controllers.midi_input import MidiInputController

__all__ = ["MidiInputController", "MidiMixController"]
