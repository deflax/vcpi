"""Plain audio-owner snapshots. Native inspection is restricted to stopped audio."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from core.models import InstrumentSlot


@dataclass(slots=True)
class ParameterSnapshot:
    range: tuple = ()
    kind: str = "float"
    label: str = ""
    units: str = ""
    valid_values: tuple = ()
    raw_value: float = 0.0
    string_value: str = ""
    is_automatable: bool = False
    is_boolean: bool = False
    is_discrete: bool = False


@dataclass(slots=True)
class PluginSnapshot:
    token: int
    name: str = ""
    descriptive_name: str = "n/a"
    manufacturer_name: str = "n/a"
    category: str = "n/a"
    version: str = "n/a"
    identifier: str = "n/a"
    reported_latency_samples: int = 0
    info_type: str = "Unknown"
    path_to_plugin_file: str = ""
    parameters: dict[str, ParameterSnapshot] = field(default_factory=dict)
    values: dict = field(default_factory=dict)


def plain_value(value, value_type=None):
    """Strip native-backed scalar subclasses before caching or sending them."""
    if value_type is bool:
        return bool(value)
    if isinstance(value, bool):
        return bool(value)
    if isinstance(value, str):
        return str(value)
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value)
    # bool cannot be subclassed; pedalboard exposes a boolean surrogate with
    # its parameter's public type and __bool__, rather than a builtin bool.
    if getattr(value, "type", None) is bool:
        return bool(value)
    raise TypeError(f"unsupported parameter value type: {type(value).__name__}")


def _attr(obj, name, default=None):
    try:
        value = getattr(obj, name, default)
        return default if value is None else value
    except Exception:
        return default


def capture_plugin(plugin, values, readers):
    """Inspect one native plugin on its owner's main thread with audio stopped."""
    path = str(_attr(plugin, "_vcpi_path", _attr(plugin, "path_to_plugin_file", "")))
    snapshot = PluginSnapshot(
        token=id(plugin),
        name=str(_attr(plugin, "name", Path(path).stem or type(plugin).__name__)),
        descriptive_name=str(_attr(plugin, "descriptive_name", "n/a")),
        manufacturer_name=str(_attr(plugin, "manufacturer_name", "n/a")),
        category=str(_attr(plugin, "category", "n/a")),
        version=str(_attr(plugin, "version", "n/a")),
        identifier=str(_attr(plugin, "identifier", "n/a")),
        reported_latency_samples=int(_attr(plugin, "reported_latency_samples", 0)),
        info_type=str(_attr(plugin, "info_type", "Unknown")),
        path_to_plugin_file=path,
    )
    raw_readers = {}
    for name, parameter in _attr(plugin, "parameters", {}).items():
        name = str(name)
        value_type = _attr(parameter, "type", float)
        raw = None
        try:
            raw = plugin._get_parameter(name)
        except Exception:
            pass
        if raw is not None:
            raw_readers[name] = raw
        bounds = tuple(None if bound is None else plain_value(bound)
                       for bound in _attr(parameter, "range", ()))
        snapshot.parameters[name] = ParameterSnapshot(
            range=bounds,
            kind="bool" if value_type is bool else "enum" if value_type is str else "float",
            label=str(_attr(parameter, "label", "")),
            units=str(_attr(parameter, "units", "")),
            valid_values=tuple(str(value) for value in _attr(parameter, "valid_values", ())),
            raw_value=float(_attr(raw, "raw_value", 0)),
            string_value=str(_attr(raw, "string_value", "")),
            is_automatable=bool(_attr(raw, "is_automatable", False)),
            is_boolean=bool(_attr(raw, "is_boolean", False)),
            is_discrete=bool(_attr(raw, "is_discrete", False)),
        )
        try:
            snapshot.values[name] = plain_value(getattr(plugin, name), value_type)
        except Exception:
            pass
    values[id(plugin)] = dict(snapshot.values)
    readers[id(plugin)] = raw_readers
    return snapshot


def capture_state(owner, generations, epoch, devices):
    """Capture full metadata only while the audio owner is quiescent."""
    from core import audio_deps

    engine = owner.engine
    if engine.running:
        raise RuntimeError("native metadata capture requires stopped audio")
    values, readers = {}, {}
    slots = []
    for index, slot in enumerate(engine.slots):
        if slot is None:
            slots.append(None)
            continue
        slots.append({
            "name": slot.name,
            "path": slot.path,
            "plugin": capture_plugin(slot.plugin, values, readers),
            "effects": [capture_plugin(effect, values, readers) for effect in slot.effects],
            "midi_channels": set(slot.midi_channels),
            "gain": float(slot.gain),
            "muted": bool(slot.muted),
            "solo": bool(slot.solo),
            "enabled": bool(slot.enabled),
            "source_type": slot.source_type,
            "vcv_patch_path": slot.vcv_patch_path,
            "generation": generations[index],
        })
    master_effects = [capture_plugin(effect, values, readers) for effect in engine.master_effects]
    engine.parameter_values = values
    engine.parameter_readers = readers
    return {
        "epoch": epoch,
        "slots": slots,
        "master_effects": master_effects,
        "master_gain": float(engine.master_gain),
        "channel_map": dict(engine.channel_map),
        "backend_label": engine.backend_label,
        "audio_output_name": owner.audio_output_name,
        "output_channels": engine.output_channels,
        "render_workers": engine.render_workers,
        "audio_devices": devices,
        "native_dependencies": {
            "pedalboard": audio_deps.HAS_PEDALBOARD,
            "sounddevice": audio_deps.HAS_SOUNDDEVICE,
        },
    }


def hydrate_slots(records):
    return [InstrumentSlot(**record) if record is not None else None for record in records]
