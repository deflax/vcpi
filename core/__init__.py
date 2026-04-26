"""vcpi - Python VST3 host with Ableton Link sync."""

from importlib import import_module

__all__ = ["VcpiCore", "InstrumentSlot", "NUM_SLOTS", "Sequencer", "NUM_SEQ_BANKS"]


def __getattr__(name: str):
    """Lazily expose common core classes without importing the audio stack."""
    if name == "VcpiCore":
        return getattr(import_module("core.host"), name)

    if name in {"InstrumentSlot", "NUM_SLOTS"}:
        return getattr(import_module("core.models"), name)

    if name in {"Sequencer", "NUM_SEQ_BANKS"}:
        return getattr(import_module("core.sequencer"), name)

    raise AttributeError(f"module 'core' has no attribute {name!r}")
