"""vcpi core - the central coordinator for all subsystems."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Optional

from core import deps, session
from controllers.bsp import BeatStepProController
from controllers.midimix import MidiMixController
from core.engine import AudioEngine
from core.link import LinkSync
from core.models import InstrumentSlot, NUM_SLOTS


class VcpiCore:
    def __init__(self, sample_rate: int = 44100, buffer_size: int = 512,
                 session_path: Optional[str] = None):
        self.sample_rate = sample_rate
        self.buffer_size = buffer_size
        self.session_path = Path(session_path) if session_path else session.DEFAULT_SESSION_PATH

        self.engine = AudioEngine(sample_rate, buffer_size)
        self.link = LinkSync()

        self.bsp = BeatStepProController(self.engine)
        self.midimix = MidiMixController(self.engine)

    @property
    def channel_map(self) -> dict[int, int]:
        return self.bsp.channel_map

    @property
    def sequencer_midi_name(self) -> Optional[str]:
        return self.bsp.port_name

    @property
    def mixer_midi_name(self) -> Optional[str]:
        return self.midimix.port_name

    # -- plugin management ---------------------------------------------------

    def load_instrument(self, slot_index: int, path: str,
                        name: Optional[str] = None) -> InstrumentSlot:
        if not deps.HAS_PEDALBOARD:
            raise RuntimeError("pedalboard not installed")
        if deps.load_plugin is None:
            raise RuntimeError("pedalboard loader unavailable")
        if not 0 <= slot_index < NUM_SLOTS:
            raise ValueError(f"slot must be 1-{NUM_SLOTS}")
        plugin = deps.load_plugin(path)
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
        if not deps.HAS_PEDALBOARD:
            raise RuntimeError("pedalboard not installed")
        if deps.load_plugin is None:
            raise RuntimeError("pedalboard loader unavailable")
        plugin = deps.load_plugin(path)
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
        self.bsp.route(midi_channel, slot_index)

    def unroute(self, midi_channel: int):
        self.bsp.unroute(midi_channel)

    # -- MIDI controllers ----------------------------------------------------

    def open_sequencer_midi(self, port_index: Optional[int] = None):
        name = self.bsp.open(port_index)
        print(f"[SEQ MIDI] Opened: {name}")

    def open_mixer_midi(self, port_index: int):
        name = self.midimix.open(port_index)
        print(f"[MIDI Mix] Opened: {name}")

    # -- convenience ---------------------------------------------------------

    def send_note(self, slot_index: int, note: int, velocity: int = 100,
                  duration: float = 0.3):
        if not deps.HAS_MIDO or deps.mido is None:
            return
        on = deps.mido.Message("note_on", note=note, velocity=velocity)
        off = deps.mido.Message("note_off", note=note)
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
        self.bsp.close()
        self.midimix.close()
        self.stop_link()
        print("[Host] Shutdown complete")
