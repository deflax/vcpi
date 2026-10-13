"""Akai MIDI Mix controller integration."""

from __future__ import annotations

import logging
from threading import RLock
from typing import Optional

from core.midi import MidiInPort, MidiOutPort
from core.models import NUM_SLOTS


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
    """Return (cc_to_fader_slot, cc_to_knob_slot_and_index) lookup dicts."""
    cc_to_fader: dict[int, int] = {}
    cc_to_knob: dict[int, tuple[int, int]] = {}
    for slot_idx in range(NUM_SLOTS):
        cc_to_fader[FADER_CCS[slot_idx]] = slot_idx
        for knob_idx, cc in enumerate(KNOB_CCS[slot_idx]):
            cc_to_knob[cc] = (slot_idx, knob_idx)
    return cc_to_fader, cc_to_knob


class MidiMixController:
    """Handle Akai MIDI Mix events and apply them to the vcpi core state."""

    def __init__(self, engine):
        self._engine = engine
        self._in_port = MidiInPort()
        self._out_port = MidiOutPort()
        self._cc_to_fader, self._cc_to_knob = build_cc_lookups()
        # slot_index -> [(param_name, (min, max)), ...]
        self._param_cache: dict[int, list[tuple[str, tuple[float, float]]]] = {}
        self._param_generations: dict[int, int] = {}
        self._led_lock = RLock()
        # Optimism is controller-local; engine snapshots only contain applied state.
        self._optimistic_state: dict[int, tuple[int, dict[str, bool]]] = {}

    def invalidate_param_cache(self, slot_index: Optional[int] = None):
        """Clear cached parameter names/ranges. Call when instruments are loaded/removed."""
        if slot_index is not None:
            self._param_cache.pop(slot_index, None)
            self._param_generations.pop(slot_index, None)
        else:
            self._param_cache.clear()
            self._param_generations.clear()

    def _build_param_cache(self, slot_index: int, slot=None) -> list[tuple[str, tuple[float, float]]]:
        """Cache ranges from one captured instrument generation."""
        if slot is None:
            slot = self._engine.slots[slot_index]
        if slot is None or slot.plugin is None:
            return []
        entries: list[tuple[str, tuple[float, float]]] = []
        for name, parameter in slot.plugin.parameters.items():
            if parameter.kind == "bool":
                bounds = (0.0, 1.0)
            elif parameter.kind == "enum":
                bounds = (0.0, float(max(0, len(parameter.valid_values) - 1)))
            else:
                bounds = (float(parameter.range[0]), float(parameter.range[1]))
            entries.append((name, bounds))
        self._param_cache[slot_index] = entries
        self._param_generations[slot_index] = slot.generation
        return entries

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

    def _set_slot_leds(self, slot_index: int):
        with self._led_lock:
            slot = self._engine.slots[slot_index]
            mute_on = bool(slot and slot.muted)
            solo_on = bool(slot and slot.solo)
            optimistic = self._optimistic_state.get(slot_index)
            if slot is not None and optimistic is not None:
                generation, fields = optimistic
                if generation == slot.generation:
                    mute_on = fields.get("muted", mute_on)
                    solo_on = fields.get("solo", solo_on)
            self._send_led_note(MUTE_NOTES[slot_index], mute_on)
            self._send_led_note(SOLO_NOTES[slot_index], solo_on)

    def refresh_leds(self, slot_indices: Optional[list[int]] = None):
        """Reconcile immediate LED feedback with actual applied slot state."""
        if slot_indices is None:
            indices = range(NUM_SLOTS)
        else:
            indices = slot_indices
        for idx in indices:
            if 0 <= idx < NUM_SLOTS:
                with self._led_lock:
                    self._optimistic_state.pop(idx, None)
                    self._set_slot_leds(idx)

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
            logger.info("master gain -> %.2f", gain)
            return

        slot_idx = self._cc_to_fader.get(cc)
        if slot_idx is not None:
            slot = self._engine.slots[slot_idx]
            if slot:
                gain = value / 127.0
                self._engine.enqueue_slot_change(slot_idx, generation=slot.generation, gain=gain)
                logger.info("slot %d gain -> %.2f", slot_idx + 1, gain)
            else:
                logger.debug("slot %d gain ignored (empty slot)", slot_idx + 1)
            return

        knob = self._cc_to_knob.get(cc)
        if knob is None:
            logger.debug("unmapped CC %d ignored", cc)
            return

        slot_idx, knob_idx = knob
        slot = self._engine.slots[slot_idx]
        if slot is None:
            logger.debug("knob on slot %d ignored (empty slot)", slot_idx + 1)
            return

        # Metadata is plain cached state, safe to read on the rtmidi thread.
        params = self._param_cache.get(slot_idx)
        if params is None or self._param_generations.get(slot_idx) != slot.generation:
            params = self._build_param_cache(slot_idx, slot)
        if not params:
            logger.debug("slot %d knob ignored (no mapped params)", slot_idx + 1)
            return
        if knob_idx >= len(params):
            logger.debug(
                "slot %d knob %d ignored (no mapped param)",
                slot_idx + 1,
                knob_idx + 1,
            )
            return

        param_name, (lo, hi) = params[knob_idx]
        parameter = slot.plugin.parameters[param_name]
        if parameter.kind == "bool":
            mapped = value >= 64
        elif parameter.kind == "enum":
            if not parameter.valid_values:
                return
            mapped = parameter.valid_values[round(value / 127.0 * hi)]
        else:
            mapped = lo + (value / 127.0) * (hi - lo)
        self._engine.enqueue_param_change(
            slot_idx, param_name, mapped, generation=slot.generation)
        logger.debug("slot %d %s -> %s", slot_idx + 1, param_name, mapped)

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
            slot = self._engine.slots[idx]
            if slot is None:
                logger.debug("%s toggle ignored (slot %d empty)", field, idx + 1)
                self._optimistic_state.pop(idx, None)
                self._set_slot_leds(idx)
                return
            optimistic = self._optimistic_state.get(idx)
            fields = (
                dict(optimistic[1])
                if optimistic is not None and optimistic[0] == slot.generation
                else {}
            )
            applied = slot.muted if field == "muted" else slot.solo
            enabled = not fields.get(field, applied)
            self._engine.enqueue_slot_change(idx, generation=slot.generation, **{field: enabled})
            fields[field] = enabled
            self._optimistic_state[idx] = (slot.generation, fields)
            self._set_slot_leds(idx)
            logger.info(
                "[slot %d] %s: %s",
                idx + 1, slot.name, enabled_label if enabled else disabled_label,
            )
