"""vcpi core - the central coordinator for all subsystems."""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Optional

from core import deps, session
from controllers.akai_midimix import MidiMixController
from controllers.arturia_beatstep_pro import BeatStepProController
from controllers.novation_25le import Novation25LeController
from core.engine import AudioEngine
from core.link import LinkSync
from core.models import InstrumentSlot, NUM_SLOTS


logger = logging.getLogger(__name__)

CARDINAL_VST3_ENV = "CARDINAL_VST3_PATH"
CARDINAL_VST3_CANDIDATES = (
    "/usr/lib/vst3/Cardinal.vst3",
    "/usr/local/lib/vst3/Cardinal.vst3",
    "~/Library/Audio/Plug-Ins/VST3/Cardinal.vst3",
    "/Library/Audio/Plug-Ins/VST3/Cardinal.vst3",
)
PATCHES_DIR_ENV = "VCPI_PATCHES_DIR"
DEFAULT_PATCHES_DIR = "patches"


class VcpiCore:
    def __init__(self, sample_rate: int = 44100, buffer_size: int = 512,
                 session_path: Optional[str] = None):
        self.sample_rate = sample_rate
        self.buffer_size = buffer_size
        self.session_path = Path(session_path) if session_path else session.DEFAULT_SESSION_PATH

        self.engine = AudioEngine(sample_rate, buffer_size)
        self.link = LinkSync()
        self.patches_dir = Path(
            os.environ.get(PATCHES_DIR_ENV, DEFAULT_PATCHES_DIR)
        ).expanduser()

        self.bsp = BeatStepProController(self.engine)
        self.novation_25le = Novation25LeController(self.engine, self.bsp.channel_map)
        self.midimix = MidiMixController(self.engine)

    @property
    def channel_map(self) -> dict[int, int]:
        return self.bsp.channel_map

    @property
    def sequencer_midi_name(self) -> Optional[str]:
        return self.bsp.port_name

    @property
    def mixer_midi_name(self) -> Optional[str]:
        return self.midimix.input_port_name

    @property
    def keyboard_midi_name(self) -> Optional[str]:
        return self.novation_25le.port_name

    @property
    def mixer_midi_out_name(self) -> Optional[str]:
        return self.midimix.output_port_name

    # -- plugin management ---------------------------------------------------

    def _resolve_cardinal_path(self, cardinal_path: Optional[str]) -> Path:
        """Resolve Cardinal VST3 path from arg, env, or common defaults."""
        candidates: list[Path] = []

        if cardinal_path:
            candidates.append(Path(cardinal_path).expanduser())
        else:
            env_path = os.environ.get(CARDINAL_VST3_ENV)
            if env_path:
                candidates.append(Path(env_path).expanduser())
            candidates.extend(Path(p).expanduser() for p in CARDINAL_VST3_CANDIDATES)

        for candidate in candidates:
            if candidate.exists():
                return candidate

        sample = CARDINAL_VST3_CANDIDATES[0]
        raise FileNotFoundError(
            "Cardinal VST3 not found. Pass explicit path to load_vcv, set "
            f"{CARDINAL_VST3_ENV}, or install Cardinal at {sample}."
        )

    def _apply_vcv_patch(self, plugin: object, patch_path: Path) -> str:
        """Attempt to load a .vcv patch into a Cardinal plugin instance."""
        load_preset = getattr(plugin, "load_preset", None)
        if callable(load_preset):
            try:
                load_preset(str(patch_path))
                return "patch loaded via load_preset()"
            except Exception as exc:
                logger.debug("load_preset failed for %s: %s", patch_path, exc)

        load_state = getattr(plugin, "load_state", None)
        if callable(load_state):
            try:
                load_state(patch_path.read_bytes())
                return "patch loaded via load_state()"
            except Exception as exc:
                logger.debug("load_state failed for %s: %s", patch_path, exc)

        if hasattr(plugin, "state"):
            try:
                setattr(plugin, "state", patch_path.read_bytes())
                return "patch loaded via state property"
            except Exception as exc:
                logger.debug("state property load failed for %s: %s", patch_path, exc)

        param_names: list[str] = []
        params = getattr(plugin, "parameters", None)
        if params is not None and hasattr(params, "keys"):
            try:
                param_names = list(params.keys())
            except Exception:
                param_names = []

        param_tokens = ("vcv", "patch", "project", "rack", "file", "path", "preset")
        likely_params = [
            name for name in param_names if any(token in name.lower() for token in param_tokens)
        ]
        for param_name in likely_params:
            try:
                setattr(plugin, param_name, str(patch_path))
                return f"patch path applied to parameter '{param_name}'"
            except Exception as exc:
                logger.debug("set param %s failed for %s: %s", param_name, patch_path, exc)

        return "could not auto-load .vcv patch; Cardinal instance is loaded"

    def load_vcv(self, slot_index: int, vcv_path: str,
                 cardinal_path: Optional[str] = None,
                 name: Optional[str] = None) -> tuple[InstrumentSlot, str, str]:
        """Load Cardinal in a slot and attempt to apply a .vcv project file."""
        patch_path = Path(vcv_path).expanduser()
        if not patch_path.exists() or not patch_path.is_file():
            raise FileNotFoundError(f".vcv patch not found: {patch_path}")

        if patch_path.suffix.lower() != ".vcv":
            logger.warning("load_vcv called with non-.vcv file: %s", patch_path)

        cardinal_vst_path = self._resolve_cardinal_path(cardinal_path)
        slot_label = name or f"Cardinal:{patch_path.stem}"

        slot = self.load_instrument(slot_index, str(cardinal_vst_path), slot_label)
        patch_result = self._apply_vcv_patch(slot.plugin, patch_path)

        logger.info(
            "[VCV] slot %d loaded from %s with patch %s (%s)",
            slot_index + 1,
            cardinal_vst_path,
            patch_path,
            patch_result,
        )
        return slot, patch_result, str(cardinal_vst_path)

    def _resolve_patch_path(self, patch_name: str) -> Path:
        """Resolve a patch file name from the configured patches directory."""
        token = patch_name.strip()
        if not token:
            raise ValueError("patch name is required")

        patches_dir = self.patches_dir
        if not patches_dir.is_absolute():
            patches_dir = Path.cwd() / patches_dir

        name_path = Path(token)
        candidates = [patches_dir / name_path]
        if name_path.suffix.lower() != ".vcv":
            candidates.append(patches_dir / f"{token}.vcv")

        for candidate in candidates:
            if candidate.exists() and candidate.is_file():
                return candidate

        tried = ", ".join(str(p) for p in candidates)
        raise FileNotFoundError(
            f"patch '{patch_name}' not found in {patches_dir} (tried: {tried})"
        )

    def load_vcv_patch(self, slot_index: int, patch_name: str,
                       cardinal_path: Optional[str] = None,
                       name: Optional[str] = None) -> tuple[InstrumentSlot, str, str, Path]:
        """Load a patch from patches/ into an explicit slot using Cardinal."""
        patch_path = self._resolve_patch_path(patch_name)
        slot_label = name or f"Cardinal:{patch_path.stem}"
        slot, patch_result, used_cardinal = self.load_vcv(
            slot_index,
            str(patch_path),
            cardinal_path=cardinal_path,
            name=slot_label,
        )
        return slot, patch_result, used_cardinal, patch_path

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
            logger.info("[FX] '%s' -> slot %d (%s)", label, slot_index + 1, slot.name)
        else:
            self.engine.master_effects.append(plugin)
            logger.info("[FX] '%s' -> master bus", label)

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
        logger.info("[SEQ MIDI] Opened: %s", name)

    def open_keyboard_midi(self, port_index: int):
        name = self.novation_25le.open(port_index)
        logger.info("[Keyboard MIDI] Opened: %s", name)

    def open_mixer_midi(self, port_index: int):
        name = self.midimix.open_input(port_index)
        logger.info("[MIDI Mix IN] Opened: %s", name)

    def open_mixer_midi_out(self, port_index: int):
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
        logger.info("[Link] Enabled at %.1f BPM", self.link.bpm)

    def stop_link(self):
        self.link.disable()
        logger.info("[Link] Disabled")

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
        self.novation_25le.close()
        self.midimix.close()
        self.stop_link()
        logger.info("[Host] Shutdown complete")
