"""Shared data models and constants."""

from __future__ import annotations

from dataclasses import dataclass, field

NUM_SLOTS = 8  # matches the 8 channel strips on the Akai MIDI Mix


@dataclass
class InstrumentSlot:
    """A VST3 instrument with optional insert-effects chain."""

    name: str
    path: str
    plugin: object  # pedalboard ExternalPlugin
    effects: list = field(default_factory=list)
    midi_channels: set = field(default_factory=set)  # routed MIDI channels (0-15)
    gain: float = 0.8
    muted: bool = False
    solo: bool = False
    enabled: bool = True
