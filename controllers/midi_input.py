"""Generic MIDI input controller.

Any MIDI input device (keyboard, sequencer, etc.) is handled identically:
parse incoming raw MIDI, look up engine.channel_map, and enqueue to the
appropriate instrument track.
"""

from __future__ import annotations

import logging
from threading import RLock
from typing import Optional

from core.deps import HAS_MIDO, mido
from core.midi import MidiInPort


logger = logging.getLogger(__name__)

# Routed generic-input bindings only. MIDI Mix mute/solo buttons stay untouched.
# Note-on velocity zero is a release, just like note-off.
DECK_NOTE_BINDINGS = {
    60: "play_toggle", 61: "cue", 62: "cue_set", 63: "nudge_down", 64: "nudge_up",
}
DECK_PITCH_CC = 20
DECK_SELECT_CC = 21


class MidiInputController:
    """Forward MIDI input from any device into routed instrument tracks."""

    def __init__(self, engine, label: str = "MIDI-in"):
        self._engine = engine
        self._port = MidiInPort()
        self.label = label  # human-readable name for logging / status
        self._deck_holds: dict[tuple[int, int, int], set[int]] = {}
        self._deck_lock = RLock()

    @property
    def port_name(self) -> Optional[str]:
        return self._port.name

    def open(self, port_index: int) -> str:
        """Open a hardware MIDI input port by index."""
        name = self._port.open_input_port(port_index, self.on_midi)
        self.label = name
        return name

    def close(self):
        self._port.close()
        self.clear_deck_holds()

    def clear_deck_holds(self, track_index=None, channel=None):
        """Release momentary controls before disconnect, reroute or replacement."""
        with self._deck_lock:
            self._clear_deck_holds(track_index, channel)

    def _clear_deck_holds(self, track_index, channel):
        for key in list(self._deck_holds):
            held_channel, index, generation = key
            if track_index is not None and index != track_index:
                continue
            if channel is not None and held_channel != channel:
                continue
            held = self._deck_holds.pop(key)
            if held.intersection((63, 64)):
                self._engine.enqueue_param_change(
                    index, "nudge", self._deck_nudge(index, generation), generation=generation)

    def _deck_midi(self, track_index, track, msg):
        """Interpret transport locally; unrelated source MIDI remains unchanged."""
        with self._deck_lock:
            self._handle_deck_midi(track_index, track, msg)

    def _deck_nudge(self, index, generation):
        notes = set()
        for (_, held_index, held_generation), held in self._deck_holds.items():
            if (held_index, held_generation) == (index, generation):
                notes.update(held)
        return float((64 in notes) - (63 in notes))

    def _handle_deck_midi(self, track_index, track, msg):
        generation = track.generation
        key = (msg.channel, track_index, generation)
        # Drop holds belonging to a replaced source; never send its release into
        # the replacement. Host lifecycle hooks clear live holds before routing.
        for old in list(self._deck_holds):
            if old[1] == track_index and old[2] != generation:
                self._deck_holds.pop(old)
        if msg.type in ("note_on", "note_off") and msg.note in DECK_NOTE_BINDINGS:
            held = self._deck_holds.setdefault(key, set())
            pressed = msg.type == "note_on" and msg.velocity > 0
            was_pressed = msg.note in held
            if pressed:
                held.add(msg.note)
            else:
                held.discard(msg.note)
            if msg.note in (63, 64):
                if pressed != was_pressed:
                    self._engine.enqueue_param_change(
                        track_index, "nudge", self._deck_nudge(track_index, generation),
                        generation=generation)
            elif pressed and not was_pressed:
                self._engine.enqueue_deck_command(
                    track_index, DECK_NOTE_BINDINGS[msg.note], generation=generation)
            if not held:
                self._deck_holds.pop(key, None)
        elif msg.type == "control_change":
            if msg.control == DECK_PITCH_CC:
                self._engine.enqueue_param_change(
                    track_index, "pitch", -16.0 + 32.0 * msg.value / 127.0,
                    generation=generation)
            elif msg.control == DECK_SELECT_CC:
                count = len(track.deck.get("tracks", []))
                if count:
                    selection = 1 + (msg.value * (count - 1) + 63) // 127
                    self._engine.enqueue_param_change(
                        track_index, "track_select", selection, generation=generation)

    def on_midi(self, event, data=None):
        """rtmidi callback forwarding MIDI into routed tracks."""
        del data

        raw, _dt = event
        if not raw or not HAS_MIDO:
            return

        status = raw[0]
        channel = status & 0x0F
        msg_type = status & 0xF0

        track_index = self._engine.channel_map.get(channel)
        if track_index is None:
            logger.debug("[%s] ch %d raw=%s dropped (unrouted)",
                         self.label, channel + 1, raw)
            return

        try:
            msg = None
            if msg_type == 0x90 and len(raw) >= 3:
                note, velocity = raw[1], raw[2]
                if velocity == 0:
                    msg = mido.Message("note_off", note=note, channel=channel)
                else:
                    msg = mido.Message("note_on", note=note, velocity=velocity,
                                       channel=channel)
            elif msg_type == 0x80 and len(raw) >= 3:
                msg = mido.Message("note_off", note=raw[1], channel=channel)
            elif msg_type == 0xB0 and len(raw) >= 3:
                msg = mido.Message("control_change", control=raw[1],
                                   value=raw[2], channel=channel)
            elif msg_type == 0xE0 and len(raw) >= 3:
                value = (raw[2] << 7) | raw[1]
                msg = mido.Message("pitchwheel", pitch=value - 8192,
                                   channel=channel)
            elif msg_type == 0xD0 and len(raw) >= 2:
                msg = mido.Message("aftertouch", value=raw[1], channel=channel)

            if msg is not None:
                track = self._engine.tracks[track_index]
                if track is not None and track.source_type == "deck":
                    self._deck_midi(track_index, track, msg)
                else:
                    self._engine.enqueue_midi(track_index, msg)
                logger.debug("[%s] ch %d -> track %d: %s",
                             self.label, channel + 1, track_index + 1, msg)
            else:
                logger.debug("[%s] ch %d raw=%s ignored",
                             self.label, channel + 1, raw)
        except Exception as exc:
            logger.warning("[%s] MIDI processing error: %s", self.label, exc)
