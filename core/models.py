"""Shared data models and constants."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

NUM_SLOTS = 8  # matches the 8 channel strips on the Akai MIDI Mix


@dataclass
class InstrumentSlot:
    """A playable slot source with optional insert-effects chain."""

    name: str
    path: str
    plugin: object  # pedalboard ExternalPlugin
    effects: list = field(default_factory=list)
    midi_channels: set = field(default_factory=set)  # routed MIDI channels (0-15)
    gain: float = 0.8
    muted: bool = False
    solo: bool = False
    enabled: bool = True
    source_type: str = "plugin"  # plugin | wav | vcv
    vcv_patch_path: str = ""     # .vcv patch file (when source_type == "vcv")
    _effects_board: object = field(default=None, repr=False, compare=False)

    @property
    def display_label(self) -> str:
        """Qualified label shown in status/slots output.

        Format:
          sample::<pack>:<stem>   for WAV samples
          vst3::<name>            for VST3 plugins
          vcv::<patch stem>       for VCV/Cardinal patches
        """
        if self.source_type == "wav":
            parts = Path(self.path).parts
            try:
                samples_idx = list(parts).index("samples")
                pack = parts[samples_idx + 1]
            except (ValueError, IndexError):
                pack = "?"
            stem = Path(self.path).stem
            return f"sample::{pack}:{stem}"
        if self.source_type == "vcv":
            patch_stem = Path(self.vcv_patch_path).stem if self.vcv_patch_path else self.name
            return f"vcv::{patch_stem}"
        # VST3 / generic plugin
        return f"vst3::{self.name}"
