"""vcpi - Python VST3 host with Ableton Link sync."""

from importlib import import_module

__all__ = ["VcpiCore", "InstrumentTrack", "NUM_TRACKS", "Sequencer", "NUM_SEQ_BANKS"]


def __getattr__(name: str):
    """Lazily expose common core classes without importing the audio stack."""
    if name == "VcpiCore":
        return getattr(import_module("vcpi.host"), name)

    if name in {"InstrumentTrack", "NUM_TRACKS"}:
        return getattr(import_module("vcpi.models"), name)

    if name in {"Sequencer", "NUM_SEQ_BANKS"}:
        return getattr(import_module("vcpi.sequencer"), name)

    raise AttributeError(f"module 'vcpi' has no attribute {name!r}")
