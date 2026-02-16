"""Akai MIDI Mix controller integration."""

from __future__ import annotations

import logging
from typing import Optional

from core.midi import MidiPort
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
        self._port = MidiPort()
        self._cc_to_fader, self._cc_to_knob = build_cc_lookups()

    @property
    def port_name(self) -> Optional[str]:
        return self._port.name

    def open(self, port_index: int) -> str:
        return self._port.open(port_index, self.on_midi)

    def close(self):
        self._port.close()

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
            self._engine.master_gain = value / 127.0
            logger.info("master gain -> %.2f", self._engine.master_gain)
            return

        slot_idx = self._cc_to_fader.get(cc)
        if slot_idx is not None:
            slot = self._engine.slots[slot_idx]
            if slot:
                slot.gain = value / 127.0
                logger.info("slot %d gain -> %.2f", slot_idx + 1, slot.gain)
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

        params = list(slot.plugin.parameters.keys())
        if knob_idx >= len(params):
            logger.debug(
                "slot %d knob %d ignored (no mapped param)",
                slot_idx + 1,
                knob_idx + 1,
            )
            return

        param_name = params[knob_idx]
        try:
            param_range = slot.plugin.parameters[param_name].range
            mapped = param_range[0] + (value / 127.0) * (param_range[1] - param_range[0])
            setattr(slot.plugin, param_name, mapped)
            logger.info("slot %d %s -> %s", slot_idx + 1, param_name, mapped)
        except Exception:
            try:
                setattr(slot.plugin, param_name, value / 127.0)
                logger.info("slot %d %s -> %s", slot_idx + 1, param_name, value / 127.0)
            except Exception:
                logger.warning("slot %d %s update failed", slot_idx + 1, param_name)

    def _handle_note(self, note: int):
        if note in MUTE_NOTES:
            idx = MUTE_NOTES.index(note)
            slot = self._engine.slots[idx]
            if slot:
                slot.muted = not slot.muted
                state = "MUTED" if slot.muted else "unmuted"
                logger.info("[slot %d] %s: %s", idx + 1, slot.name, state)
            else:
                logger.debug("mute toggle ignored (slot %d empty)", idx + 1)
            return

        if note in SOLO_NOTES:
            idx = SOLO_NOTES.index(note)
            slot = self._engine.slots[idx]
            if slot:
                slot.solo = not slot.solo
                state = "SOLO" if slot.solo else "unsolo"
                logger.info("[slot %d] %s: %s", idx + 1, slot.name, state)
            else:
                logger.debug("solo toggle ignored (slot %d empty)", idx + 1)
            return

        logger.debug("unmapped note %d ignored", note)
