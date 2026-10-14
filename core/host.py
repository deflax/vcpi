"""vcpi core - the central coordinator for all subsystems."""

from __future__ import annotations

import logging
import importlib
import os
import re
import threading
from pathlib import Path
from typing import Optional

from core import deps
from controllers.akai_midimix import MidiMixController
from controllers.midi_input import MidiInputController
from core.audio_process import AudioProcessError, RemoteAudioEngine
from core.link import LinkSync
from core.midi import list_midi_input_ports, list_midi_output_ports
from core.models import InstrumentSlot, NUM_SLOTS


logger = logging.getLogger(__name__)

PATCHES_DIR_ENV = "VCPI_PATCHES_DIR"
DEFAULT_PATCHES_DIR = "patches"


class VcpiCore:
    def __init__(self, sample_rate: int = 44100, buffer_size: int = 512,
                 session_path: Optional[str] = None):
        self.sample_rate = sample_rate
        self.buffer_size = buffer_size
        session_module = importlib.import_module("core.session")
        self.session_path = Path(session_path) if session_path else session_module.DEFAULT_SESSION_PATH
        self.loaded_session_name: Optional[str] = None
        self.loaded_session_path: Optional[Path] = None
        self.patches_dir = Path(
            os.environ.get(PATCHES_DIR_ENV, DEFAULT_PATCHES_DIR)
        ).expanduser()
        self.midi_inputs: list[MidiInputController] = []
        self._shutdown_lock = threading.Lock()
        self._shutdown = False
        self._note_lock = threading.Lock()
        self._note_timers: dict[threading.Timer, tuple[int, object, int]] = {}
        self.engine = RemoteAudioEngine(sample_rate, buffer_size)
        try:
            self.link = LinkSync()
            self.midimix = MidiMixController(self.engine)
            self.engine.add_state_listener(self.midimix.refresh_leds)
            sequencer_module = importlib.import_module("core.sequencer")
            self.sequencer = sequencer_module.Sequencer(self)
        except BaseException:
            self._shutdown = True
            self._close_resources()
            raise

    @property
    def channel_map(self) -> dict[int, int]:
        return self.engine.channel_map

    @property
    def midi_input_names(self) -> list[str]:
        """Port names of all open MIDI inputs."""
        return [c.port_name for c in self.midi_inputs if c.port_name]

    @property
    def mixer_midi_name(self) -> Optional[str]:
        return self.midimix.input_port_name

    @property
    def mixer_midi_out_name(self) -> Optional[str]:
        return self.midimix.output_port_name

    @property
    def audio_output_name(self) -> Optional[str]:
        """Most recently selected output name, from the audio child's cache."""
        return self.engine.audio_output_name

    @staticmethod
    def _strip_alsa_suffix(name: str) -> str:
        """Strip ALSA numeric port suffixes that change across reboots.

        Examples:
            "ReMOTE LE:ReMOTE LE 24:0"  -> "ReMOTE LE:ReMOTE LE"
            "MIDI Mix:MIDI Mix MIDI 1 20:0" -> "MIDI Mix:MIDI Mix MIDI 1"
            "IQaudIODAC: DAC HiFi pcm512x-hifi-0 (hw:0,0)" ->
                "IQaudIODAC: DAC HiFi pcm512x-hifi-0"
        """
        # Remove trailing " (hw:X,Y)" or "(hw:X,Y,Z)" etc.
        s = re.sub(r"\s*\(hw:\d+(?:,\d+)*\)\s*$", "", name)
        # Remove trailing " N:M" (ALSA client:port numbers)
        s = re.sub(r"\s+\d+:\d+\s*$", "", s)
        return s.strip()

    @staticmethod
    def _resolve_port_index_by_name(port_name: str, ports: list[str], kind: str) -> int:
        """Resolve a saved port name to a current index.

        Matching priority:
          1. Exact match
          2. Case-insensitive match
          3. Match after stripping ALSA numeric suffixes
          4. Substring containment (unique match only)
        """
        # 1) exact
        if port_name in ports:
            return ports.index(port_name)

        # 2) case-insensitive
        lower = port_name.lower()
        for i, candidate in enumerate(ports):
            if candidate.lower() == lower:
                return i

        # 3) stripped ALSA suffixes
        stripped_target = VcpiCore._strip_alsa_suffix(port_name).lower()
        for i, candidate in enumerate(ports):
            stripped_candidate = VcpiCore._strip_alsa_suffix(candidate).lower()
            if stripped_target and stripped_candidate == stripped_target:
                return i

        # 4) substring containment -- only if exactly one match
        if stripped_target:
            matches = [
                i for i, candidate in enumerate(ports)
                if stripped_target in VcpiCore._strip_alsa_suffix(candidate).lower()
                or VcpiCore._strip_alsa_suffix(candidate).lower() in stripped_target
            ]
            if len(matches) == 1:
                return matches[0]

        raise ValueError(f"{kind} port '{port_name}' not found")

    def _resolve_midi_input_port(self, port: int | str) -> int:
        if isinstance(port, int):
            return port

        token = port.strip()
        if token.isdigit():
            return int(token)

        ports = list_midi_input_ports()
        return self._resolve_port_index_by_name(token, ports, "MIDI input")

    def _resolve_midi_output_port(self, port: int | str) -> int:
        if isinstance(port, int):
            return port

        token = port.strip()
        if token.isdigit():
            return int(token)

        ports = list_midi_output_ports()
        return self._resolve_port_index_by_name(token, ports, "MIDI output")

    # -- plugin management ---------------------------------------------------

    def _loaded_slot(self, slot_index: int) -> InstrumentSlot:
        slot = self.engine.slots[slot_index]
        if slot is None:
            raise RuntimeError(f"Audio child did not publish slot {slot_index + 1}")
        self.midimix.invalidate_param_cache(slot_index)
        self.midimix._build_param_cache(slot_index)
        return slot

    def load_instrument(self, slot_index: int, path: str,
                        name: Optional[str] = None) -> InstrumentSlot:
        """Stage a native slot process and return its published cached state."""
        self.engine.request("load_instrument", slot_index=slot_index, path=path, name=name)
        return self._loaded_slot(slot_index)

    def load_wav(self, slot_index: int, wav_path: str,
                 name: Optional[str] = None) -> InstrumentSlot:
        """Stage a sampler slot process and return its published cached state."""
        self.engine.request("load_wav", slot_index=slot_index, wav_path=wav_path, name=name)
        return self._loaded_slot(slot_index)

    def remove_instrument(self, slot_index: int) -> InstrumentSlot:
        """Remove an instrument, retaining the previous snapshot for callers."""
        if not 0 <= slot_index < NUM_SLOTS:
            raise ValueError(f"slot must be 1-{NUM_SLOTS}")
        slot = self.engine.slots[slot_index]
        if slot is None:
            raise ValueError(f"Slot {slot_index + 1} is already empty")
        self.engine.request("remove_instrument", slot_index=slot_index)
        self.midimix.invalidate_param_cache(slot_index)
        return slot

    def load_effect(self, path: str, slot_index: Optional[int] = None,
                    name: Optional[str] = None):
        self.engine.request("load_effect", path=path, slot_index=slot_index, name=name)
        if slot_index is not None:
            self.midimix.invalidate_param_cache(slot_index)

    def remove_effect(self, slot_index: Optional[int], effect_index: int):
        self.engine.request("remove_effect", slot_index=slot_index, effect_index=effect_index)
        if slot_index is not None:
            self.midimix.invalidate_param_cache(slot_index)

    def load_vcv(self, slot_index: int, vcv_path: str,
                 cardinal_path: Optional[str] = None,
                 name: Optional[str] = None) -> tuple[InstrumentSlot, str, str]:
        result = self.engine.request(
            "load_vcv", slot_index=slot_index, vcv_path=vcv_path,
            cardinal_path=cardinal_path, name=name,
        )
        return self._loaded_slot(slot_index), result["patch_result"], result["cardinal_path"]

    def load_vcv_patch(self, slot_index: int, patch_name: str,
                       cardinal_path: Optional[str] = None,
                       name: Optional[str] = None) -> tuple[InstrumentSlot, str, str, Path]:
        result = self.engine.request(
            "load_vcv_patch", slot_index=slot_index, patch_name=patch_name,
            cardinal_path=cardinal_path, name=name,
        )
        return (
            self._loaded_slot(slot_index), result["patch_result"],
            result["cardinal_path"], Path(result["patch_path"]),
        )

    # -- routing -------------------------------------------------------------

    def route(self, midi_channel: int, slot_index: int):
        self.engine.route(midi_channel, slot_index)

    def unroute(self, midi_channel: int):
        self.engine.unroute(midi_channel)

    # -- MIDI controllers ----------------------------------------------------

    def open_midi_input(self, port_index: int | str) -> MidiInputController:
        """Open any MIDI input port and add it to the active inputs list."""
        port_index = self._resolve_midi_input_port(port_index)
        ctrl = MidiInputController(self.engine)
        name = ctrl.open(port_index)
        self.midi_inputs.append(ctrl)
        logger.info("[MIDI IN] Opened: %s", name)
        return ctrl

    def close_midi_input(self, index: int):
        """Close and remove a MIDI input by its position in midi_inputs."""
        if not 0 <= index < len(self.midi_inputs):
            raise ValueError(
                f"MIDI input index must be 1-{len(self.midi_inputs)}")
        ctrl = self.midi_inputs.pop(index)
        ctrl.close()
        logger.info("[MIDI IN] Closed: %s", ctrl.label)

    def open_mixer_midi(self, port_index: int | str):
        port_index = self._resolve_midi_input_port(port_index)
        name = self.midimix.open_input(port_index)
        logger.info("[MIDI Mix IN] Opened: %s", name)

    def open_mixer_midi_out(self, port_index: int | str):
        port_index = self._resolve_midi_output_port(port_index)
        name = self.midimix.open_output(port_index)
        logger.info("[MIDI Mix OUT] Opened: %s", name)

    def open_virtual_mixer_midi_out(self, name: str = "vcpi-MIDI-Mix-LED"):
        port_name = self.midimix.open_virtual_output(name)
        logger.info("[MIDI Mix OUT] Opened virtual: %s", port_name)

    def refresh_mixer_leds(self, slot_indices: Optional[list[int]] = None):
        self.midimix.refresh_leds(slot_indices)

    # -- convenience ---------------------------------------------------------

    def send_note(self, slot_index: int, note: int, velocity: int = 100,
                  duration: float = 0.3):
        if not deps.HAS_MIDO or deps.mido is None:
            return
        note = max(0, min(127, note))
        velocity = max(0, min(127, velocity))
        on = deps.mido.Message("note_on", note=note, velocity=velocity)
        off = deps.mido.Message("note_off", note=note)
        with self._note_lock:
            if self._shutdown:
                return
            slot = self.engine.slots[slot_index]
            if slot is None:
                return
            generation = slot.generation
            self.engine.enqueue_midi(slot_index, on, generation=generation)

            def finish_note():
                with self._note_lock:
                    if self._shutdown:
                        return
                    pending = self._note_timers.pop(timer, None)
                    if pending is None:
                        return
                    current = self.engine.slots[slot_index]
                    if current is not None and current.generation == generation:
                        self.engine.enqueue_midi(slot_index, off, generation=generation)

            timer = threading.Timer(duration, finish_note)
            timer.daemon = True
            self._note_timers[timer] = (slot_index, off, generation)
            timer.start()

    # -- audio / link --------------------------------------------------------

    def start_audio(self, output_device=None):
        self.engine.request("start_audio", output_device=output_device)

    def stop_audio(self):
        self.engine.request("stop_audio")

    def start_link(self, bpm: Optional[float] = None):
        if bpm is not None:
            self.link.bpm = bpm
        self.link.enable()
        logger.info("[Link] Enabled at %.1f BPM", self.link.bpm)

    def stop_link(self):
        self.link.disable()
        logger.info("[Link] Disabled")

    # -- session persistence -------------------------------------------------

    def save_session(self, path: Optional[str] = None):
        """Save current state to a JSON session file."""
        session_module = importlib.import_module("core.session")
        p = Path(path) if path else self.session_path
        session_module.save(self, p)

    def restore_session(self, path: Optional[str] = None):
        """Restore state from a JSON session file."""
        session_module = importlib.import_module("core.session")
        p = Path(path) if path else self.session_path
        session_module.restore(self, p)

    # -- shutdown ------------------------------------------------------------

    def _cancel_note_timers(self):
        with self._note_lock:
            pending = list(self._note_timers.items())
            self._note_timers.clear()
        for timer, _ in pending:
            timer.cancel()
        for timer, (slot_index, off, generation) in pending:
            timer.join()
            slot = self.engine.slots[slot_index]
            if slot is not None and slot.generation == generation:
                try:
                    self.engine.enqueue_midi(slot_index, off, generation=generation)
                except Exception:
                    logger.exception("[Host] Failed sending final note-off")

    def _close_resources(self):
        """Try every cleanup even when another subsystem has failed."""
        actions = [self._cancel_note_timers]
        sequencer = getattr(self, "sequencer", None)
        if sequencer is not None:
            actions.append(sequencer.stop)
        midimix = getattr(self, "midimix", None)
        if midimix is not None:
            actions.append(lambda: self.engine.remove_state_listener(midimix.refresh_leds))
        actions.extend(ctrl.close for ctrl in self.midi_inputs)
        self.midi_inputs.clear()
        if midimix is not None:
            actions.append(midimix.close)
        link = getattr(self, "link", None)
        if link is not None:
            actions.append(link.disable)
        actions.append(self.engine.shutdown)
        first_error = None
        for action in actions:
            try:
                action()
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
                logger.exception("[Host] Cleanup failed")
        return first_error

    def shutdown(self, *, save: bool = True):
        """Release all resources, optionally saving a fully initialized session."""
        with self._shutdown_lock:
            if self._shutdown:
                return
            self._shutdown = True
            save_error = None
            if save:
                try:
                    self.save_session()
                except AudioProcessError as exc:
                    if self.engine.failure is None:
                        save_error = exc
                    else:
                        logger.warning(
                            "[Host] Skipping shutdown session save; preserving %s: %s",
                            self.session_path, exc,
                        )
                except BaseException as exc:
                    save_error = exc
            cleanup_error = self._close_resources()
            logger.info("[Host] Shutdown complete")
            if save_error is not None:
                raise save_error
            if cleanup_error is not None:
                raise cleanup_error
