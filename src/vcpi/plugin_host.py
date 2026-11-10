"""Native plugin and audio-device owner, imported only by the audio child."""

from __future__ import annotations

import logging
import json
import os
import re
import time
from pathlib import Path
from typing import Optional

from vcpi import audio_deps as deps
from vcpi.audio.control import AudioEngine
from vcpi.models import InstrumentTrack, NUM_TRACKS
from vcpi.resources import configured_patches_root


logger = logging.getLogger(__name__)

class NativePlugin:
    """Metadata/parameter adapter; no Python audio-processing entrypoint."""

    def __init__(self, path, sample_rate, frames, kind):
        from types import SimpleNamespace

        if kind == "deck":
            native_path = path
        else:
            path = Path(path).expanduser().resolve()
            if not path.exists():
                raise FileNotFoundError(f"plugin not found: {path}")
            native_path = str(path)
        object.__setattr__(self, "native", deps.require_native().Plugin(
            native_path, sample_rate, frames, kind))
        object.__setattr__(self, "_kind", kind)
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

    def get_state(self) -> bytes | None:
        """Capture VST3 chunks only; file-backed sources persist explicit fields."""
        return None if self._kind == "deck" else self.native.get_state()

    def load_state(self, state):
        result = self.native.load_state(state)
        object.__setattr__(self, "_parameter_names", frozenset(self.native.parameters()))
        return result

    def load_saved_state(self, state: bytes):
        """Restore a captured JUCE VST3 chunk, rejecting malformed state."""
        result = self.native.load_saved_state(state)
        object.__setattr__(self, "_parameter_names", frozenset(self.native.parameters()))
        return result


CARDINAL_VST3_ENV = "CARDINAL_VST3_PATH"
CARDINAL_VST3_CANDIDATES = (
    "/usr/lib/vst3/Cardinal.vst3",
    "/usr/local/lib/vst3/Cardinal.vst3",
    "~/Library/Audio/Plug-Ins/VST3/Cardinal.vst3",
    "/Library/Audio/Plug-Ins/VST3/Cardinal.vst3",
)
PLUGDATA_VST3_ENV = "PLUGDATA_VST3_PATH"
PLUGDATA_VST3_CANDIDATES = (
    "~/.vst3/plugdata.vst3",
    "/usr/lib/vst3/plugdata.vst3",
    "/usr/local/lib/vst3/plugdata.vst3",
    "~/Library/Audio/Plug-Ins/VST3/plugdata.vst3",
    "/Library/Audio/Plug-Ins/VST3/plugdata.vst3",
)


class PluginHost:
    def __init__(self, sample_rate: int = 44100, buffer_size: int = 512,
                 *, track_mode=False):
        self.sample_rate = sample_rate
        self.buffer_size = buffer_size
        self.patches_dir = configured_patches_root()
        self.engine = AudioEngine(sample_rate, buffer_size, track_mode=track_mode)
        self._shutdown = False

    @property
    def audio_output_name(self) -> Optional[str]:
        """Last successfully opened output name, retained while stopped."""
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

    def _apply_vcv_patch(self, plugin: NativePlugin, patch_path: Path) -> str:
        """Import a Cardinal project through the native VST3 state adapter."""
        plugin.load_preset(str(patch_path))
        return "patch imported via Cardinal VST3 state"

    def load_vcv(self, track_index: int, vcv_path: str,
                 cardinal_path: Optional[str] = None,
                 name: Optional[str] = None) -> tuple[InstrumentTrack, str, str]:
        """Load Cardinal in a track and import a .vcv project file."""
        patch_path = Path(vcv_path).expanduser()
        if not patch_path.exists() or not patch_path.is_file():
            raise FileNotFoundError(f".vcv patch not found: {patch_path}")

        if patch_path.suffix.lower() != ".vcv":
            logger.warning("load_vcv called with non-.vcv file: %s", patch_path)

        cardinal_vst_path = self._resolve_cardinal_path(cardinal_path)
        track_label = name or f"Cardinal:{patch_path.stem}"

        track = self.load_instrument(track_index, str(cardinal_vst_path), track_label)
        track.source_type = "vcv"
        track.vcv_patch_path = str(patch_path)
        patch_result = self._apply_vcv_patch(track.plugin, patch_path)

        logger.info(
            "[VCV] track %d loaded from %s with patch %s (%s)",
            track_index + 1,
            cardinal_vst_path,
            patch_path,
            patch_result,
        )
        return track, patch_result, str(cardinal_vst_path)

    def _resolve_plugdata_path(self, plugdata_path: Optional[str]) -> Path:
        """Resolve an explicit override or discover the installed plugdata bundle."""
        override = plugdata_path or os.environ.get(PLUGDATA_VST3_ENV)
        if override:
            candidates = [Path(override).expanduser()]
        else:
            candidates = [Path(path).expanduser() for path in PLUGDATA_VST3_CANDIDATES]
        for candidate in candidates:
            if candidate.exists():
                return candidate.resolve()
        tried = ", ".join(str(candidate) for candidate in candidates)
        raise FileNotFoundError(
            "plugdata VST3 not found. Pass plugdata_path, set "
            f"{PLUGDATA_VST3_ENV}, or install plugdata in a standard VST3 directory "
            f"(tried: {tried})."
        )

    def load_pd(self, track_index: int, pd_path: str,
                plugdata_path: Optional[str] = None,
                name: Optional[str] = None) -> tuple[InstrumentTrack, str, str]:
        """Import a Pure Data patch before publishing its plugdata instrument."""
        if not 0 <= track_index < NUM_TRACKS:
            raise ValueError(f"track must be 1-{NUM_TRACKS}")
        patch_path = Path(pd_path).expanduser().resolve()
        if not patch_path.is_file():
            raise FileNotFoundError(f".pd patch not found: {patch_path}")
        if patch_path.suffix.lower() != ".pd":
            raise ValueError(f"Pure Data patch must have a .pd extension: {patch_path}")
        plugin_path = self._resolve_plugdata_path(plugdata_path)
        plugin = NativePlugin(
            str(plugin_path), self.sample_rate, self.buffer_size, "instrument")
        self._set_plugin_info_type(plugin, "Instrument")
        plugin.load_preset(str(patch_path))
        track = InstrumentTrack(
            name=name or f"plugdata:{patch_path.stem}",
            path=str(plugin_path),
            plugin=plugin,
            source_type="pd",
            pd_patch_path=str(patch_path),
        )
        self.engine.tracks[track_index] = track
        patch_result = "patch imported via plugdata VST3 state"
        logger.info("[PD] track %d loaded from %s with patch %s (%s)",
                    track_index + 1, plugin_path, patch_path, patch_result)
        return track, patch_result, str(plugin_path)

    def load_pd_patch(self, track_index: int, patch_name: str,
                      plugdata_path: Optional[str] = None,
                      name: Optional[str] = None) -> tuple[InstrumentTrack, str, str, Path]:
        """Load a .pd patch from the shared configured patches directory."""
        patch_path = self._resolve_patch_path(patch_name, ".pd").resolve()
        track, patch_result, used_plugdata = self.load_pd(
            track_index, str(patch_path), plugdata_path=plugdata_path, name=name)
        return track, patch_result, used_plugdata, patch_path

    def _resolve_patch_path(self, patch_name: str, extension: str = ".vcv") -> Path:
        """Resolve a patch file name from the configured patches directory."""
        token = patch_name.strip()
        if not token:
            raise ValueError("patch name is required")

        patches_dir = self.patches_dir
        if not patches_dir.is_absolute():
            patches_dir = Path.cwd() / patches_dir

        name_path = Path(token)
        candidates = [patches_dir / name_path]
        if name_path.suffix.lower() != extension:
            candidates.append(patches_dir / f"{token}{extension}")

        for candidate in candidates:
            if candidate.exists() and candidate.is_file():
                return candidate

        tried = ", ".join(str(p) for p in candidates)
        raise FileNotFoundError(
            f"patch '{patch_name}' not found in {patches_dir} (tried: {tried})"
        )

    def load_vcv_patch(self, track_index: int, patch_name: str,
                       cardinal_path: Optional[str] = None,
                       name: Optional[str] = None) -> tuple[InstrumentTrack, str, str, Path]:
        """Load a bundled or configured patch into a track using Cardinal."""
        patch_path = self._resolve_patch_path(patch_name)
        track_label = name or f"Cardinal:{patch_path.stem}"
        track, patch_result, used_cardinal = self.load_vcv(
            track_index,
            str(patch_path),
            cardinal_path=cardinal_path,
            name=track_label,
        )
        return track, patch_result, used_cardinal, patch_path

    # -- instrument loading ---------------------------------------------------

    def load_instrument(self, track_index: int, path: str,
                        name: Optional[str] = None) -> InstrumentTrack:
        """Load and warm a native VST3 instrument on its control owner."""
        if not 0 <= track_index < NUM_TRACKS:
            raise ValueError(f"track must be 1-{NUM_TRACKS}")

        track_name = name or Path(path).stem
        t0 = time.monotonic()
        logger.info("[INST] loading track %d '%s' from %s …",
                    track_index + 1, track_name, path)

        plugin = NativePlugin(path, self.sample_rate, self.buffer_size, "instrument")
        self._set_plugin_info_type(plugin, "Instrument")

        track = InstrumentTrack(
            name=track_name,
            path=path,
            plugin=plugin,
            source_type="plugin",
        )

        # The child lifecycle dispatcher has stopped audio before publication.
        self.engine.tracks[track_index] = track

        elapsed = time.monotonic() - t0
        logger.info("[INST] track %d ready (%.2fs)", track_index + 1, elapsed)
        return track

    def load_deck(self, track_index: int, catalog: str,
                  name: Optional[str] = None) -> InstrumentTrack:
        """Prepare the selected playlist asset before publishing any replacement."""
        from vcpi.deck_bank import validate_catalog

        if not 0 <= track_index < NUM_TRACKS:
            raise ValueError(f"track must be 1-{NUM_TRACKS}")
        bank = json.loads(validate_catalog(catalog))
        plugin = NativePlugin(catalog, self.sample_rate, self.buffer_size, "deck")
        object.__setattr__(plugin, "_vcpi_path", bank["bank_path"])
        self._set_plugin_info_type(plugin, "Deck")
        deck = dict(plugin.native.deck_status())
        deck.update(bank_path=bank["bank_path"], bank_id=bank["id"],
                    tracks=[dict(item) for item in bank["tracks"]])
        track = InstrumentTrack(name=name or bank["id"], path=bank["bank_path"],
                                plugin=plugin, source_type="deck", deck=deck)
        self.engine.tracks[track_index] = track
        return track

    def update_deck_catalog(self, track_index: int, catalog: str,
                            name: Optional[str] = None):
        """Edit the existing native deck, leaving its owner and effects intact."""
        from vcpi.deck_bank import validate_catalog

        track = self.engine.tracks[track_index]
        if track is None or track.source_type != "deck":
            raise ValueError(f"track {track_index + 1} is not a deck")
        bank = json.loads(validate_catalog(catalog))
        track.plugin.native.update_deck_catalog(catalog)
        track.plugin._refresh_metadata()
        track.deck.update(track.plugin.native.deck_status())
        track.deck.update(bank_path=bank["bank_path"], bank_id=bank["id"],
                          tracks=[dict(item) for item in bank["tracks"]])
        if name is not None:
            track.name = name

    def remove_instrument(self, track_index: int) -> InstrumentTrack:
        """Unload and clear one instrument track."""
        if not 0 <= track_index < NUM_TRACKS:
            raise ValueError(f"track must be 1-{NUM_TRACKS}")

        track = self.engine.tracks[track_index]
        if track is None:
            raise ValueError(f"Track {track_index + 1} is already empty")

        self.engine.tracks[track_index] = None
        logger.info("[INST] removed track %d (%s)", track_index + 1, track.name)
        return track

    def load_effect(self, path: str, track_index: Optional[int] = None,
                    name: Optional[str] = None):
        """Publish a native insert/master chain; warmup happens in Plugin."""
        if track_index is not None:
            if not 0 <= track_index < NUM_TRACKS:
                raise ValueError(f"track must be 1-{NUM_TRACKS}")
            if self.engine.tracks[track_index] is None:
                raise ValueError(f"Track {track_index + 1} is empty")

        plugin = NativePlugin(path, self.sample_rate, self.buffer_size, "effect")
        plugin._vcpi_path = path  # stash load path for session save
        self._set_plugin_info_type(plugin, "Effect")
        label = name or Path(path).stem
        with self.engine.edit_effects():
            track = None
            if track_index is not None:
                track = self.engine.tracks[track_index]
                if track is None:
                    raise ValueError(f"Track {track_index + 1} is empty")
            effects = [
                *(track.effects if track is not None else self.engine.master_effects),
                plugin,
            ]
            self.engine.set_effects(effects, track_index)
        if track is not None:
            logger.info("[FX] '%s' -> track %d (%s)", label, track_index + 1, track.name)
        else:
            logger.info("[FX] '%s' -> master bus", label)


    def remove_effect(self, track_index: Optional[int], effect_index: int):
        """Publish a replacement chain without mutating or processing the old one."""
        if track_index is not None and not 0 <= track_index < NUM_TRACKS:
            raise ValueError(f"track must be 1-{NUM_TRACKS}")
        with self.engine.edit_effects():
            track = None
            if track_index is not None:
                track = self.engine.tracks[track_index]
                if track is None:
                    raise ValueError(f"Track {track_index + 1} is empty")
            effects = list(
                track.effects if track is not None else self.engine.master_effects
            )
            del effects[effect_index]
            self.engine.set_effects(effects, track_index)

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
