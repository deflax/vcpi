"""Plain metadata and parameter snapshots from native control owners."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from collections.abc import Iterator
from pathlib import Path
import re

from vcpi.models import InstrumentTrack


PARAMETER_GROUPS = ("controls", "settings", "midi", "unused")
_CARDINAL_SETTINGS = frozenset((
    "bypass", "show_tooltips", "cable_opacity", "cable_tension",
    "room_brightness", "light_bloom", "knob_mode", "scroll_wheel_knob_control",
    "scroll_wheel_knob_sensitivity", "lock_module_positions", "update_rate_limit",
    "browser_sort", "browser_zoom", "invert_zoom", "auto_squeeze_module_positions",
))
_CARDINAL_NAMES = frozenset((
    "cardinal", "cardinalfx", "cardinalsynth", "cardinalmini", "cardinalnative",
))
_JUCE_MIDI_PARAMETER = re.compile(r"MIDI CC ([0-9]+)\|([0-9]+)")
_CARDINAL_MIDI_PARAMETER = re.compile(r"midi_ch_([0-9]+)_cc_([0-9]+)")


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
    native_state: bytes | None = None

    def parameter_group(self, name: str) -> str:
        """Classify host controls using cached identity and published parameter labels.

        plugdata marks unused automation slots with ``(DISABLED)`` in its title.
        Cardinal has a fixed host-parameter interface for MIDI and window settings.
        Never infer visibility from a control's value or automation capability.
        """
        parameter = self.parameters[name]
        midi = _JUCE_MIDI_PARAMETER.fullmatch(parameter.label)
        if midi and 0 <= int(midi[1]) < 16 and 0 <= int(midi[2]) < 130:
            return "midi"
        plugin_name = self.name.casefold().replace(" ", "")
        manufacturer = self.manufacturer_name.casefold()
        if manufacturer == "plugdata" and plugin_name in {"plugdata", "plugdata-fx"}:
            if parameter.label.startswith("(DISABLED) "):
                return "unused"
            if name in {"bypass", "program"}:
                return "settings"
        elif manufacturer == "distrho" and plugin_name in _CARDINAL_NAMES:
            midi = _CARDINAL_MIDI_PARAMETER.fullmatch(name)
            if midi and 1 <= int(midi[1]) <= 16 and 0 <= int(midi[2]) < 130:
                return "midi"
            if name in _CARDINAL_SETTINGS:
                return "settings"
        return "controls"

    def parameter_items(self, group: str = "controls") -> Iterator[tuple[str, ParameterSnapshot]]:
        """Select cached parameters in plugin order, retaining the complete inventory."""
        if group not in (*PARAMETER_GROUPS, "all"):
            raise ValueError(f"unknown parameter group '{group}'")
        for name, parameter in self.parameters.items():
            if group == "all" or self.parameter_group(name) == group:
                yield name, parameter


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
    raise TypeError(f"unsupported parameter value type: {type(value).__name__}")


def _attr(obj, name, default=None):
    try:
        value = getattr(obj, name, default)
        return default if value is None else value
    except Exception:
        return default


def capture_plugin(plugin, values, readers, *, capture_native_state=False):
    """Inspect metadata; capture opaque state only at an explicit save barrier."""
    if isinstance(plugin, PluginSnapshot):
        return (plugin if capture_native_state or plugin.native_state is None
                else replace(plugin, native_state=None))
    refresh_metadata = _attr(plugin, "_refresh_metadata")
    if refresh_metadata is not None:
        refresh_metadata()
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
    if capture_native_state:
        from vcpi.state_transport import validate_native_state

        state = plugin.get_state()
        snapshot.native_state = None if state is None else validate_native_state(state)
    raw_readers = {}
    for name, parameter in _attr(plugin, "parameters", {}).items():
        name = str(name)
        kind = _attr(parameter, "kind", "float")
        value_type = bool if kind == "bool" else str if kind == "enum" else float
        raw = parameter
        raw_readers[name] = raw
        bounds = tuple(None if bound is None else plain_value(bound)
                       for bound in _attr(parameter, "range", ()))
        snapshot.parameters[name] = ParameterSnapshot(
            range=bounds,
            kind=kind,
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
            value = (parameter.value if hasattr(parameter, "value")
                     else getattr(plugin, name))
            snapshot.values[name] = plain_value(value, value_type)
        except Exception:
            pass
    values[id(plugin)] = dict(snapshot.values)
    readers[id(plugin)] = raw_readers
    return snapshot


def apply_deck_status(track, status):
    """Refresh transport and typed parameter caches from one applied snapshot."""
    metadata = status.get("parameters")
    track.deck.update((key, value) for key, value in status.items() if key != "parameters")
    plugin = track.plugin
    if metadata is not None and plugin.parameters != metadata:
        plugin.parameters = metadata
    values = dict(status)
    if "position" in status:
        values["seek"] = status["position"]
    for name in ("index", "seek", "nudge", "play", "pitch", "sync", "phase_offset"):
        if name not in values:
            continue
        value = values[name]
        plugin.values[name] = value
        descriptor = plugin.parameters.get(name)
        if descriptor is not None:
            descriptor.string_value = str(value)
            if len(descriptor.range) == 2:
                low, high = descriptor.range
                if low is not None and high is not None and high > low:
                    descriptor.raw_value = (float(value) - low) / (high - low)


def capture_state(owner, generations, epoch, devices, cached_master_effects=None,
                  *, capture_native_state=False):
    """Copy remote track state; inspect native plugins only when stopped."""
    from vcpi import audio_deps

    engine = owner.engine
    if engine.running and cached_master_effects is None:
        raise RuntimeError("native metadata capture requires stopped audio")
    values, readers = {}, {}
    tracks = []
    for index, track in enumerate(engine.tracks):
        if track is None:
            tracks.append(None)
            continue
        if engine.running and track.render_owner is None:
            raise RuntimeError("native track metadata capture requires stopped audio")
        tracks.append({
            "name": track.name,
            "path": track.path,
            "plugin": capture_plugin(track.plugin, values, readers,
                                     capture_native_state=capture_native_state),
            "effects": [capture_plugin(effect, values, readers,
                                       capture_native_state=capture_native_state)
                        for effect in track.effects],
            "midi_channels": set(track.midi_channels),
            "gain": float(track.gain),
            "muted": bool(track.muted),
            "solo": bool(track.solo),
            "enabled": bool(track.enabled),
            "source_type": track.source_type,
            "vcv_patch_path": track.vcv_patch_path,
            "pd_patch_path": track.pd_patch_path,
            "deck": dict(track.deck),
            "generation": generations[index],
            "worker_pid": track.worker_pid,
            "worker_failure": track.worker_failure,
            "worker_misses": track.worker_misses,
            "worker_realtime": dict(track.worker_realtime),
            "buffering_samples": track.buffering_samples,
        })
    master_effects = (
        [capture_plugin(effect, values, readers, capture_native_state=capture_native_state)
         for effect in engine.master_effects]
        if cached_master_effects is None else
        [capture_plugin(effect, values, readers, capture_native_state=capture_native_state)
         for effect in cached_master_effects]
    )
    if cached_master_effects is None:
        engine.parameter_values = values
        engine.parameter_readers = readers
    return {
        "epoch": epoch,
        "tracks": tracks,
        "master_effects": master_effects,
        "master_gain": float(engine.master_gain),
        "channel_map": dict(engine.channel_map),
        "backend_label": engine.backend_label,
        "output_realtime": dict(engine.output_realtime),
        "audio_output_name": owner.audio_output_name,
        "audio_output_index": engine.output_index,
        "output_channels": engine.output_channels,
        "render_workers": engine.render_workers,
        "audio_devices": devices,
        "native_dependencies": {"native": audio_deps.HAS_NATIVE},
    }


def hydrate_tracks(records):
    return [InstrumentTrack(**record) if record is not None else None for record in records]
