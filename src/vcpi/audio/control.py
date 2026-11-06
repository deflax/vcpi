"""Control-only facade for native output, mixing and isolated track DSP."""

from __future__ import annotations

import collections
import logging
import math
import threading
import time
from contextlib import contextmanager

from vcpi.audio_deps import require_native
from vcpi.audio_state import plain_value
from vcpi.models import InstrumentTrack, NUM_TRACKS


logger = logging.getLogger(__name__)


class _Control:
    __slots__ = ("operation", "arguments", "done", "result", "error", "cancelled")

    def __init__(self, operation, arguments, synchronous):
        self.operation = operation
        self.arguments = arguments
        self.done = threading.Event() if synchronous else None
        self.result = self.error = None
        self.cancelled = False


class AudioEngine:
    """Python owns commands and snapshots; native code owns every audio thread."""

    _track_fields = frozenset(
        ("gain", "muted", "solo", "enabled", "source_type", "vcv_patch_path", "pd_patch_path"))
    _gain_log_quiet_seconds = 0.3

    def __init__(self, sample_rate=44100, buffer_size=512, output_channels=2,
                 health_buffer=None, *, track_mode=False):
        if output_channels != 2:
            raise ValueError("native audio output requires two channels")
        backend = require_native()
        self.sample_rate = sample_rate
        self.buffer_size = buffer_size
        self.output_channels = output_channels
        self._native = None if track_mode else backend.Engine(sample_rate, buffer_size)
        self.runner = None
        self.tracks = [None] * NUM_TRACKS
        self.master_effects = []
        self.master_gain = 1.0
        self.isolated_tracks = not track_mode
        self.channel_map = {}
        self.parameter_values = {}
        self.parameter_readers = {}
        self._midi_queue = collections.deque()
        self._control_queue = collections.deque()
        self._applied_updates = collections.deque()
        self._pending_gain_logs = {}
        self._control_lock = threading.Lock()
        self._lock = threading.RLock()
        self._owner_thread = threading.get_ident()
        self._control_capacity = 4096
        self._control_timeout = 2.0
        self.control_error = None
        self.stream_error = None
        self.health_buffer = health_buffer
        self._running = False
        self._closed = False
        self._output_device = None
        self.render_workers = NUM_TRACKS
        self.render_tracks = self.eligible_tracks = 0
        self.underruns = self.render_errors = self.callback_count = 0
        self.last_callback_ms = self.peak_callback_ms = 0.0
        self.deadline_misses = 0
        self.block_period_ms = buffer_size * 1000.0 / sample_rate
        self.backend_label = "not opened"
        self.output_name = None
        self.output_index = None
        self.output_realtime = {}
        self.refresh_health()

    def route(self, midi_channel, track_index):
        if not 0 <= midi_channel < 16:
            raise ValueError("MIDI channel must be 1-16")
        if not 0 <= track_index < NUM_TRACKS:
            raise ValueError(f"track must be 1-{NUM_TRACKS}")
        previous = self.channel_map.get(midi_channel)
        if previous is not None and self.tracks[previous] is not None:
            self._clear_deck_nudge(previous)
            self.tracks[previous].midi_channels.discard(midi_channel)
        self.channel_map[midi_channel] = track_index
        if self.tracks[track_index] is not None:
            self.tracks[track_index].midi_channels.add(midi_channel)

    def unroute(self, midi_channel):
        index = self.channel_map.pop(midi_channel, None)
        if index is not None and self.tracks[index] is not None:
            self._clear_deck_nudge(index)
            self.tracks[index].midi_channels.discard(midi_channel)

    def _clear_deck_nudge(self, index):
        track = self.tracks[index]
        if track.source_type != "deck":
            return
        if track.render_owner is not None:
            track.render_owner.request("set_parameter", name="nudge", value=0)
        else:
            self.set_parameter(index, "nudge", 0)

    def _control_track(self, index):
        if not isinstance(index, int) or not 0 <= index < NUM_TRACKS:
            raise ValueError(f"track index must be 0-{NUM_TRACKS - 1}")
        track = self.tracks[index]
        if track is None:
            raise ValueError(f"track {index + 1} is empty")
        return track

    def _send_midi(self, index, message):
        track = self.tracks[index]
        if track is None:
            return
        worker = track.render_owner
        if worker is None:
            raise RuntimeError("track has no isolated render owner")
        if getattr(worker, "failure", None) is not None:
            return
        try:
            worker.transport.send_midi(bytes(message.bytes()))
        except (BufferError, ValueError) as error:
            # Overflow/oversized MIDI invalidates this instrument's note state,
            # not the device or other tracks. Retain its visible failure snapshot.
            worker.failure = f"track MIDI admission failed: {error}"
            if self._native is not None:
                self._native.attach(index, None)

    def enqueue_midi(self, track_index, msg, generation=None):
        if not isinstance(track_index, int) or not 0 <= track_index < NUM_TRACKS:
            raise ValueError(f"track index must be 0-{NUM_TRACKS - 1}")
        with self._control_lock:
            if self._closed:
                raise RuntimeError("audio engine is shut down")
            track = self.tracks[track_index]
            if generation is not None and generation != (track.generation if track else 0):
                return False
            if self._running:
                self._send_midi(track_index, msg)
            else:
                if len(self._midi_queue) >= self._control_capacity:
                    raise BufferError("audio MIDI queue is full")
                self._midi_queue.append((track_index, msg))
        return True

    @property
    def pending_control_count(self):
        return len(self._control_queue)

    def _submit_control(self, operation, arguments, synchronous):
        control = _Control(operation, arguments, synchronous)
        with self._control_lock:
            if self._closed:
                raise RuntimeError("audio engine is shut down")
            if len(self._control_queue) >= self._control_capacity:
                raise BufferError("audio control queue is full")
            self._control_queue.append(control)
        if not synchronous:
            return
        if threading.get_ident() == self._owner_thread:
            self.apply_pending_controls()
        if not control.done.wait(self._control_timeout):
            with self._control_lock:
                control.cancelled = True
            raise TimeoutError("audio control acknowledgement timed out")
        if control.error is not None:
            raise control.error
        return control.result

    def _apply_control(self, control):
        operation, arguments = control.operation, control.arguments
        if operation == "publish_track":
            index, track = arguments
            if self._native is not None:
                transport = track.render_owner.transport if track is not None else None
                self._native.attach(index, transport, track.gain if track else 1.0,
                                    track.muted if track else False, track.solo if track else False)
            with self._control_lock:
                retired = self.tracks[index]
                self.tracks[index] = track
                self._midi_queue = collections.deque(
                    item for item in self._midi_queue if item[0] != index)
            self._pending_gain_logs.pop(index, None)
            return retired
        if operation == "parameter":
            index, fx_index, name, value, *guard = arguments
            if guard and guard[0] is not None:
                current = self.tracks[index]
                if current is None or current.generation != guard[0]:
                    return
            if index is None:
                if fx_index is None:
                    raise ValueError("master parameter requires an effect index")
                effects = self.master_effects
            else:
                track = self._control_track(index)
                if self.isolated_tracks:
                    raise RuntimeError("isolated track parameters must be changed by the track owner")
                effects = track.effects
            if fx_index is not None:
                if not isinstance(fx_index, int) or not 0 <= fx_index < len(effects):
                    raise ValueError("effect index out of range")
                plugin = effects[fx_index]
            else:
                plugin = track.plugin
            setattr(plugin, name, value)
            actual = plain_value(getattr(plugin, name))
            self.parameter_values.setdefault(id(plugin), {})[name] = actual
            raw = display = None
            try:
                descriptor = (plugin.parameter_descriptor(name)
                              if hasattr(plugin, "parameter_descriptor")
                              else plugin.parameters[name])
                raw, display = descriptor.raw_value, descriptor.string_value
            finally:
                self._applied_updates.append(
                    ("parameter", index, fx_index, name, actual, raw, display))
            return actual
        if operation == "deck":
            index, action, value, generation = arguments
            track = self._control_track(index)
            if generation is not None and generation != track.generation:
                return
            if track.source_type != "deck":
                raise ValueError(f"track {index + 1} is not a deck")
            if track.render_owner is not None:
                return track.render_owner.request(
                    "deck_command", action=action, value=value).get("result")
            return track.plugin.native.deck_command(action, value)
        if operation == "deck_clock":
            index, bpm, beat, sampled_at, generation = arguments
            track = self._control_track(index)
            if generation is not None and generation != track.generation:
                return
            if track.source_type != "deck":
                raise ValueError(f"track {index + 1} is not a deck")
            if track.render_owner is not None:
                return track.render_owner.request(
                    "set_deck_clock", bpm=bpm, beat=beat, sampled_at=sampled_at).get("result")
            return track.plugin.native.set_deck_clock(bpm, beat, sampled_at)
        if operation == "barrier":
            if self.control_error is not None:
                raise self.control_error
            if self._native is not None:
                self._native.barrier()
            if self.runner is not None:
                self.runner.barrier()
            return
        raise ValueError(f"unknown audio control: {operation}")

    def _apply_mixer_controls(self, controls):
        """Publish adjacent mixer edits with one callback acknowledgment."""
        states = [(track.gain, track.muted, track.solo) if track is not None
                  else (1.0, False, False) for track in self.tracks]
        master = self.master_gain
        updates = {}
        master_changed = False

        def gain(value):
            value = float(value)
            if not math.isfinite(value) or not 0 <= value <= 3.4028234663852886e38:
                raise ValueError("Gain must be a finite nonnegative float")
            return value

        for control in controls:
            try:
                if control.operation == "track":
                    index, fields = control.arguments
                    self._control_track(index)
                    unknown = fields.keys() - self._track_fields
                    if unknown:
                        raise TypeError(f"unsupported track fields: {', '.join(sorted(unknown))}")
                    applied = {name: (gain(value) if name == "gain" else bool(value)
                               if name in ("muted", "solo", "enabled") else str(value))
                               for name, value in fields.items()}
                    old_gain, muted, solo = states[index]
                    states[index] = (applied.get("gain", old_gain),
                                     applied.get("muted", muted), applied.get("solo", solo))
                    updates.setdefault(index, {}).update(applied)
                else:
                    value = gain(control.arguments[0])
                    master = value
                    master_changed = True
            except Exception as error:
                control.error = error
        if not updates and not master_changed:
            return
        if self._native is not None:
            self._native.set_mixer(states, master)
        deadline = time.monotonic() + self._gain_log_quiet_seconds
        # Cached state and completion notifications remain applied-only: publish
        # them after the entire snapshot has reached the native callback.
        for index, fields in updates.items():
            track = self.tracks[index]
            for name, value in fields.items():
                setattr(track, name, value)
            self._applied_updates.append(("track", index, fields))
            if "gain" in fields:
                self._pending_gain_logs[index] = (track.gain, deadline)
        if master_changed:
            self.master_gain = master
            self._applied_updates.append(("master_gain", master))
            self._pending_gain_logs[None] = (master, deadline)

    def _flush_gain_logs(self, *, force=False):
        now = time.monotonic()
        for index, (gain, deadline) in tuple(self._pending_gain_logs.items()):
            if not force and now < deadline:
                continue
            if index is None:
                logger.info("master gain -> %.2f (applied)", gain)
            else:
                logger.info("track %d gain -> %.2f (applied)", index + 1, gain)
            del self._pending_gain_logs[index]

    def apply_pending_controls(self):
        """Apply FIFO controls on the control owner, including during playback."""
        if threading.get_ident() != self._owner_thread:
            raise RuntimeError("pending controls must be applied by the audio owner")
        with self._lock:
            remaining = len(self._control_queue)
            while remaining:
                with self._control_lock:
                    control = self._control_queue.popleft()
                    remaining -= 1
                    controls = [control]
                    mixer = control.operation in ("track", "master_gain")
                    if mixer:
                        while remaining and self._control_queue[0].operation in ("track", "master_gain"):
                            controls.append(self._control_queue.popleft())
                            remaining -= 1
                    controls = [item for item in controls if not item.cancelled]
                try:
                    if mixer:
                        self._apply_mixer_controls(controls)
                    elif controls:
                        control.result = self._apply_control(control)
                except Exception as error:
                    for item in controls:
                        if item.error is None:
                            item.error = error
                finally:
                    for item in controls:
                        if item.error is not None and item.done is None and self.control_error is None:
                            self.control_error = item.error
                        if item.done is not None:
                            item.done.set()
            if self.control_error is not None:
                raise self.control_error
            self._flush_gain_logs()

    def publish_track(self, index, track):
        if not isinstance(index, int) or not 0 <= index < NUM_TRACKS:
            raise ValueError(f"track index must be 0-{NUM_TRACKS - 1}")
        return self._submit_control("publish_track", (index, track), True)

    def clear_track(self, index):
        return self.publish_track(index, None)

    def set_parameter(self, track_index, name, value, fx_index=None):
        return self._submit_control("parameter", (track_index, fx_index, name, value), True)

    def deck_command(self, track_index, action, value=0):
        return self._submit_control("deck", (track_index, action, value, None), True)

    def enqueue_deck_command(self, track_index, action, value=0, generation=None):
        self._submit_control("deck", (track_index, action, value, generation), False)

    def set_deck_clock(self, track_index, bpm, beat, sampled_at):
        return self._submit_control(
            "deck_clock", (track_index, bpm, beat, sampled_at, None), True)

    def enqueue_deck_clock(self, track_index, bpm, beat, sampled_at, generation=None):
        self._submit_control(
            "deck_clock", (track_index, bpm, beat, sampled_at, generation), False)

    def load_plugin_state(self, track_index, state, fx_index=None):
        """Restore an opaque chunk on its native control owner."""
        from vcpi.state_transport import validate_native_state

        validate_native_state(state)
        if threading.get_ident() != self._owner_thread:
            raise RuntimeError("plugin state must be loaded by the audio owner")
        if track_index is None:
            if self.running:
                raise RuntimeError("master plugin state requires stopped audio")
            effects = self.master_effects
            if fx_index is None:
                raise ValueError("master plugin state requires an effect index")
        else:
            track = self._control_track(track_index)
            if self.isolated_tracks:
                raise RuntimeError("isolated plugin state must be loaded by the track owner")
            effects = track.effects
        if fx_index is not None:
            if not isinstance(fx_index, int) or not 0 <= fx_index < len(effects):
                raise ValueError("effect index out of range")
            plugin = effects[fx_index]
        else:
            plugin = track.plugin
        if self.runner is not None:
            self.runner.barrier()
        plugin.load_saved_state(state)

    def set_track_state(self, track_index, **fields):
        return self._submit_control("track", (track_index, fields), True)

    def set_master_gain(self, value):
        return self._submit_control("master_gain", (value,), True)

    def enqueue_param_change(self, track_index, param_name, value, fx_index=None,
                             generation=None):
        self._submit_control("parameter",
                             (track_index, fx_index, param_name, value, generation), False)

    def enqueue_track_change(self, index, **fields):
        self._submit_control("track", (index, fields), False)

    def enqueue_master_gain(self, value):
        self._submit_control("master_gain", (value,), False)

    def flush_controls(self):
        return self._submit_control("barrier", (), True)

    def any_solo(self):
        return any(track.solo for track in self.tracks if track is not None)

    def set_effects(self, effects, track_index=None):
        plugins = [effect.native for effect in effects]
        if track_index is None:
            self._native.set_master_effects(plugins)
            self.master_effects = list(effects)
        else:
            if self.runner is None:
                raise RuntimeError("track native runner is not initialized")
            self.runner.set_effects(plugins)
            self._control_track(track_index).effects = list(effects)

    @contextmanager
    def edit_effects(self):
        with self._lock:
            resume, device = self._running, self._output_device
            if resume:
                self.stop()
            if self.runner is not None:
                self.runner.barrier()
            try:
                yield
            finally:
                if resume:
                    self.start(device)

    def start(self, output_device=None):
        with self._lock:
            if self._closed:
                raise RuntimeError("audio engine is shut down")
            if self._native is None:
                raise RuntimeError("track owner cannot open an output device")
            self.stop()
            self.apply_pending_controls()
            while self._midi_queue:
                index, message = self._midi_queue[0]
                self._send_midi(index, message)
                self._midi_queue.popleft()
            try:
                self._native.start(output_device)
            except require_native().OwnershipError as error:
                # Failed device teardown cannot establish callback quiescence.
                # Supervision terminates this owner instead of reusing it.
                self.stream_error = error
                raise
            self._output_device = output_device
            self.refresh_health()

    def stop(self):
        with self._lock:
            try:
                if self._native is not None:
                    self._native.stop()
            except BaseException as error:
                self.stream_error = error
                raise
            self.refresh_health()

    def refresh_health(self):
        """Copy native counters on the control tick, never from an audio callback."""
        if self._native is not None:
            stats = self._native.stats()
            self._running = bool(stats["running"])
            for name in ("eligible_tracks", "underruns", "render_errors", "callback_count",
                         "last_callback_ms", "peak_callback_ms", "deadline_misses",
                         "block_period_ms", "backend_label", "output_name", "output_index"):
                if name in stats:
                    setattr(self, name, stats[name])
            self.output_realtime = stats["realtime"]
        self.render_tracks = sum(track is not None for track in self.tracks)
        self._publish_health()

    def _publish_health(self):
        if self.health_buffer is not None:
            for index, value in enumerate((self._running, self.eligible_tracks, self.underruns,
                    self.render_errors, self.callback_count, self.last_callback_ms,
                    self.peak_callback_ms, self.deadline_misses, self.block_period_ms)):
                self.health_buffer[index] = float(value)

    def stats(self):
        self.refresh_health()
        return self._native.stats() if self._native is not None else {"running": False}

    def render_block(self):
        self.apply_pending_controls()
        while self._midi_queue:
            index, message = self._midi_queue[0]
            self._send_midi(index, message)
            self._midi_queue.popleft()
        result = self._native.render_block()
        self.refresh_health()
        return result

    def shutdown(self):
        with self._lock:
            if self._closed:
                return
            if self.runner is not None:
                self.runner.shutdown()
                self.runner = None
            if self._native is not None:
                self._native.shutdown()
            self._flush_gain_logs(force=True)
            self._closed = True
            self._running = False
            self.master_effects.clear()
            self.tracks[:] = [None] * NUM_TRACKS
            self._midi_queue.clear()
            with self._control_lock:
                while self._control_queue:
                    control = self._control_queue.popleft()
                    control.error = RuntimeError("audio engine is shut down")
                    if control.done is not None:
                        control.done.set()
            self._applied_updates.clear()
            self.parameter_values.clear()
            self.parameter_readers.clear()
            self.channel_map.clear()
            self._publish_health()

    @property
    def running(self):
        return self._running
