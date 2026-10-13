"""Native plugin and audio-device owner, imported only by the audio child."""

from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

from core import audio_deps as deps
from core.engine import AudioEngine
from core.models import InstrumentSlot, NUM_SLOTS
from sampler import WavSamplerPlugin


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


class PluginHost:
    def __init__(self, sample_rate: int = 44100, buffer_size: int = 512):
        self.sample_rate = sample_rate
        self.buffer_size = buffer_size
        self.patches_dir = Path(
            os.environ.get(PATCHES_DIR_ENV, DEFAULT_PATCHES_DIR)
        ).expanduser()
        self.engine = AudioEngine(sample_rate, buffer_size)
        self._shutdown = False

    @property
    def audio_output_name(self) -> Optional[str]:
        """Last selected output name, cached before the stream starts."""
        return self.engine.output_name

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

    def _warmup_plugin(self, plugin, num_blocks: int = 2) -> None:
        """Render a few silent blocks at the runtime sample rate / buffer
        size so any remaining lazy init (FFT plans, JIT) is paid here
        rather than inside the real-time audio callback.
        """
        if deps.np is None:
            return
        sr = self.sample_rate
        buf = self.engine.buffer_size
        channels = self.engine.output_channels
        duration = buf / sr
        silence_midi: list[object] = []
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

    def _warmup_effect(self, plugin, num_blocks: int = 2) -> None:
        """Prime only a new effect with silent audio; propagate init failures.

        Call while engine.edit_effects() has stopped audio so existing effects
        retain their processing state and the new plugin uses runtime settings.
        """
        silence = deps.np.zeros(
            (self.engine.output_channels, self.engine.buffer_size),
            dtype=deps.np.float32,
        )
        for _ in range(num_blocks):
            silence.fill(0)
            plugin.process(
                silence,
                sample_rate=self.engine.sample_rate,
                buffer_size=self.engine.buffer_size,
                reset=False,
            )

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

        # Skip pedalboard's internal warmup (attemptToWarmUp) which can
        # block for up to 10 s sending MIDI and pumping the JUCE message
        # loop.  Our own lightweight _warmup_plugin() below primes the
        # plugin at the actual runtime sample-rate / buffer-size instead.
        plugin = deps.load_plugin(path, initialization_timeout=0)
        if not plugin.is_instrument:
            raise ValueError(f"{path} is not an instrument")
        self._set_plugin_info_type(plugin, "Instrument")

        # Light warmup at the correct sr / buffer size.
        self._warmup_plugin(plugin)

        slot = InstrumentSlot(
            name=slot_name,
            path=path,
            plugin=plugin,
            source_type="plugin",
        )

        # The child lifecycle dispatcher has stopped audio before publication.
        self.engine.slots[slot_index] = slot

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
        # Allocate reusable sampler scratch before publishing to the callback.
        plugin.process(
            deps.np.zeros((self.engine.output_channels, self.engine.buffer_size),
                          dtype=deps.np.float32),
            self.engine.sample_rate,
        )
        self._set_plugin_info_type(plugin, "Sample")

        slot = InstrumentSlot(
            name=name or Path(resolved).stem,
            path=resolved,
            plugin=plugin,
            source_type="wav",
        )
        self.engine.slots[slot_index] = slot
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
        logger.info("[INST] removed slot %d (%s)", slot_index + 1, slot.name)
        return slot

    def load_effect(self, path: str, slot_index: Optional[int] = None,
                    name: Optional[str] = None):
        """Warm and publish a replacement insert/master chain with audio stopped."""
        if not deps.HAS_PEDALBOARD:
            raise RuntimeError("pedalboard not installed")
        if deps.load_plugin is None:
            raise RuntimeError("pedalboard loader unavailable")
        if deps.Pedalboard is None:
            raise RuntimeError("pedalboard chain unavailable")
        if slot_index is not None:
            if not 0 <= slot_index < NUM_SLOTS:
                raise ValueError(f"slot must be 1-{NUM_SLOTS}")
            if self.engine.slots[slot_index] is None:
                raise ValueError(f"Slot {slot_index + 1} is empty")

        plugin = deps.load_plugin(path, initialization_timeout=0)
        if self._plugin_is_instrument(plugin):
            raise ValueError(f"{path} is an instrument, not an effect")
        plugin._vcpi_path = path  # stash load path for session save
        self._set_plugin_info_type(plugin, "Effect")
        label = name or Path(path).stem
        with self.engine.edit_effects():
            slot = None
            if slot_index is not None:
                slot = self.engine.slots[slot_index]
                if slot is None:
                    raise ValueError(f"Slot {slot_index + 1} is empty")
            effects = [
                *(slot.effects if slot is not None else self.engine.master_effects),
                plugin,
            ]
            board = deps.Pedalboard(effects)
            self._warmup_effect(plugin)
            # Publish only after construction and warmup both succeed.
            if slot is not None:
                slot.effects = effects
                slot._effects_board = board
            else:
                self.engine.master_effects = effects
                self.engine._master_board = board
        if slot is not None:
            logger.info("[FX] '%s' -> slot %d (%s)", label, slot_index + 1, slot.name)
        else:
            logger.info("[FX] '%s' -> master bus", label)

    @staticmethod
    def _plugin_is_instrument(plugin: object) -> bool:
        try:
            is_instrument = getattr(plugin, "is_instrument", None)
        except Exception:
            is_instrument = None
        if is_instrument is True:
            return True

        try:
            category = getattr(plugin, "category", None)
        except Exception:
            category = None
        if isinstance(category, str):
            tokens = {token.strip().lower() for token in re.split(r"[|,;/]", category)}
            if "instrument" in tokens:
                return True

        return False

    def remove_effect(self, slot_index: Optional[int], effect_index: int):
        """Publish a replacement chain without mutating or processing the old one."""
        if slot_index is not None and not 0 <= slot_index < NUM_SLOTS:
            raise ValueError(f"slot must be 1-{NUM_SLOTS}")
        with self.engine.edit_effects():
            slot = None
            if slot_index is not None:
                slot = self.engine.slots[slot_index]
                if slot is None:
                    raise ValueError(f"Slot {slot_index + 1} is empty")
            effects = list(
                slot.effects if slot is not None else self.engine.master_effects
            )
            del effects[effect_index]
            board = deps.Pedalboard(effects) if effects else None
            if slot is not None:
                slot.effects = effects
                slot._effects_board = board
            else:
                self.engine.master_effects = effects
                self.engine._master_board = board

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
        self.engine.stop()
        if isinstance(output_device, str):
            token = output_device.strip()
            if token.isdigit():
                output_device = int(token)
            elif not token:
                output_device = None
            else:
                output_device = self._resolve_audio_device_by_name(token)

        self.engine.start(output_device)

    def stop_audio(self):
        self.engine.stop()

    def shutdown(self):
        """Release native audio resources once."""
        if self._shutdown:
            return
        self._shutdown = True
        self.engine.shutdown()
