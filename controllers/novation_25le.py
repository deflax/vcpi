"""Novation 25 LE MIDI keyboard integration."""

from __future__ import annotations

import logging
from typing import Optional

from core.deps import HAS_MIDO, mido
from core.midi import MidiInPort


logger = logging.getLogger(__name__)


class Novation25LeController:
    """Handle Novation 25 LE MIDI input using shared channel routing."""

    def __init__(self, engine, channel_map: dict[int, int]):
        self._engine = engine
        self._channel_map = channel_map
        self._port = MidiInPort()

    @property
    def port_name(self) -> Optional[str]:
        return self._port.name

    def open(self, port_index: int) -> str:
        return self._port.open_input_port(port_index, self.on_midi)

    def close(self):
        self._port.close()

    def on_midi(self, event, data=None):
        """rtmidi callback forwarding keyboard MIDI into routed slots."""
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
            logger.warning("keyboard MIDI processing error: %s", exc)
