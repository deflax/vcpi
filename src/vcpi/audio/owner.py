"""Output-owner dispatcher and spawned native-audio bootstrap."""

from __future__ import annotations

import logging
import os
import pickle
import signal
import time
from collections import deque

from vcpi.audio_state import apply_deck_status, capture_state
from vcpi.deck_bank import encode_deck_message, encode_deck_update
from vcpi.logging_setup import configure_logging, set_log_level
from vcpi.models import NUM_TRACKS
from vcpi.native_output import native_output
from vcpi.state_transport import (
    encode_native_message, receive_frame, send_frame, validate_native_message,
)

from .protocol import (
    CONTROL_LIMIT, INPUT_CAPACITY, INPUT_LIMIT, LIFECYCLE_OPERATIONS,
    STATE_LIMIT, UPDATE_LIMIT, _encode,
)

logger = logging.getLogger(__name__)


class _OwnerCancelled(BaseException):
    """Unwind native ownership on deliberate control-process interruption."""


def _discover_devices():
    from vcpi import audio_deps

    if not audio_deps.HAS_NATIVE:
        return []
    devices = []
    apis = audio_deps.native.hostapis()
    for index, info in enumerate(audio_deps.native.devices()):
        entry = dict(info)
        entry.setdefault("index", index)
        try:
            entry["hostapi_name"] = str(apis[entry["hostapi"]]["name"])
        except Exception:
            entry["hostapi_name"] = "unknown"
        devices.append(entry)
    return devices


class _OwnerRuntime:
    """Output dispatcher; only native master/device edits quiesce the stream."""

    _parameter_log_quiet_seconds = 0.3

    def __init__(self, owner, control, inputs, updates, cancelled):
        self.owner = owner
        self.engine = owner.engine
        self.control = control
        self.inputs = inputs
        self.updates = updates
        self.epoch = 0
        self.cancelled = cancelled
        self.generations = [0] * NUM_TRACKS
        self.previous_tracks = [None] * NUM_TRACKS
        self.received_input = 0
        self.update_serial = 0
        self.devices = _discover_devices()
        self.pending_parameters = deque()
        self._pending_parameter_logs = {}
        self.master_snapshots = []
        self._last_track_health = 0.0
        self._last_output_realtime = None
        self.benchmark_revision = 0
        try:
            import mido
        except ImportError:
            mido = None
        self.mido = mido

    def pending(self):
        return (self.engine.pending_control_count + len(self.engine._midi_queue)
                + len(self.engine._applied_updates) + len(self.pending_parameters))

    def receive_input(self):
        sequence, kind, index, generation, payload = pickle.loads(
            self.inputs.recv_bytes(INPUT_LIMIT))
        if sequence != self.received_input + 1:
            raise RuntimeError("audio input sequence is discontinuous")
        self.received_input = sequence
        if index is not None:
            if not 0 <= index < NUM_TRACKS:
                raise ValueError("invalid audio input track")
            if generation != self.generations[index]:
                return  # Retired instrument: never deliver its events to a replacement.
        if kind == "midi":
            if self.mido is None:
                raise RuntimeError("mido not installed")
            self.engine.enqueue_midi(index, self.mido.Message.from_dict(payload),
                                     generation=generation)
        elif kind == "parameter":
            name, value, fx_index, fx_token = payload
            if index is None:
                if fx_token is not None:
                    effects = self.engine.master_effects
                    if (not isinstance(fx_index, int) or not 0 <= fx_index < len(effects)
                            or id(effects[fx_index]) != fx_token):
                        return
                self.engine.enqueue_param_change(index, name, value, fx_index=fx_index)
            else:
                self.pending_parameters.append((index, generation, name, value,
                                                "parameter", fx_index, fx_token))
        elif kind == "deck":
            self.pending_parameters.append(
                (index, generation, payload[0], payload[1], "deck", None, None))
        elif kind == "deck_clock":
            self.pending_parameters.append(
                (index, generation, None, payload, "deck_clock", None, None))
        elif kind == "track":
            self.engine.enqueue_track_change(index, **payload)
        elif kind == "master_gain":
            self.engine.enqueue_master_gain(payload)
        else:
            raise ValueError(f"unknown audio input kind: {kind}")

    def await_inputs(self, sequence):
        deadline = time.monotonic() + 5
        while self.received_input < sequence:
            self.check_cancelled()
            self.engine.apply_pending_controls()
            self.engine.refresh_health()
            self.send_updates()
            self.apply_track_parameters()
            if self.pending() >= INPUT_CAPACITY:
                if not self.engine.running:
                    raise BufferError("audio input backlog is full while audio is stopped")
                time.sleep(0.001)
            elif self.inputs.poll(0.01):
                for _ in range(64):
                    self.receive_input()
                    if (self.received_input >= sequence
                            or self.pending() >= INPUT_CAPACITY
                            or not self.inputs.poll()):
                        break
            if time.monotonic() >= deadline:
                raise TimeoutError("audio input ordering barrier timed out")

    def send_updates(self):
        if self.engine.control_error is not None:
            raise RuntimeError(f"audio control failed: {self.engine.control_error}")
        pending = self.engine._applied_updates
        while pending:
            batch = [pending.popleft() for _ in range(min(64, len(pending)))]
            batches = deque([batch])
            while batches:
                current = batches.popleft()
                serial = self.update_serial + 1
                try:
                    data = _encode(("updates", self.epoch, serial, current), UPDATE_LIMIT)
                except BufferError:
                    if len(current) == 1:
                        data = encode_deck_update(
                            ("updates", self.epoch, serial, current), UPDATE_LIMIT, STATE_LIMIT)
                    else:
                        middle = len(current) // 2
                        batches.appendleft(current[middle:])
                        batches.appendleft(current[:middle])
                        continue
                send_frame(self.updates, data, UPDATE_LIMIT)
                self._cache_master_updates(current)
                self.update_serial = serial

    def _cache_master_updates(self, updates):
        for update in updates:
            if update[0] != "parameter" or update[1] is not None:
                continue
            _, _, fx_index, name, actual, raw, display = update
            if fx_index is None or not 0 <= fx_index < len(self.master_snapshots):
                continue
            plugin = self.master_snapshots[fx_index]
            plugin.values[name] = actual
            descriptor = plugin.parameters.get(name)
            if descriptor is not None:
                if raw is not None:
                    descriptor.raw_value = raw
                descriptor.string_value = str(actual) if display is None else display

    def _track_worker(self, index):
        if not isinstance(index, int) or not 0 <= index < NUM_TRACKS:
            raise ValueError("invalid track index")
        track = self.engine.tracks[index]
        if track is None:
            raise ValueError(f"track {index + 1} is empty")
        return track.render_owner

    def service_wait(self):
        """Keep MIDI and mixer controls moving while another track initializes."""
        self.check_cancelled()
        self.engine.apply_pending_controls()
        self.engine.refresh_health()
        self.send_updates()
        for _ in range(64):
            if self.pending() >= INPUT_CAPACITY or not self.inputs.poll():
                break
            self.receive_input()
        self.publish_track_health()

    def check_cancelled(self):
        if self.cancelled.value:
            raise _OwnerCancelled


    def track_request(self, index, operation, **arguments):
        worker = self._track_worker(index)
        reply = worker.request(operation, wait_hook=self.service_wait, **arguments)
        track = self.engine.tracks[index]
        track.plugin = worker.track.plugin
        track.effects = worker.track.effects
        track.deck = worker.track.deck
        self.engine._applied_updates.extend(reply.get("updates", ()))
        if reply.get("deck") is not None:
            self.engine._applied_updates.append(
                ("deck", index, self.generations[index], reply["deck"]))
        return reply.get("result")

    def apply_track_parameters(self):
        # Bound command work per dispatcher pass; never process these on the
        # mixer callback or inspect a processing plugin from the output owner.
        count = min(64, len(self.pending_parameters))
        for _ in range(count):
            index, generation, name, value, kind, fx_index, fx_token = self.pending_parameters.popleft()
            if generation != self.generations[index]:
                continue
            if fx_token is not None:
                # A lifecycle request can receive inputs in service_wait before
                # refreshing the chain. Check identity here, after that refresh,
                # not when the input first arrives or against a stale index.
                track = self.engine.tracks[index]
                if (track is None or not isinstance(fx_index, int)
                        or not 0 <= fx_index < len(track.effects)
                        or track.effects[fx_index].token != fx_token):
                    continue
            try:
                if kind == "deck":
                    self.track_request(index, "deck_command", action=name, value=value)
                    continue
                if kind == "deck_clock":
                    bpm, beat, sampled_at = value
                    self.track_request(index, "set_deck_clock",
                                       bpm=bpm, beat=beat, sampled_at=sampled_at)
                    continue
                actual = self.track_request(index, "set_parameter", name=name, value=value,
                                            fx_index=fx_index)
                self._pending_parameter_logs[index, fx_index, name] = (
                    generation, actual, time.monotonic() + self._parameter_log_quiet_seconds)
            except Exception as error:
                worker = self._track_worker(index)
                worker.failure = f"parameter update failed: {error}"
        self.send_updates()
        self._flush_parameter_logs()

    def _flush_parameter_logs(self, *, force=False):
        now = time.monotonic()
        for (index, fx_index, name), (generation, actual, deadline) in tuple(
                self._pending_parameter_logs.items()):
            if generation != self.generations[index]:
                del self._pending_parameter_logs[index, fx_index, name]
                continue
            if not force and now < deadline:
                continue
            target = f"track {index + 1}" + (f" fx {fx_index + 1}" if fx_index is not None else "")
            logger.info("%s %s -> %s (applied)", target, name, actual)
            del self._pending_parameter_logs[index, fx_index, name]

    def publish_track_health(self):
        now = time.monotonic()
        self.engine.refresh_health()
        if now - self._last_track_health < 0.1:
            return
        self._last_track_health = now
        realtime = self.engine.output_realtime
        if realtime != self._last_output_realtime:
            self._last_output_realtime = realtime
            self.engine._applied_updates.append(("output_realtime", realtime))
        for index, track in enumerate(self.engine.tracks):
            if track is None:
                continue
            worker = track.render_owner
            worker.poll_failure()
            status = worker.poll_deck_status() if track.source_type == "deck" else None
            if status is not None:
                apply_deck_status(track, status)
                self.engine._applied_updates.append(
                    ("deck", index, self.generations[index], status))
            fields = {"worker_pid": worker.pid, "worker_failure": worker.failure,
                      "worker_misses": worker.misses,
                      "worker_realtime": worker.realtime,
                      "buffering_samples": self.engine.buffer_size}
            changed = {key: value for key, value in fields.items()
                       if getattr(track, key) != value}
            if changed:
                for key, value in changed.items():
                    setattr(track, key, value)
                self.engine._applied_updates.append(("track", index, changed))

    def snapshot(self, inspect_master=False, *, capture_native_state=False):
        self.engine.refresh_health()
        state = capture_state(
            self.owner, self.generations, self.epoch, self.devices,
            cached_master_effects=None if inspect_master else self.master_snapshots,
            capture_native_state=capture_native_state)
        if inspect_master:
            self.master_snapshots = state["master_effects"]
        return state

    def state_reply(self, result=None, inspect_master=False, *, capture_native_state=False):
        self.send_updates()
        self.epoch += 1
        state = self.snapshot(inspect_master, capture_native_state=capture_native_state)
        reply = {"ok": True, "result": result, "state": state,
                 "update_serial": self.update_serial}
        return (encode_native_message(reply, STATE_LIMIT) if capture_native_state
                else _encode(reply, STATE_LIMIT))

    def shutdown_tracks(self):
        self._flush_parameter_logs(force=True)
        for index, track in enumerate(self.engine.tracks):
            if track is not None and track.render_owner is not None:
                try:
                    self.engine.clear_track(index)
                    track.render_owner.shutdown()
                except Exception:
                    logger.exception("track worker shutdown failed")

    def refresh_generations(self):
        changed = set()
        for index, track in enumerate(self.engine.tracks):
            if track is not self.previous_tracks[index]:
                self.generations[index] += 1
                if track is not None:
                    track.generation = self.generations[index]
                self.previous_tracks[index] = track
                changed.add(index)
        if changed:
            # Inputs already queued for a retired instance must not play the
            # replacement when a stream starts/resumes after a lifecycle edit.
            self.engine._midi_queue = deque(
                (index, message) for index, message in self.engine._midi_queue
                if index not in changed)

    def audio_info(self):
        return {
            "backend_label": self.engine.backend_label,
            "audio_output_name": self.owner.audio_output_name,
            "audio_output_index": self.engine.output_index,
            "audio_devices": self.devices,
        }

    def diagnose(self):
        """Read native signal counters and cached mixer metadata without plugin IPC."""
        output = dict(self.engine.stats())
        output["master_gain"] = float(self.engine.master_gain)
        tracks = []
        for index, track in enumerate(self.engine.tracks):
            if track is None:
                continue
            worker = track.render_owner
            stats = dict(worker.transport.stats())
            stats.update({
                "index": index + 1,
                "name": track.name,
                "gain": float(track.gain),
                "muted": bool(track.muted),
                "solo": bool(track.solo),
                "worker_failure": worker.failure,
                "generation": self.generations[index],
                "transport_path": worker.transport_path,
                "worker_pid": worker.pid,
            })
            tracks.append(stats)
        return {"output": output, "tracks": tracks}

    def benchmark(self, enabled=None):
        """Opt-in native counters only; never wait for a measurement window."""
        if enabled is not None:
            if type(enabled) is not bool:
                raise ValueError("timing enabled must be a boolean")
            transports = [track.render_owner.transport for track in self.engine.tracks
                          if track is not None]
            errors = []
            for transport in transports:
                try:
                    transport.set_timing_enabled(enabled)
                except Exception as exc:
                    errors.append(exc)
            if errors:
                if enabled:
                    # Roll back every transport, including a setter that failed
                    # after changing its shared flag.
                    for transport in transports:
                        try:
                            transport.set_timing_enabled(False)
                        except Exception:
                            logger.exception("benchmark timing rollback failed")
                raise RuntimeError(f"cannot set benchmark timing: {errors[0]}")
        snapshot = self.diagnose()
        snapshot["config"] = {
            "sample_rate": self.engine.sample_rate,
            "buffer_size": self.engine.buffer_size,
            "output_channels": self.engine.output_channels,
            "output_index": self.engine.output_index,
            "revision": self.benchmark_revision,
            "owner_pid": os.getpid(),
        }
        return snapshot

    def execute(self, operation, arguments):
        if operation in LIFECYCLE_OPERATIONS or operation in {
            "start_audio", "stop_audio", "load_plugin_state",
        }:
            # Preserve evidence of stop/restart or replace/restore between samples.
            self.benchmark_revision += 1
        if operation in {
            "restore_patch", "load_vcv", "load_vcv_patch",
            "load_pd", "load_pd_patch", "load_deck",
        }:
            from .track_client import TrackWorker

            index = arguments["track_index"]
            if not isinstance(index, int) or not 0 <= index < NUM_TRACKS:
                raise ValueError("invalid track index")
            staged = TrackWorker(
                self.engine.sample_rate, self.engine.buffer_size, index,
                operation, arguments, wait_hook=self.service_wait)
            published = False
            try:
                staged.track.render_owner = staged
                staged.track.worker_pid = staged.pid
                staged.track.buffering_samples = self.engine.buffer_size
                staged.track.worker_realtime = staged.transport.stats()["realtime"]
                staged.track.midi_channels = {
                    channel for channel, target in self.engine.channel_map.items()
                    if target == index}
                retired = self.engine.publish_track(index, staged.track)
                published = True
                self.refresh_generations()
                if retired is not None:
                    retired.render_owner.shutdown()
                return self.state_reply(staged.result)
            finally:
                if not published:
                    staged.shutdown()
        if operation == "remove_instrument":
            index = arguments["track_index"]
            self._track_worker(index)
            retired = self.engine.clear_track(index)
            self.refresh_generations()
            retired.render_owner.shutdown()
            return self.state_reply()
        if operation in {"load_effect", "remove_effect"} and arguments.get("track_index") is not None:
            index = arguments["track_index"]
            forwarded = {key: value for key, value in arguments.items() if key != "track_index"}
            self.track_request(index, operation, **forwarded)
            return self.state_reply()
        if operation == "update_deck_catalog":
            index = arguments["track_index"]
            forwarded = {key: value for key, value in arguments.items() if key != "track_index"}
            self.track_request(index, operation, **forwarded)
            return self.state_reply()
        if operation == "load_plugin_state":
            index = arguments.get("track_index")
            if index is not None:
                forwarded = {key: value for key, value in arguments.items()
                             if key != "track_index"}
                result = self.track_request(index, operation, **forwarded)
                return self.state_reply(result)
            with self.engine.edit_effects():
                self.engine.apply_pending_controls()
                self.send_updates()
                self.engine.load_plugin_state(**arguments)
                state = self.snapshot(inspect_master=True)
                result = state["master_effects"][arguments["fx_index"]]
                return self.state_reply(result)
        if operation == "sync_state":
            if self.engine.running:
                self.engine.flush_controls()
                deadline = time.monotonic() + 2
                while self.engine._midi_queue:
                    self.check_cancelled()
                    if time.monotonic() >= deadline:
                        raise TimeoutError("MIDI state capture barrier timed out")
                    self.send_updates()
                    time.sleep(0.001)
            for index, track in enumerate(self.engine.tracks):
                if track is not None:
                    self.track_request(index, "sync_state")
            if not self.engine.master_effects:
                return self.state_reply(capture_native_state=True)
        if operation in LIFECYCLE_OPERATIONS or operation == "sync_state":
            # Only native master-bus inspection/chain edits still need the
            # entire device stopped. Track native work lives in its own child.
            with self.engine.edit_effects():
                self.engine.apply_pending_controls()
                self.send_updates()
                if operation != "sync_state":
                    getattr(self.owner, operation)(**arguments)
                return self.state_reply(
                    inspect_master=True, capture_native_state=operation == "sync_state")
        if operation == "start_audio":
            self.engine.stop()
            self.engine.apply_pending_controls()
            self.send_updates()
            self.devices = _discover_devices()
            self.owner.start_audio(**arguments)
            result = None
        elif operation == "stop_audio":
            self.owner.stop_audio()
            self.engine.apply_pending_controls()
            self.devices = _discover_devices()
            result = None
        elif operation == "set_parameter":
            index = arguments.get("track_index")
            if index is None:
                result = self.engine.set_parameter(**arguments)
            else:
                forwarded = {key: value for key, value in arguments.items() if key != "track_index"}
                result = self.track_request(index, operation, **forwarded)
        elif operation == "deck_command":
            result = self.track_request(arguments["track_index"], operation,
                                        action=arguments["action"],
                                        value=arguments.get("value", 0))
        elif operation == "set_deck_clock":
            result = self.track_request(arguments["track_index"], operation,
                                        bpm=arguments["bpm"], beat=arguments["beat"],
                                        sampled_at=arguments["sampled_at"])
        elif operation == "set_parameters":
            index = arguments.get("track_index")
            if index is not None:
                forwarded = {key: value for key, value in arguments.items() if key != "track_index"}
                result = self.track_request(index, operation, **forwarded)
            else:
                if len(arguments["params"]) > 64:
                    raise ValueError("parameter batch exceeds 64 entries")
                values, errors = {}, {}
                for name, value in arguments["params"].items():
                    self.check_cancelled()
                    try:
                        values[name] = self.engine.set_parameter(
                            None, name, value, fx_index=arguments.get("fx_index"))
                    except (TimeoutError, BufferError):
                        raise
                    except Exception as error:
                        errors[name] = str(error)
                result = {"values": values, "errors": errors}
        elif operation == "set_track_state":
            self.engine.set_track_state(**arguments)
            result = None
        elif operation == "set_master_gain":
            self.engine.set_master_gain(**arguments)
            result = float(self.engine.master_gain)
        elif operation == "route":
            self.engine.route(**arguments)
            result = dict(self.engine.channel_map)
        elif operation == "unroute":
            self.engine.unroute(**arguments)
            result = dict(self.engine.channel_map)
        elif operation == "diagnose":
            result = self.diagnose()
        elif operation == "benchmark":
            result = self.benchmark(**arguments)
        elif operation == "set_log_level":
            result = set_log_level(**arguments)
            for track in self.engine.tracks:
                worker = track.render_owner if track is not None else None
                if worker is not None and worker.failure is None:
                    worker.request("set_log_level", wait_hook=self.service_wait, **arguments)
        elif operation == "shutdown":
            if self.engine.running:
                self.engine.flush_controls()
            else:
                self.engine.apply_pending_controls()
            self.send_updates()
            self.shutdown_tracks()
            self.previous_tracks = [None] * NUM_TRACKS
            self.owner.shutdown()
            result = None
        else:
            raise ValueError(f"unknown audio operation: {operation}")
        self.send_updates()
        reply = {"ok": True, "result": result, "update_serial": self.update_serial}
        if operation in {"start_audio", "stop_audio"}:
            reply["audio"] = self.audio_info()
        return _encode(reply, STATE_LIMIT)

    def run(self):
        state = self.snapshot(inspect_master=True)
        self.control.send_bytes(_encode({"ok": True, "state": state}, STATE_LIMIT))
        while True:
            self.check_cancelled()
            self.engine.apply_pending_controls()
            self.engine.refresh_health()
            self.send_updates()
            self.apply_track_parameters()
            self.publish_track_health()
            if self.control.poll():
                request = receive_frame(self.control, CONTROL_LIMIT, native_state=True)
                operation = request["operation"]
                if operation == "load_plugin_state":
                    validate_native_message(request, CONTROL_LIMIT)
                elif operation in {"load_deck", "update_deck_catalog"}:
                    encode_deck_message(request, CONTROL_LIMIT)
                else:
                    _encode(request, CONTROL_LIMIT)
                try:
                    self.await_inputs(request["after"])
                    while self.pending_parameters:
                        self.apply_track_parameters()
                    if not self.engine.running:
                        self.engine.apply_pending_controls()
                    reply = self.execute(operation, request["arguments"])
                except Exception as error:
                    fatal = (isinstance(error, (TimeoutError, BufferError))
                             or self.engine.stream_error is not None)
                    reply = _encode({"ok": False, "error": str(error),
                                     "error_type": type(error).__name__, "fatal": fatal,
                                     "update_serial": self.update_serial}, STATE_LIMIT)
                    self.control.send_bytes(reply)
                    if fatal:
                        # A hung native setter cannot be canceled safely. Exit
                        # its ownership process instead of allowing late writes.
                        os._exit(1)
                    continue
                send_frame(self.control, reply, STATE_LIMIT)
                if operation == "shutdown":
                    return
            for _ in range(64):
                if self.pending() >= INPUT_CAPACITY or not self.inputs.poll():
                    break
                self.receive_input()
            # poll() releases the GIL; never spin while idle or compete with
            # rendering merely because a client is displaying diagnostics.
            self.control.poll(0.005)


def _audio_owner(sample_rate, buffer_size, health, control, inputs, updates, cancelled,
                 output):
    with native_output("audio", output):
        _run_audio_owner(sample_rate, buffer_size, health, control, inputs, updates,
                         cancelled)


def _run_audio_owner(sample_rate, buffer_size, health, control, inputs, updates, cancelled):
    owner = None
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    configure_logging()
    try:
        from vcpi.plugin_host import PluginHost

        owner = PluginHost(sample_rate=sample_rate, buffer_size=buffer_size)
        owner.engine.health_buffer = health
        owner.engine.isolated_tracks = True
        owner.engine.refresh_health()
        runtime = _OwnerRuntime(owner, control, inputs, updates, cancelled)
        runtime.run()
    except _OwnerCancelled:
        pass  # Deliberate cancellation still runs normal native/finalizer teardown.
    except (EOFError, BrokenPipeError):
        pass  # Control process exited; release native resources on this main thread.
    except Exception as error:
        try:
            updates.send_bytes(_encode(("failure", str(error)), UPDATE_LIMIT))
            control.send_bytes(_encode({"ok": False, "error": str(error),
                                       "error_type": type(error).__name__, "fatal": True,
                                       "update_serial": 0}, STATE_LIMIT))
        except (EOFError, OSError):
            pass
    finally:
        if owner is not None:
            try:
                owner.engine.stop()
            finally:
                if "runtime" in locals():
                    runtime.shutdown_tracks()
                owner.shutdown()
        control.close()
        inputs.close()
        updates.close()
