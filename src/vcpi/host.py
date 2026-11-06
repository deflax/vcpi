"""vcpi core - the central coordinator for all subsystems."""

from __future__ import annotations

import logging
import importlib
import re
import threading
import time
from pathlib import Path
from typing import Optional

from vcpi import deps
from vcpi.controllers.akai_midimix import MidiMixController
from vcpi.controllers.midi_input import MidiInputController
from vcpi.controllers.beatstep_pro import BeatStepProController
from vcpi.audio.client import RemoteAudioEngine
from vcpi.audio.protocol import AudioProcessError
from vcpi.link import LinkSync
from vcpi.timeline import MusicalTimeline
from vcpi.midi import list_midi_input_ports, list_midi_output_ports
from vcpi.models import InstrumentTrack, NUM_TRACKS
from vcpi.resources import configured_patches_root


logger = logging.getLogger(__name__)



class VcpiCore:
    def __init__(self, sample_rate: int = 44100, buffer_size: int = 512,
                 session_path: Optional[str] = None):
        self.sample_rate = sample_rate
        self.buffer_size = buffer_size
        self.preferred_output_device: int | str | None = None
        session_module = importlib.import_module("vcpi.sessions.persistence")
        self.session_path = Path(session_path) if session_path else session_module.DEFAULT_SESSION_PATH
        self.loaded_session_name: Optional[str] = None
        self.loaded_session_path: Optional[Path] = None
        self.patches_dir = configured_patches_root()
        self.midi_inputs: list[MidiInputController] = []
        self.preferred_beatstep_input: str | None = None
        self._beatstep_discovery_at = 0.0
        self.controller_discovery_enabled = False
        self._controller_lock = threading.RLock()
        self._shutdown_lock = threading.Lock()
        self._shutdown = False
        self._note_lock = threading.Lock()
        self._note_timers: dict[threading.Timer, tuple[int, object, int]] = {}
        self._deck_clock_stop = threading.Event()
        self._deck_clock_thread: threading.Thread | None = None
        self.engine = RemoteAudioEngine(sample_rate, buffer_size)
        try:
            self.link = LinkSync()
            self.timeline = MusicalTimeline(self.link)
            self.beatstep = BeatStepProController(self.engine, self.timeline)
            self.midimix = MidiMixController(self.engine)
            self.engine.add_state_listener(self.midimix.refresh_leds)
            sequencer_module = importlib.import_module("vcpi.sequencer")
            self.sequencer = sequencer_module.Sequencer(self)
            self._deck_clock_thread = threading.Thread(
                target=self._run_deck_clock, name="vcpi-deck-clock", daemon=True)
            self._deck_clock_thread.start()
        except BaseException:
            self._shutdown = True
            self._close_resources()
            raise

    def _feed_deck_clock(self):
        bpm, beat, sampled_at = self.timeline.clock_timeline()
        decks = [(index, track.generation)
                 for index, track in enumerate(self.engine.tracks)
                 if track is not None and track.source_type == "deck"]
        for index, generation in decks:
            if self._deck_clock_stop.is_set():
                return
            accepted = self.engine.enqueue_deck_command(
                index, "sync_tempo", bpm, generation=generation)
            if accepted is False:
                continue
            current_beat = beat + (time.monotonic() - sampled_at) * bpm / 60.0
            self.engine.enqueue_deck_command(
                index, "sync_beat", current_beat, generation=generation)

    def _run_deck_clock(self):
        failed = False
        next_feed = 0.0
        while not self._deck_clock_stop.is_set():
            try:
                now = time.monotonic()
                self.timeline.timeline()
                if now >= next_feed:
                    self._feed_deck_clock()
                    next_feed = now + 0.1
                if now >= self._beatstep_discovery_at:
                    self._discover_beatstep()
                    self._beatstep_discovery_at = now + 2.0
                failed = False
            except Exception:
                if not failed:
                    logger.warning("[Timing] Timing service unavailable", exc_info=True)
                failed = True
            self._deck_clock_stop.wait(1.0 if failed else 0.01)

    def _stop_deck_clock(self):
        self._deck_clock_stop.set()
        thread = self._deck_clock_thread
        if thread is not None and thread.ident is not None:
            thread.join(timeout=1.0)

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
        """Last successfully opened output name, from the audio child's cache."""
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

    def _clear_deck_holds(self, *, track_index=None, channel=None):
        for controller in self.midi_inputs:
            controller.clear_deck_holds(track_index=track_index, channel=channel)
        if channel is None:
            self.beatstep.clear_deck_holds(track_index=track_index)

    def _loaded_track(self, track_index: int) -> InstrumentTrack:
        track = self.engine.tracks[track_index]
        if track is None:
            raise RuntimeError(f"Audio child did not publish track {track_index + 1}")
        self.midimix.invalidate_param_cache(track_index)
        self.midimix._build_param_cache(track_index)
        return track

    def load_instrument(self, track_index: int, path: str,
                        name: Optional[str] = None) -> InstrumentTrack:
        """Stage a native track process and return its published cached state."""
        self._clear_deck_holds(track_index=track_index)
        self.engine.request("load_instrument", track_index=track_index, path=path, name=name)
        return self._loaded_track(track_index)

    def load_wav(self, track_index: int, wav_path: str,
                 name: Optional[str] = None) -> InstrumentTrack:
        """Stage a sampler track process and return its published cached state."""
        self._clear_deck_holds(track_index=track_index)
        self.engine.request("load_wav", track_index=track_index, wav_path=wav_path, name=name)
        return self._loaded_track(track_index)

    def load_deck(self, track_index: int, audio_path: str,
                  name: Optional[str] = None) -> InstrumentTrack:
        """Create a paused playlist or append without replacing its audio owner."""
        from uuid import uuid4
        from vcpi.deck_bank import AUDIO_SUFFIXES, audio_entry, make_catalog

        if not 0 <= track_index < NUM_TRACKS:
            raise ValueError(f"track must be 1-{NUM_TRACKS}")
        path = Path(audio_path).expanduser().resolve()
        if path.is_dir():
            paths = sorted(
                (child for child in path.iterdir()
                 if child.suffix.lower() in AUDIO_SUFFIXES and child.is_file()),
                key=lambda child: child.name,
            )
            if not paths:
                raise ValueError(f"no supported deck audio files in directory: {path}")
        else:
            paths = [path]
        track = self.engine.tracks[track_index]
        existing = track.deck["tracks"] if track is not None and track.source_type == "deck" else []
        seen = {entry["path"] for entry in existing}
        entries = []
        for candidate in paths:
            resolved = str(candidate.resolve())
            if resolved not in seen:
                entries.append(audio_entry(candidate))
                seen.add(resolved)
        if existing:
            if not entries and name is None:
                return self._loaded_track(track_index)
            catalog = make_catalog(track.deck["bank_id"], [*existing, *entries],
                                   bank_path=track.deck["bank_path"])
            self.engine.request("update_deck_catalog", track_index=track_index,
                                catalog=catalog, name=name)
        else:
            catalog = make_catalog(uuid4().hex, entries)
            self._clear_deck_holds(track_index=track_index)
            self.engine.request("load_deck", track_index=track_index, catalog=catalog,
                                name=name if name is not None else entries[0]["title"])
        return self._loaded_track(track_index)

    def remove_deck_entry(self, track_index: int, ordinal: int) -> InstrumentTrack:
        """Remove a one-based playlist entry while retaining the track and inserts."""
        from vcpi.deck_bank import make_catalog

        if not 0 <= track_index < NUM_TRACKS:
            raise ValueError(f"track must be 1-{NUM_TRACKS}")
        track = self.engine.tracks[track_index]
        if track is None or track.source_type != "deck":
            raise ValueError(f"Track {track_index + 1} is not a deck")
        entries = track.deck["tracks"]
        if not 1 <= ordinal <= len(entries):
            raise ValueError(f"deck entry must be 1-{len(entries)}")
        if len(entries) == 1:
            raise ValueError(f"Cannot remove the final deck entry; use track {track_index + 1} clear")
        catalog = make_catalog(track.deck["bank_id"], entries[:ordinal - 1] + entries[ordinal:],
                               bank_path=track.deck["bank_path"])
        self.engine.request("update_deck_catalog", track_index=track_index, catalog=catalog)
        return self._loaded_track(track_index)

    def restore_deck(self, track_index: int, deck: dict,
                     name: Optional[str] = None) -> InstrumentTrack:
        """Restore embedded assets, resolving a legacy manifest only when required."""
        from vcpi.deck_bank import load_bank, make_catalog

        for key in ("bank_id", "track_id"):
            if not isinstance(deck.get(key), str) or not deck[key].strip():
                raise ValueError(f"deck {key} is required")
        settings = dict(track_id=deck["track_id"], cue=deck["cue"],
                        pitch=deck["pitch"], sync=deck.get("sync", True),
                        phase_offset=deck.get("phase_offset", 0))
        if "tracks" in deck:
            catalog = make_catalog(deck["bank_id"], deck["tracks"],
                                   bank_path=deck.get("bank_path"), **settings)
        else:
            catalog = load_bank(deck["bank_path"], bank_id=deck["bank_id"], **settings)
        self._clear_deck_holds(track_index=track_index)
        self.engine.request("load_deck", track_index=track_index, catalog=catalog, name=name)
        return self._loaded_track(track_index)

    def remove_instrument(self, track_index: int) -> InstrumentTrack:
        """Remove an instrument, retaining the previous snapshot for callers."""
        if not 0 <= track_index < NUM_TRACKS:
            raise ValueError(f"track must be 1-{NUM_TRACKS}")
        track = self.engine.tracks[track_index]
        if track is None:
            raise ValueError(f"Track {track_index + 1} is already empty")
        self._clear_deck_holds(track_index=track_index)
        self.engine.request("remove_instrument", track_index=track_index)
        self.midimix.invalidate_param_cache(track_index)
        return track

    def load_effect(self, path: str, track_index: Optional[int] = None,
                    name: Optional[str] = None):
        self.engine.request("load_effect", path=path, track_index=track_index, name=name)
        if track_index is not None:
            self.midimix.invalidate_param_cache(track_index)

    def remove_effect(self, track_index: Optional[int], effect_index: int):
        self.engine.request("remove_effect", track_index=track_index, effect_index=effect_index)
        if track_index is not None:
            self.midimix.invalidate_param_cache(track_index)

    def load_vcv(self, track_index: int, vcv_path: str,
                 cardinal_path: Optional[str] = None,
                 name: Optional[str] = None) -> tuple[InstrumentTrack, str, str]:
        self._clear_deck_holds(track_index=track_index)
        result = self.engine.request(
            "load_vcv", track_index=track_index, vcv_path=vcv_path,
            cardinal_path=cardinal_path, name=name,
        )
        return self._loaded_track(track_index), result["patch_result"], result["cardinal_path"]

    def load_vcv_patch(self, track_index: int, patch_name: str,
                       cardinal_path: Optional[str] = None,
                       name: Optional[str] = None) -> tuple[InstrumentTrack, str, str, Path]:
        self._clear_deck_holds(track_index=track_index)
        result = self.engine.request(
            "load_vcv_patch", track_index=track_index, patch_name=patch_name,
            cardinal_path=cardinal_path, name=name,
        )
        return (
            self._loaded_track(track_index), result["patch_result"],
            result["cardinal_path"], Path(result["patch_path"]),
        )

    def load_pd(self, track_index: int, pd_path: str,
                plugdata_path: Optional[str] = None,
                name: Optional[str] = None) -> tuple[InstrumentTrack, str, str]:
        """Stage a plugdata patch and publish only after successful import."""
        self._clear_deck_holds(track_index=track_index)
        result = self.engine.request(
            "load_pd", track_index=track_index, pd_path=pd_path,
            plugdata_path=plugdata_path, name=name,
        )
        return self._loaded_track(track_index), result["patch_result"], result["plugdata_path"]

    def load_pd_patch(self, track_index: int, patch_name: str,
                      plugdata_path: Optional[str] = None,
                      name: Optional[str] = None) -> tuple[InstrumentTrack, str, str, Path]:
        self._clear_deck_holds(track_index=track_index)
        result = self.engine.request(
            "load_pd_patch", track_index=track_index, patch_name=patch_name,
            plugdata_path=plugdata_path, name=name,
        )
        return (
            self._loaded_track(track_index), result["patch_result"],
            result["plugdata_path"], Path(result["patch_path"]),
        )

    # -- routing -------------------------------------------------------------

    def route(self, midi_channel: int, track_index: int):
        self._clear_deck_holds(channel=midi_channel)
        self.engine.route(midi_channel, track_index)

    def unroute(self, midi_channel: int):
        self._clear_deck_holds(channel=midi_channel)
        self.engine.unroute(midi_channel)

    # -- MIDI controllers ----------------------------------------------------

    def open_midi_input(self, port_index: int | str) -> MidiInputController:
        """Open any MIDI input port and add it to the active inputs list."""
        with self._controller_lock:
            return self._open_midi_input(port_index)

    def _open_midi_input(self, port_index):
        ports = list_midi_input_ports()
        port_index = self._resolve_midi_input_port(port_index)
        if self.beatstep.input_port_name == ports[port_index]:
            raise ValueError("BeatStep Pro input is already owned by its controller integration")
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

    def refresh_mixer_leds(self, track_indices: Optional[list[int]] = None):
        self.midimix.refresh_leds(track_indices)

    def open_beatstep_midi(self, port: int | str | None = None):
        """Open the dedicated controller, optionally identifying its USB port."""
        with self._controller_lock:
            return self._open_beatstep_midi(port)

    def _open_beatstep_midi(self, port):
        ports = list_midi_input_ports()
        if port is None:
            matches = [index for index, name in enumerate(ports)
                       if "beatstep" in name.lower().replace(" ", "")]
            if len(matches) != 1:
                raise ValueError("Expected one BeatStep Pro input; select a port explicitly")
            index = matches[0]
        else:
            index = self._resolve_midi_input_port(port)
        name = ports[index]
        if name in self.midi_input_names:
            raise ValueError("Close this generic MIDI input before opening BeatStep Pro")
        if self.beatstep.input_port_name != name:
            self.beatstep.open_input(index)
        self.preferred_beatstep_input = name
        logger.info("[BeatStep Pro] Opened: %s", name)

    def close_beatstep_midi(self):
        with self._controller_lock:
            self.preferred_beatstep_input = ""
            self.beatstep.close()

    def _discover_beatstep(self):
        """Reconnect the selected device, or identify one unambiguous USB device."""
        with self._controller_lock:
            self._discover_beatstep_locked()

    def _discover_beatstep_locked(self):
        if not self.controller_discovery_enabled:
            return
        if self.preferred_beatstep_input == "":
            return
        try:
            ports = list_midi_input_ports()
        except Exception:
            return  # MIDI can be unavailable on a headless machine.
        current = self.beatstep.input_port_name
        if current is not None:
            if current in ports:
                return
            self.beatstep.close()
        target = self.preferred_beatstep_input
        if target:
            if isinstance(target, int) or target.strip().isdigit():
                index = int(target)
                if not 0 <= index < len(ports):
                    return
            else:
                try:
                    index = self._resolve_port_index_by_name(target, ports, "BeatStep Pro input")
                except ValueError:
                    return
        else:
            matches = [index for index, name in enumerate(ports)
                       if "beatstep" in name.lower().replace(" ", "")
                       and name not in self.midi_input_names]
            if len(matches) != 1:
                return
            index = matches[0]
        try:
            self.open_beatstep_midi(index)
        except Exception:
            logger.debug("[BeatStep Pro] Input not available", exc_info=True)


    # -- convenience ---------------------------------------------------------

    def send_note(self, track_index: int, note: int, velocity: int = 100,
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
            track = self.engine.tracks[track_index]
            if track is None:
                return
            generation = track.generation
            self.engine.enqueue_midi(track_index, on, generation=generation)

            def finish_note():
                with self._note_lock:
                    if self._shutdown:
                        return
                    pending = self._note_timers.pop(timer, None)
                    if pending is None:
                        return
                    current = self.engine.tracks[track_index]
                    if current is not None and current.generation == generation:
                        self.engine.enqueue_midi(track_index, off, generation=generation)

            timer = threading.Timer(duration, finish_note)
            timer.daemon = True
            self._note_timers[timer] = (track_index, off, generation)
            timer.start()

    # -- audio / link --------------------------------------------------------

    def start_audio(self, output_device=None):
        selected = self.preferred_output_device if output_device is None else output_device
        if isinstance(selected, str) and selected.strip().isdigit():
            selected = int(selected.strip())
        self.engine.request("start_audio", output_device=selected)
        self.preferred_output_device = self.engine.audio_output_name or selected

    def stop_audio(self):
        self.engine.request("stop_audio")

    def start_link(self, bpm: Optional[float] = None):
        if bpm is not None:
            self.timeline.bpm = bpm
        bpm_now, beat, sampled_at = self.timeline.clock_timeline()
        self.link.enable()
        if self.timeline.active_source == "internal":
            beat += (time.monotonic() - sampled_at) * bpm_now / 60.0
            self.link.seed(bpm_now, beat, self.timeline.running)
        logger.info("[Link] Enabled at %.1f BPM", self.link.bpm)

    def stop_link(self):
        self.link.disable()
        logger.info("[Link] Disabled")

    # -- session persistence -------------------------------------------------

    def save_session(self, path: Optional[str] = None):
        """Save current state to a JSON session file."""
        session_module = importlib.import_module("vcpi.sessions.persistence")
        p = Path(path) if path else self.session_path
        session_module.save(self, p)

    def restore_session(self, path: Optional[str] = None, *,
                        start_audio: bool = True, skip_settings=()):
        """Restore a session; startup may defer audio and override saved settings."""
        session_module = importlib.import_module("vcpi.sessions.persistence")
        p = Path(path) if path else self.session_path
        session_module.restore(self, p, start_audio=start_audio, skip_settings=skip_settings)

    # -- shutdown ------------------------------------------------------------

    def _cancel_note_timers(self):
        with self._note_lock:
            pending = list(self._note_timers.items())
            self._note_timers.clear()
        for timer, _ in pending:
            timer.cancel()
        for timer, (track_index, off, generation) in pending:
            timer.join()
            track = self.engine.tracks[track_index]
            if track is not None and track.generation == generation:
                try:
                    self.engine.enqueue_midi(track_index, off, generation=generation)
                except Exception:
                    logger.exception("[Host] Failed sending final note-off")

    def _close_resources(self):
        """Try every cleanup even when another subsystem has failed."""
        actions = [self._stop_deck_clock, self._cancel_note_timers]
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
        beatstep = getattr(self, "beatstep", None)
        if beatstep is not None:
            actions.append(beatstep.close)
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
