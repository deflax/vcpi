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


logger = logging.getLogger(__name__)

class NativePlugin:
    """Metadata/parameter adapter; no Python audio-processing entrypoint."""

    def __init__(self, path, sample_rate, frames, kind):
        from types import SimpleNamespace

        path = Path(path).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(f"plugin or WAV not found: {path}")
        object.__setattr__(self, "native", deps.require_native().Plugin(
            str(path), sample_rate, frames, kind))
        object.__setattr__(self, "_descriptor_type", SimpleNamespace)
        object.__setattr__(self, "_parameter_names", frozenset(self.native.parameters()))
        self._refresh_metadata()
        object.__setattr__(self, "_vcpi_path", str(path))

    def _refresh_metadata(self):
        for key, value in self.native.metadata().items():
            object.__setattr__(self, key, value)

    @property
    def parameters(self):
        return {name: self._descriptor_type(**descriptor)
                for name, descriptor in self.native.parameters().items()}

    def parameter_descriptor(self, name):
        return self._descriptor_type(**self.native.parameter(name))

    def __getattr__(self, name):
        if name in self._parameter_names:
            return self.native.get_parameter(name)
        raise AttributeError(name)

    def __setattr__(self, name, value):
        if name in self._parameter_names:
            self.native.set_parameter(name, value)
        else:
            object.__setattr__(self, name, value)

    def load_preset(self, path):
        result = self.native.load_preset(path)
        object.__setattr__(self, "_parameter_names", frozenset(self.native.parameters()))
        return result

    def load_state(self, state):
        result = self.native.load_state(state)
        object.__setattr__(self, "_parameter_names", frozenset(self.native.parameters()))
        return result


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
    def __init__(self, sample_rate: int = 44100, buffer_size: int = 512,
                 *, slot_mode=False):
        self.sample_rate = sample_rate
        self.buffer_size = buffer_size
        self.patches_dir = Path(
            os.environ.get(PATCHES_DIR_ENV, DEFAULT_PATCHES_DIR)
        ).expanduser()
        self.engine = AudioEngine(sample_rate, buffer_size, slot_mode=slot_mode)
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

    # -- instrument loading ---------------------------------------------------

    def load_instrument(self, slot_index: int, path: str,
                        name: Optional[str] = None) -> InstrumentSlot:
        """Load and warm a native VST3 instrument on its control owner."""
        if not 0 <= slot_index < NUM_SLOTS:
            raise ValueError(f"slot must be 1-{NUM_SLOTS}")

        slot_name = name or Path(path).stem
        t0 = time.monotonic()
        logger.info("[INST] loading slot %d '%s' from %s …",
                    slot_index + 1, slot_name, path)

        plugin = NativePlugin(path, self.sample_rate, self.buffer_size, "instrument")
        self._set_plugin_info_type(plugin, "Instrument")

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
        plugin = NativePlugin(resolved, self.sample_rate, self.buffer_size, "wav")
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
        """Publish a native insert/master chain; warmup happens in Plugin."""
        if slot_index is not None:
            if not 0 <= slot_index < NUM_SLOTS:
                raise ValueError(f"slot must be 1-{NUM_SLOTS}")
            if self.engine.slots[slot_index] is None:
                raise ValueError(f"Slot {slot_index + 1} is empty")

        plugin = NativePlugin(path, self.sample_rate, self.buffer_size, "effect")
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
            self.engine.set_effects(effects, slot_index)
        if slot is not None:
            logger.info("[FX] '%s' -> slot %d (%s)", label, slot_index + 1, slot.name)
        else:
            logger.info("[FX] '%s' -> master bus", label)


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
            self.engine.set_effects(effects, slot_index)

    def _resolve_audio_device_by_name(self, name: str):
        """Resolve an audio output device name to a device index.

        Uses the same ALSA suffix stripping as MIDI port resolution so
        that saved names like "IQaudIODAC: DAC HiFi pcm512x-hifi-0 (hw:0,0)"
        survive reboots where the hw index changes.
        """
        if not deps.HAS_NATIVE:
            return name

        try:
            devices = deps.native.devices()
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
                out_indices.append(int(info.get("index", i)))

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
