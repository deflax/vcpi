"""VSTHost - the central coordinator tying all subsystems together."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Optional

from linkvst.deps import HAS_PEDALBOARD, HAS_MIDO, load_plugin, mido
from linkvst.models import InstrumentSlot, NUM_SLOTS
from linkvst.engine import AudioEngine
from linkvst.midi import MidiPort
from linkvst.midimix import MidiMixHandler
from linkvst.link import LinkSync
from linkvst import session


class VSTHost:
    def __init__(self, sample_rate: int = 44100, buffer_size: int = 512,
                 session_path: Optional[str] = None):
        self.sample_rate = sample_rate
        self.buffer_size = buffer_size
        self.session_path = Path(session_path) if session_path else session.DEFAULT_SESSION_PATH

        self.engine = AudioEngine(sample_rate, buffer_size)
        self.midi_seq = MidiPort()    # Beatstep Pro
        self.midi_mix = MidiPort()    # Akai MIDI Mix
        self.mix_handler = MidiMixHandler(self.engine)
        self.link = LinkSync()

        # MIDI channel -> slot index  (Beatstep Pro routing)
        self._channel_map: dict[int, int] = {}

    # -- plugin management ---------------------------------------------------

    def load_instrument(self, slot_index: int, path: str,
                        name: Optional[str] = None) -> InstrumentSlot:
        if not HAS_PEDALBOARD:
            raise RuntimeError("pedalboard not installed")
        if not 0 <= slot_index < NUM_SLOTS:
            raise ValueError(f"slot must be 1-{NUM_SLOTS}")
        plugin = load_plugin(path)
        if not plugin.is_instrument:
            raise ValueError(f"{path} is not an instrument")
        slot = InstrumentSlot(
            name=name or Path(path).stem,
            path=path,
            plugin=plugin,
        )
        self.engine.slots[slot_index] = slot
        return slot

    def load_effect(self, path: str, slot_index: Optional[int] = None,
                    name: Optional[str] = None):
        """Load effect into a slot's insert chain or the master bus."""
        if not HAS_PEDALBOARD:
            raise RuntimeError("pedalboard not installed")
        plugin = load_plugin(path)
        label = name or Path(path).stem
        if slot_index is not None:
            slot = self.engine.slots[slot_index]
            if slot is None:
                raise ValueError(f"Slot {slot_index + 1} is empty")
            slot.effects.append(plugin)
            print(f"[FX] '{label}' -> slot {slot_index + 1} ({slot.name})")
        else:
            self.engine.master_effects.append(plugin)
            print(f"[FX] '{label}' -> master bus")

    def remove_effect(self, slot_index: Optional[int], effect_index: int):
        if slot_index is not None:
            slot = self.engine.slots[slot_index]
            if slot is None:
                raise ValueError(f"Slot {slot_index + 1} is empty")
            del slot.effects[effect_index]
        else:
            del self.engine.master_effects[effect_index]

    # -- routing -------------------------------------------------------------

    def route(self, midi_channel: int, slot_index: int):
        self._channel_map[midi_channel] = slot_index
        slot = self.engine.slots[slot_index]
        if slot:
            slot.midi_channels.add(midi_channel)

    def unroute(self, midi_channel: int):
        idx = self._channel_map.pop(midi_channel, None)
        if idx is not None:
            slot = self.engine.slots[idx]
            if slot:
                slot.midi_channels.discard(midi_channel)

    # -- Beatstep Pro MIDI ---------------------------------------------------

    def open_sequencer_midi(self, port_index: Optional[int] = None):
        if port_index is not None:
            name = self.midi_seq.open(port_index, self._on_seq_midi)
        else:
            name = self.midi_seq.open_virtual("LinkVST-Seq", self._on_seq_midi)
        print(f"[SEQ MIDI] Opened: {name}")

    def _on_seq_midi(self, event, data=None):
        """Route Beatstep Pro MIDI to the appropriate instrument slot."""
        raw, _dt = event
        if not raw or not HAS_MIDO:
            return
        status = raw[0]
        channel = status & 0x0F
        msg_type = status & 0xF0

        idx = self._channel_map.get(channel)
        if idx is None:
            return

        try:
            m = None
            if msg_type == 0x90 and len(raw) >= 3:
                note, vel = raw[1], raw[2]
                m = (mido.Message("note_off", note=note, channel=channel)
                     if vel == 0 else
                     mido.Message("note_on", note=note, velocity=vel,
                                  channel=channel))
            elif msg_type == 0x80 and len(raw) >= 3:
                m = mido.Message("note_off", note=raw[1], channel=channel)
            elif msg_type == 0xB0 and len(raw) >= 3:
                m = mido.Message("control_change", control=raw[1],
                                 value=raw[2], channel=channel)
            elif msg_type == 0xE0 and len(raw) >= 3:
                val = (raw[2] << 7) | raw[1]
                m = mido.Message("pitchwheel", pitch=val - 8192,
                                 channel=channel)
            elif msg_type == 0xD0 and len(raw) >= 2:
                m = mido.Message("aftertouch", value=raw[1], channel=channel)
            if m is not None:
                self.engine.enqueue_midi(idx, m)
        except Exception as exc:
            print(f"[SEQ MIDI] {exc}")

    # -- Akai MIDI Mix -------------------------------------------------------

    def open_mixer_midi(self, port_index: int):
        name = self.midi_mix.open(port_index, self.mix_handler.on_midi)
        print(f"[MIDI Mix] Opened: {name}")

    # -- convenience ---------------------------------------------------------

    def send_note(self, slot_index: int, note: int, velocity: int = 100,
                  duration: float = 0.3):
        if not HAS_MIDO:
            return
        on = mido.Message("note_on", note=note, velocity=velocity)
        off = mido.Message("note_off", note=note)
        self.engine.enqueue_midi(slot_index, on)
        threading.Timer(duration, self.engine.enqueue_midi,
                        args=(slot_index, off)).start()

    # -- audio / link --------------------------------------------------------

    def start_audio(self, output_device=None):
        self.engine.start(output_device)

    def stop_audio(self):
        self.engine.stop()

    def start_link(self, bpm: Optional[float] = None):
        if bpm is not None:
            self.link.bpm = bpm
        self.link.enable()
        print(f"[Link] Enabled at {self.link.bpm:.1f} BPM")

    def stop_link(self):
        self.link.disable()
        print("[Link] Disabled")

    # -- session persistence -------------------------------------------------

    def save_session(self, path: Optional[str] = None):
        """Save current state to a JSON session file."""
        p = Path(path) if path else self.session_path
        session.save(self, p)

    def restore_session(self, path: Optional[str] = None):
        """Restore state from a JSON session file."""
        p = Path(path) if path else self.session_path
        session.restore(self, p)

    # -- shutdown ------------------------------------------------------------

    def shutdown(self):
        self.save_session()
        self.stop_audio()
        self.midi_seq.close()
        self.midi_mix.close()
        self.stop_link()
        print("[Host] Shutdown complete")
