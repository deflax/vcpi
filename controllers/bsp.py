"""BeatStep Pro MIDI controller integration."""

from __future__ import annotations

import logging
from typing import Optional

from core.deps import HAS_MIDO, mido
from core.midi import MidiPort
from core.models import NUM_SLOTS


logger = logging.getLogger(__name__)


class BeatStepProController:
    """Handle BeatStep Pro MIDI input and channel-to-slot routing."""

    def __init__(self, engine):
        self._engine = engine
        self._port = MidiPort()
        # MIDI channel (0-15) -> slot index (0-7)
        self._channel_map: dict[int, int] = {}

    @property
    def channel_map(self) -> dict[int, int]:
        return self._channel_map

    @property
    def port_name(self) -> Optional[str]:
        return self._port.name

    def open(self, port_index: Optional[int] = None) -> str:
        if port_index is not None:
            return self._port.open(port_index, self.on_midi)
        return self._port.open_virtual("vcpi-Seq", self.on_midi)

    def close(self):
        self._port.close()

    def route(self, midi_channel: int, slot_index: int):
        if not 0 <= midi_channel < 16:
            raise ValueError("MIDI channel must be 1-16")
        if not 0 <= slot_index < NUM_SLOTS:
            raise ValueError(f"slot must be 1-{NUM_SLOTS}")

        prev_idx = self._channel_map.get(midi_channel)
        if prev_idx is not None and prev_idx != slot_index:
            prev_slot = self._engine.slots[prev_idx]
            if prev_slot:
                prev_slot.midi_channels.discard(midi_channel)
            logger.info(
                "route update: ch %d slot %d -> slot %d",
                midi_channel + 1,
                prev_idx + 1,
                slot_index + 1,
            )

        self._channel_map[midi_channel] = slot_index
        slot = self._engine.slots[slot_index]
        if slot:
            slot.midi_channels.add(midi_channel)

        if prev_idx is None:
            logger.info("route set: ch %d -> slot %d", midi_channel + 1, slot_index + 1)

    def unroute(self, midi_channel: int):
        idx = self._channel_map.pop(midi_channel, None)
        if idx is not None:
            slot = self._engine.slots[idx]
            if slot:
                slot.midi_channels.discard(midi_channel)
            logger.info("route removed: ch %d (slot %d)", midi_channel + 1, idx + 1)

    def on_midi(self, event, data=None):
        """rtmidi callback that forwards MIDI to routed instrument slots."""
        del data

        raw, _dt = event
        if not raw or not HAS_MIDO:
            return

        status = raw[0]
        channel = status & 0x0F
        msg_type = status & 0xF0

        slot_index = self._channel_map.get(channel)
        if slot_index is None:
            logger.debug("ch %d raw=%s dropped (unrouted)", channel + 1, raw)
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
                self._engine.enqueue_midi(slot_index, msg)
                logger.debug("ch %d -> slot %d: %s", channel + 1, slot_index + 1, msg)
            else:
                logger.debug("ch %d raw=%s ignored", channel + 1, raw)
        except Exception as exc:
            logger.warning("MIDI processing error: %s", exc)
