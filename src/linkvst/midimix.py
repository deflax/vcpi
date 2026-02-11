"""Akai MIDI Mix CC / note mapping (factory defaults).

Each of the 8 channel strips has:
  - 1 fader   (volume)
  - 3 knobs   (high / mid / low)
  - 1 MUTE button   (note-on toggle)
  - 1 REC ARM button (repurposed as SOLO)

Plus a master fader (CC 62).
"""

from __future__ import annotations

from typing import Optional

from linkvst.models import NUM_SLOTS

# -- CC numbers per channel strip (1-8) ------------------------------------

FADER_CCS = [19, 23, 27, 31, 49, 53, 57, 61]

KNOB_CCS = [
    # (high, mid, low) per strip
    (16, 17, 18),  # ch 1
    (20, 21, 22),  # ch 2
    (24, 25, 26),  # ch 3
    (28, 29, 30),  # ch 4
    (46, 47, 48),  # ch 5
    (50, 51, 52),  # ch 6
    (54, 55, 56),  # ch 7
    (58, 59, 60),  # ch 8
]

MUTE_NOTES = [1, 4, 7, 10, 13, 16, 19, 22]
SOLO_NOTES = [3, 6, 9, 12, 15, 18, 21, 24]  # "rec arm" repurposed

MASTER_FADER_CC = 62


# -- Reverse lookup tables -------------------------------------------------

def build_cc_lookups() -> tuple[dict[int, int], dict[int, tuple[int, int]]]:
    """Return (cc_to_fader_slot, cc_to_knob_slot_and_index) dicts."""
    cc_to_fader: dict[int, int] = {}
    cc_to_knob: dict[int, tuple[int, int]] = {}
    for slot_idx in range(NUM_SLOTS):
        cc_to_fader[FADER_CCS[slot_idx]] = slot_idx
        for knob_idx, cc in enumerate(KNOB_CCS[slot_idx]):
            cc_to_knob[cc] = (slot_idx, knob_idx)
    return cc_to_fader, cc_to_knob


class MidiMixHandler:
    """Interprets raw MIDI from the Akai MIDI Mix and mutates engine state."""

    def __init__(self, engine):
        """*engine* is an AudioEngine instance whose slots/master_gain we control."""
        self._engine = engine
        self._cc_to_fader, self._cc_to_knob = build_cc_lookups()

    def on_midi(self, event, data=None):
        """rtmidi callback – wire this to MidiPort.open()."""
        raw, _dt = event
        if not raw:
            return
        status = raw[0]
        msg_type = status & 0xF0

        if msg_type == 0xB0 and len(raw) >= 3:
            self._handle_cc(raw[1], raw[2])
        elif msg_type == 0x90 and len(raw) >= 3 and raw[2] > 0:
            self._handle_note(raw[1])

    # -- CC handling ---------------------------------------------------------

    def _handle_cc(self, cc: int, value: int):
        # Master fader
        if cc == MASTER_FADER_CC:
            self._engine.master_gain = value / 127.0
            return

        # Per-channel fader -> slot gain
        slot_idx = self._cc_to_fader.get(cc)
        if slot_idx is not None:
            slot = self._engine.slots[slot_idx]
            if slot:
                slot.gain = value / 127.0
            return

        # Per-channel knob -> first 3 instrument parameters
        knob = self._cc_to_knob.get(cc)
        if knob is not None:
            slot_idx, knob_idx = knob
            slot = self._engine.slots[slot_idx]
            if slot is None:
                return
            params = list(slot.plugin.parameters.keys())
            if knob_idx < len(params):
                pname = params[knob_idx]
                try:
                    prange = slot.plugin.parameters[pname].range
                    mapped = prange[0] + (value / 127.0) * (prange[1] - prange[0])
                    setattr(slot.plugin, pname, mapped)
                except Exception:
                    try:
                        setattr(slot.plugin, pname, value / 127.0)
                    except Exception:
                        pass

    # -- Note handling (mute / solo toggles) ---------------------------------

    def _handle_note(self, note: int):
        if note in MUTE_NOTES:
            idx = MUTE_NOTES.index(note)
            slot = self._engine.slots[idx]
            if slot:
                slot.muted = not slot.muted
                state = "MUTED" if slot.muted else "unmuted"
                print(f"  [slot {idx + 1}] {slot.name}: {state}")
            return

        if note in SOLO_NOTES:
            idx = SOLO_NOTES.index(note)
            slot = self._engine.slots[idx]
            if slot:
                slot.solo = not slot.solo
                state = "SOLO" if slot.solo else "unsolo"
                print(f"  [slot {idx + 1}] {slot.name}: {state}")
