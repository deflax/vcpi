"""vcpi core - the central coordinator for all subsystems."""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Optional

from core import deps, session
from controllers.akai_midimix import MidiMixController
from controllers.midi_input import MidiInputController
from core.engine import AudioEngine
from core.link import LinkSync
from core.midi import list_midi_input_ports, list_midi_output_ports
from core.models import InstrumentSlot, NUM_SLOTS
from core.sampler import WavSamplerPlugin


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

        self.midi_inputs: list[MidiInputController] = []
        self.midimix = MidiMixController(self.engine)

        # Remember the most recently selected/active audio output device name.
        self._audio_output_name: Optional[str] = None

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
        """Most recently selected/active audio output device name."""
        current = self._active_audio_output_name()
        if current:
            self._audio_output_name = current
        return self._audio_output_name

    @staticmethod
    def _output_device_index(device: object) -> Optional[int]:
        """Extract output device index from sounddevice stream/default value."""
        if isinstance(device, int):
            return device
        if isinstance(device, (tuple, list)):
            if len(device) >= 2 and isinstance(device[1], int):
                return device[1]
            if len(device) == 1 and isinstance(device[0], int):
                return device[0]
        return None

    def _active_audio_output_name(self) -> Optional[str]:
        """Resolve the currently active output device name, if available."""
        if not deps.HAS_SOUNDDEVICE or deps.sd is None:
            return None

        stream = self.engine._stream
        if stream is None:
            return None

        index = self._output_device_index(getattr(stream, "device", None))
        if index is None or index < 0:
            return None

        try:
            info = deps.sd.query_devices(index)
        except Exception:
            return None

        name = info.get("name")
        if not name:
            return None
        return str(name)

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

    @staticmethod
    def _set_plugin_info_type(plugin: object, plugin_type: str) -> None:
        """Tag plugin instance so info panels can show a precise type."""
        try:
            setattr(plugin, "info_type", plugin_type)
        except Exception:
            logger.debug("failed setting info_type=%s", plugin_type, exc_info=True)

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
            "Cardinal VST3 not found. Pass explicit path to 'load vcv', set "
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
        slot.source_type = "vcv"
        slot.vcv_patch_path = str(patch_path)
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

    # -- plugin warmup --------------------------------------------------------

    def _warmup_plugin(self, plugin, num_blocks: int = 4) -> None:
        """Render a few silent blocks so the plugin's first-render penalty
        is absorbed here rather than in the real-time audio callback.

        Many VST3 plugins perform lazy initialization (JIT, FFT plans,
        internal caches) on their first process() call.  By doing it
        before the plugin is assigned to a slot, we prevent the audio
        callback from missing its deadline.
        """
        if deps.np is None:
            return
        sr = self.sample_rate
        buf = self.engine.buffer_size
        channels = self.engine.output_channels
        duration = buf / sr
        silence_midi: list = []
        for _ in range(num_blocks):
            try:
                plugin.process(
                    silence_midi,
                    duration=duration,
                    sample_rate=sr,
                    num_channels=channels,
                    buffer_size=buf,
                    reset=False,
                )
            except Exception:
                break

    # -- instrument loading ---------------------------------------------------

    def load_instrument(self, slot_index: int, path: str,
                        name: Optional[str] = None) -> InstrumentSlot:
        """Load a VST3 instrument into a slot.

        The plugin is warmed up (a few silent render passes) *before*
        being assigned to the slot so the audio callback never sees the
        expensive first-render cost that causes underflow warnings.
        """
        if not deps.HAS_PEDALBOARD:
            raise RuntimeError("pedalboard not installed")
        if deps.load_plugin is None:
            raise RuntimeError("pedalboard loader unavailable")
        if not 0 <= slot_index < NUM_SLOTS:
            raise ValueError(f"slot must be 1-{NUM_SLOTS}")

        slot_name = name or Path(path).stem
        t0 = time.monotonic()
        logger.info("[INST] loading slot %d '%s' from %s …",
                    slot_index + 1, slot_name, path)

        plugin = deps.load_plugin(path)
        if not plugin.is_instrument:
            raise ValueError(f"{path} is not an instrument")
        self._set_plugin_info_type(plugin, "Instrument")

        # Warmup: render silent blocks so the plugin's first-render
        # penalty (JIT, FFT plans, internal caches) is paid here on
        # the main thread rather than inside the real-time audio callback.
        self._warmup_plugin(plugin)

        slot = InstrumentSlot(
            name=slot_name,
            path=path,
            plugin=plugin,
            source_type="plugin",
        )

        # Atomic slot assignment (GIL guarantees reference store is atomic).
        self.engine.slots[slot_index] = slot

        # Build param cache for MIDI Mix (C++ property introspection).
        self.midimix.invalidate_param_cache(slot_index)
        self.midimix._build_param_cache(slot_index)

        elapsed = time.monotonic() - t0
        logger.info("[INST] slot %d ready (%.2fs)", slot_index + 1, elapsed)
        return slot

    def load_wav(self, slot_index: int, wav_path: str,
                 name: Optional[str] = None) -> InstrumentSlot:
        """Load a WAV file as a one-shot sampler instrument into a slot."""
        if not 0 <= slot_index < NUM_SLOTS:
            raise ValueError(f"slot must be 1-{NUM_SLOTS}")

        path = Path(wav_path).expanduser()
        if not path.is_absolute():
            cwd_candidate = Path.cwd() / path
            repo_candidate = Path(__file__).resolve().parent.parent / path
            if cwd_candidate.exists():
                path = cwd_candidate
            elif repo_candidate.exists():
                path = repo_candidate
            else:
                path = cwd_candidate

        resolved = str(path)
        plugin = WavSamplerPlugin.from_file(
            resolved,
            target_sample_rate=self.sample_rate,
            output_channels=self.engine.output_channels,
        )
        self._set_plugin_info_type(plugin, "Sample")

        slot = InstrumentSlot(
            name=name or Path(resolved).stem,
            path=resolved,
            plugin=plugin,
            source_type="wav",
        )
        self.engine.slots[slot_index] = slot
        self.midimix.invalidate_param_cache(slot_index)
        self.midimix._build_param_cache(slot_index)
        logger.info("[WAV] slot %d loaded from %s", slot_index + 1, resolved)
        return slot

    def remove_instrument(self, slot_index: int) -> InstrumentSlot:
        """Unload and clear one instrument slot."""
        if not 0 <= slot_index < NUM_SLOTS:
            raise ValueError(f"slot must be 1-{NUM_SLOTS}")

        slot = self.engine.slots[slot_index]
        if slot is None:
            raise ValueError(f"Slot {slot_index + 1} is already empty")

        self.engine.slots[slot_index] = None
        self.midimix.invalidate_param_cache(slot_index)
        logger.info("[INST] removed slot %d (%s)", slot_index + 1, slot.name)
        return slot

    def load_effect(self, path: str, slot_index: Optional[int] = None,
                    name: Optional[str] = None):
        """Load effect into a slot's insert chain or the master bus."""
        if not deps.HAS_PEDALBOARD:
            raise RuntimeError("pedalboard not installed")
        if deps.load_plugin is None:
            raise RuntimeError("pedalboard loader unavailable")
        plugin = deps.load_plugin(path)
        plugin._vcpi_path = path  # stash load path for session save
        self._set_plugin_info_type(plugin, "Effect")
        label = name or Path(path).stem
        if slot_index is not None:
            slot = self.engine.slots[slot_index]
            if slot is None:
                raise ValueError(f"Slot {slot_index + 1} is empty")
            slot.effects.append(plugin)
            slot._effects_board = None  # invalidate cached Pedalboard
            logger.info("[FX] '%s' -> slot %d (%s)", label, slot_index + 1, slot.name)
        else:
            self.engine.master_effects.append(plugin)
            self.engine._master_board = None  # invalidate cached Pedalboard
            logger.info("[FX] '%s' -> master bus", label)

    def remove_effect(self, slot_index: Optional[int], effect_index: int):
        if slot_index is not None:
            slot = self.engine.slots[slot_index]
            if slot is None:
                raise ValueError(f"Slot {slot_index + 1} is empty")
            del slot.effects[effect_index]
            slot._effects_board = None  # invalidate cached Pedalboard
        else:
            del self.engine.master_effects[effect_index]
            self.engine._master_board = None  # invalidate cached Pedalboard

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
        self.engine.enqueue_midi(slot_index, on)
        threading.Timer(duration, self.engine.enqueue_midi,
                        args=(slot_index, off)).start()

    # -- audio / link --------------------------------------------------------

    def _resolve_audio_device_by_name(self, name: str):
        """Resolve an audio output device name to a device index.

        Uses the same ALSA suffix stripping as MIDI port resolution so
        that saved names like "IQaudIODAC: DAC HiFi pcm512x-hifi-0 (hw:0,0)"
        survive reboots where the hw index changes.
        """
        if not deps.HAS_SOUNDDEVICE or deps.sd is None:
            return name

        try:
            devices = deps.sd.query_devices()
        except Exception:
            return name

        out_names: list[str] = []
        out_indices: list[int] = []
        for i, info in enumerate(devices):
            try:
                max_out = int(info.get("max_output_channels", 0))
            except (TypeError, ValueError):
                max_out = 0
            if max_out <= 0:
                continue
            dev_name = str(info.get("name", "")).strip()
            if dev_name:
                out_names.append(dev_name)
                out_indices.append(i)

        if not out_names:
            return name

        # exact
        if name in out_names:
            return out_indices[out_names.index(name)]

        # case-insensitive
        lower = name.lower()
        for j, candidate in enumerate(out_names):
            if candidate.lower() == lower:
                return out_indices[j]

        # stripped ALSA suffix
        stripped = self._strip_alsa_suffix(name).lower()
        for j, candidate in enumerate(out_names):
            if stripped and self._strip_alsa_suffix(candidate).lower() == stripped:
                return out_indices[j]

        # substring containment (unique)
        if stripped:
            matches = [
                j for j, candidate in enumerate(out_names)
                if stripped in self._strip_alsa_suffix(candidate).lower()
                or self._strip_alsa_suffix(candidate).lower() in stripped
            ]
            if len(matches) == 1:
                return out_indices[matches[0]]

        logger.warning("[Audio] could not fuzzy-match device '%s', passing as-is", name)
        return name

    def start_audio(self, output_device=None):
        if isinstance(output_device, str):
            token = output_device.strip()
            if token.isdigit():
                output_device = int(token)
            elif not token:
                output_device = None
            else:
                output_device = self._resolve_audio_device_by_name(token)

        self.engine.start(output_device)

        current = self._active_audio_output_name()
        if current:
            self._audio_output_name = current
        elif isinstance(output_device, str):
            self._audio_output_name = output_device
        elif isinstance(output_device, int):
            self._audio_output_name = str(output_device)

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
        self.engine.shutdown()  # stops audio stream + render thread pool
        for ctrl in self.midi_inputs:
            ctrl.close()
        self.midi_inputs.clear()
        self.midimix.close()
        self.stop_link()
        logger.info("[Host] Shutdown complete")
