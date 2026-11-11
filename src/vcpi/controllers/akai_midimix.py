"""Akai MIDI Mix controller integration."""

from __future__ import annotations

import logging
from itertools import islice
from threading import RLock
from typing import Optional

from vcpi.deck_bank import selectable_indexes
from vcpi.midi import MidiInPort, MidiOutPort
from vcpi.controllers.bindings import (
    ControlBinding, cached_value, input_availability, parameter_bounds,
    parameter_range, parameter_value,
)


logger = logging.getLogger(__name__)

# -- CC numbers per channel strip (1-8) -------------------------------------

FADER_CCS = [19, 23, 27, 31, 49, 53, 57, 61]

KNOB_CCS = [
    # (high, mid, low) per strip
    (16, 17, 18),
    (20, 21, 22),
    (24, 25, 26),
    (28, 29, 30),
    (46, 47, 48),
    (50, 51, 52),
    (54, 55, 56),
    (58, 59, 60),
]

MUTE_NOTES = [1, 4, 7, 10, 13, 16, 19, 22]
SOLO_NOTES = [3, 6, 9, 12, 15, 18, 21, 24]

MASTER_FADER_CC = 62


def build_cc_lookups() -> tuple[dict[int, int], dict[int, tuple[int, int]]]:
    """Return (cc_to_fader_track, cc_to_knob_track_and_index) lookup dicts."""
    cc_to_fader: dict[int, int] = {}
    cc_to_knob: dict[int, tuple[int, int]] = {}
    for track_idx in range(len(FADER_CCS)):
        cc_to_fader[FADER_CCS[track_idx]] = track_idx
        for knob_idx, cc in enumerate(KNOB_CCS[track_idx]):
            cc_to_knob[cc] = (track_idx, knob_idx)
    return cc_to_fader, cc_to_knob


def _knob_parameters(track):
    """Resolve the same three cached destinations for dispatch and display."""
    if track is None or track.plugin is None:
        return []
    plugin = track.plugin
    # This profile uses the fader for track gain, not a knob for plugdata volume.
    skip_volume = (
        plugin.manufacturer_name.casefold() == "plugdata"
        and plugin.name.casefold().replace(" ", "") in {"plugdata", "plugdata-fx"}
    )
    candidates = (
        (name, parameter) for name, parameter in plugin.parameter_items("controls")
        if not (skip_volume and name == "volume")
    )
    return list(islice(candidates, 3))


def midimix_bindings(engine, input_name, output_name) -> list[ControlBinding]:
    """Describe the expected MIDI Mix profile without querying any device."""
    bindings = []
    for index, track in enumerate(islice(engine.tracks, len(FADER_CCS))):
        label = f"Track {index + 1}"
        availability, reason = (input_availability(input_name) if track is not None
                                else ("unavailable", "empty track"))
        bindings.append(ControlBinding(
            "midimix", f"{label} fader", f"CC {FADER_CCS[index]}",
            f"{label} gain", cached_value(track.gain) if track is not None else "-",
            availability, reason, index + 1, "0 .. 1", "absolute",
        ))
        parameters = _knob_parameters(track)
        for knob, cc in enumerate(KNOB_CCS[index]):
            state, why = availability, reason
            value, bounds = "-", ""
            mode = "absolute"
            if knob < len(parameters):
                name, parameter = parameters[knob]
                source = "deck source" if track.source_type == "deck" else "instrument"
                target = f"{label} {source}: {name}"
                value = parameter_value(track.plugin, name, parameter)
                bounds = parameter_range(parameter)
                if parameter.kind == "bool":
                    mode = "absolute; CC 0-63 / 64-127"
                if parameter.kind == "enum" and not parameter.valid_values:
                    state, why = "unsupported", "no enum values"
            else:
                target = f"{label}: (unbound)"
                if track is not None:
                    state, why = "unbound", "no exposed instrument parameter"
            bindings.append(ControlBinding(
                "midimix", f"{label} knob {knob + 1}", f"CC {cc}",
                target, value, state, why, index + 1, bounds, mode,
            ))
        for field, notes, enabled in (
                ("mute", MUTE_NOTES, bool(track is not None and track.muted)),
                ("solo", SOLO_NOTES, bool(track is not None and track.solo))):
            feedback = (f"LED {'on' if enabled else 'off'} "
                        f"(velocity {127 if enabled else 0}); output "
                        f"{'connected' if output_name else 'disconnected'}, channel 1")
            bindings.append(ControlBinding(
                "midimix", f"{label} {field}",
                f"Note {notes[index]}", f"{label} {field} toggle",
                cached_value(enabled) if track is not None else "-", availability,
                reason, index + 1, "false / true", "toggle; note-on velocity > 0", feedback,
            ))
    availability, reason = input_availability(input_name)
    bindings.append(ControlBinding(
        "midimix", "Master fader", f"CC {MASTER_FADER_CC}", "Master gain",
        cached_value(engine.master_gain) if hasattr(engine, "master_gain") else "(not cached)",
        availability, reason, range="0 .. 1", mode="absolute",
    ))
    return bindings


class MidiMixController:
    """Handle Akai MIDI Mix events and apply them to the vcpi core state."""

    def __init__(self, engine):
        self._engine = engine
        self._in_port = MidiInPort()
        self._out_port = MidiOutPort()
        self._cc_to_fader, self._cc_to_knob = build_cc_lookups()
        # track_index -> [(param_name, (min, max)), ...]
        self._param_cache: dict[int, list[tuple[str, tuple[float, float]]]] = {}
        self._param_generations: dict[int, int] = {}
        # Full metadata refreshes replace this mapping even within a generation;
        # ordinary value updates only change raw_value/string_value in place.
        self._param_metadata: dict[int, object] = {}
        self._led_lock = RLock()
        # Optimism is controller-local; engine snapshots only contain applied state.
        self._optimistic_state: dict[int, tuple[int, dict[str, bool]]] = {}

    def invalidate_param_cache(self, track_index: Optional[int] = None):
        """Clear cached parameter names/ranges and their metadata references."""
        if track_index is not None:
            self._param_cache.pop(track_index, None)
            self._param_generations.pop(track_index, None)
            self._param_metadata.pop(track_index, None)
        else:
            self._param_cache.clear()
            self._param_generations.clear()
            self._param_metadata.clear()

    def _build_param_cache(self, track_index: int, track=None) -> list[tuple[str, tuple[float, float]]]:
        """Cache the first three sound controls from one metadata snapshot."""
        if track is None:
            track = self._engine.tracks[track_index]
        if track is None or track.plugin is None:
            self.invalidate_param_cache(track_index)
            return []
        entries = [(name, parameter_bounds(parameter))
                   for name, parameter in _knob_parameters(track)]
        self._param_cache[track_index] = entries
        self._param_generations[track_index] = track.generation
        self._param_metadata[track_index] = track.plugin.parameters
        return entries

    def control_bindings(self) -> list[ControlBinding]:
        """Return expected mappings and cached values, never hardware state."""
        return midimix_bindings(self._engine, self.input_port_name, self.output_port_name)

    @property
    def input_port_name(self) -> Optional[str]:
        return self._in_port.name

    @property
    def output_port_name(self) -> Optional[str]:
        return self._out_port.name

    @property
    def port_name(self) -> Optional[str]:
        return self.input_port_name

    def open_input(self, port_index: int) -> str:
        return self._in_port.open_input_port(port_index, self.on_midi)

    def open_output(self, port_index: int) -> str:
        name = self._out_port.open_output_port(port_index)
        self.refresh_leds()
        return name

    def open_virtual_output(self, name: str = "vcpi-MIDI-Mix-LED") -> str:
        port_name = self._out_port.open_virtual_output_port(name)
        self.refresh_leds()
        return port_name

    def close_input(self):
        self._in_port.close()

    def close_output(self):
        self._out_port.close()

    def close(self):
        self.close_input()
        self.close_output()

    def _send_led_note(self, note: int, enabled: bool):
        if not self._out_port.is_open:
            return
        velocity = 127 if enabled else 0
        try:
            self._out_port.send([0x90, note, velocity])
        except Exception as exc:
            logger.warning("failed to send LED note=%d state=%s: %s", note, enabled, exc)

    def _set_track_leds(self, track_index: int):
        with self._led_lock:
            track = self._engine.tracks[track_index]
            mute_on = bool(track and track.muted)
            solo_on = bool(track and track.solo)
            optimistic = self._optimistic_state.get(track_index)
            if track is not None and optimistic is not None:
                generation, fields = optimistic
                if generation == track.generation:
                    mute_on = fields.get("muted", mute_on)
                    solo_on = fields.get("solo", solo_on)
            self._send_led_note(MUTE_NOTES[track_index], mute_on)
            self._send_led_note(SOLO_NOTES[track_index], solo_on)

    def refresh_leds(self, track_indices: Optional[list[int]] = None):
        """Reconcile immediate LED feedback with actual applied track state."""
        if track_indices is None:
            indices = range(len(FADER_CCS))
        else:
            indices = track_indices
        for idx in indices:
            if 0 <= idx < len(FADER_CCS):
                with self._led_lock:
                    self._optimistic_state.pop(idx, None)
                    self._set_track_leds(idx)

    def on_midi(self, event, data=None):
        """rtmidi callback for incoming MIDI Mix events."""
        del data

        raw, _dt = event
        if not raw:
            return

        status = raw[0]
        msg_type = status & 0xF0

        if msg_type == 0xB0 and len(raw) >= 3:
            logger.debug("CC %d value=%d", raw[1], raw[2])
            self._handle_cc(raw[1], raw[2])
        elif msg_type == 0x90 and len(raw) >= 3 and raw[2] > 0:
            logger.debug("note %d velocity=%d", raw[1], raw[2])
            self._handle_note(raw[1])
        else:
            logger.debug("raw=%s ignored", raw)

    def _handle_cc(self, cc: int, value: int):
        if cc == MASTER_FADER_CC:
            gain = value / 127.0
            self._engine.enqueue_master_gain(gain)
            logger.debug("master gain -> %.2f", gain)
            return

        track_idx = self._cc_to_fader.get(cc)
        if track_idx is not None:
            track = self._engine.tracks[track_idx]
            if track:
                gain = value / 127.0
                self._engine.enqueue_track_change(track_idx, generation=track.generation, gain=gain)
                logger.debug("track %d gain -> %.2f", track_idx + 1, gain)
            else:
                logger.debug("track %d gain ignored (empty track)", track_idx + 1)
            return

        knob = self._cc_to_knob.get(cc)
        if knob is None:
            logger.debug("unmapped CC %d ignored", cc)
            return

        track_idx, knob_idx = knob
        track = self._engine.tracks[track_idx]
        if track is None:
            logger.debug("knob on track %d ignored (empty track)", track_idx + 1)
            return

        # Metadata is plain cached state, safe to read on the rtmidi thread.
        params = self._param_cache.get(track_idx)
        metadata = track.plugin.parameters if track.plugin is not None else None
        if (params is None
                or self._param_generations.get(track_idx) != track.generation
                or self._param_metadata.get(track_idx) is not metadata):
            params = self._build_param_cache(track_idx, track)
        if not params:
            logger.debug("track %d knob ignored (no mapped params)", track_idx + 1)
            return
        if knob_idx >= len(params):
            logger.debug(
                "track %d knob %d ignored (no mapped param)",
                track_idx + 1,
                knob_idx + 1,
            )
            return

        param_name, (lo, hi) = params[knob_idx]
        parameter = track.plugin.parameters[param_name]
        if track.source_type == "deck" and param_name == "index":
            indexes = selectable_indexes(track.deck)
            if not indexes:
                return
            mapped = indexes[round(value / 127.0 * (len(indexes) - 1))]
        elif parameter.kind == "bool":
            mapped = value >= 64
        elif parameter.kind == "enum":
            if not parameter.valid_values:
                return
            mapped = parameter.valid_values[round(value / 127.0 * hi)]
        elif parameter.kind == "int":
            mapped = round(lo + (value / 127.0) * (hi - lo))
        else:
            mapped = lo + (value / 127.0) * (hi - lo)
        self._engine.enqueue_param_change(
            track_idx, param_name, mapped, generation=track.generation)
        logger.debug("track %d %s -> %s", track_idx + 1, param_name, mapped)

    def _handle_note(self, note: int):
        if note in MUTE_NOTES:
            idx = MUTE_NOTES.index(note)
            field = "muted"
            enabled_label, disabled_label = "MUTED", "unmuted"
        elif note in SOLO_NOTES:
            idx = SOLO_NOTES.index(note)
            field = "solo"
            enabled_label, disabled_label = "SOLO", "unsolo"
        else:
            logger.debug("unmapped note %d ignored", note)
            return

        with self._led_lock:
            track = self._engine.tracks[idx]
            if track is None:
                logger.debug("%s toggle ignored (track %d empty)", field, idx + 1)
                self._optimistic_state.pop(idx, None)
                self._set_track_leds(idx)
                return
            optimistic = self._optimistic_state.get(idx)
            fields = (
                dict(optimistic[1])
                if optimistic is not None and optimistic[0] == track.generation
                else {}
            )
            applied = track.muted if field == "muted" else track.solo
            enabled = not fields.get(field, applied)
            self._engine.enqueue_track_change(idx, generation=track.generation, **{field: enabled})
            fields[field] = enabled
            self._optimistic_state[idx] = (track.generation, fields)
            self._set_track_leds(idx)
            logger.info(
                "[track %d] %s: %s",
                idx + 1, track.name, enabled_label if enabled else disabled_label,
            )
