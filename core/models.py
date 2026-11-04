"""Shared data models and constants."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

NUM_TRACKS = 8  # matches the 8 channel strips on the Akai MIDI Mix


@dataclass
class InstrumentTrack:
    """A playable track source with optional insert-effects chain."""

    name: str
    path: str
    plugin: object  # native plugin in track owner, PluginSnapshot elsewhere
    effects: list = field(default_factory=list)
    midi_channels: set = field(default_factory=set)  # routed MIDI channels (0-15)
    gain: float = 0.8
    muted: bool = False
    solo: bool = False
    enabled: bool = True
    source_type: str = "plugin"  # plugin | wav | vcv | pd
    vcv_patch_path: str = ""     # .vcv patch file (when source_type == "vcv")
    pd_patch_path: str = ""      # .pd patch file (when source_type == "pd")
    generation: int = 0  # protects queued inputs from instrument replacement
    render_owner: object = field(default=None, repr=False, compare=False)
    worker_pid: int | None = None
    worker_failure: str | None = None
    worker_misses: int = 0
    worker_realtime: dict = field(default_factory=dict)
    buffering_samples: int = 0

    @property
    def display_label(self) -> str:
        """Qualified label shown in status/tracks output.

        Format:
          sample::<pack>:<stem>   for WAV samples
          vst3::<name>            for VST3 plugins
          vcv::<patch stem>       for VCV/Cardinal patches
          pd::<patch stem>        for Pure Data/plugdata patches
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
        if self.source_type == "pd":
            patch_stem = Path(self.pd_patch_path).stem if self.pd_patch_path else self.name
            return f"pd::{patch_stem}"
        # VST3 / generic plugin
        return f"vst3::{self.name}"
