"""Spawned audio owner with bounded control/input channels and cached diagnostics.

The output child owns PortAudio and master effects; persistent track children own
instruments and inserts. The callback only exchanges fixed shared-memory blocks.
"""

from __future__ import annotations

import logging
import multiprocessing as mp
import os
import pickle
import queue
import signal
import threading
import time
from collections import deque
from itertools import islice
from multiprocessing.connection import wait as wait_connections

from core.audio_state import apply_deck_status, capture_state, hydrate_tracks
from core.deck_bank import encode_deck_message
from core.models import NUM_TRACKS
from core.logging_setup import configure_logging, set_log_level
from core.native_output import native_output, output_destination
from core.state_transport import (
    encode_native_message, receive_frame, send_frame, validate_native_message,
    validate_native_state,
)

logger = logging.getLogger(__name__)

INPUT_CAPACITY = 4096
CONTROL_LIMIT = 64 * 1024
STATE_LIMIT = 16 * 1024 * 1024
INPUT_LIMIT = 16 * 1024
UPDATE_LIMIT = 64 * 1024
CALLBACK_STALL_TIMEOUT = 2.0
OWNER_SHUTDOWN_TIMEOUT = 10.0
LIFECYCLE_OPERATIONS = frozenset({
    "load_instrument", "load_wav", "remove_instrument", "load_effect",
    "remove_effect", "load_vcv", "load_vcv_patch", "load_pd", "load_pd_patch", "load_deck",
})
REMOTE_ERRORS = {
    cls.__name__: cls for cls in
    (ValueError, TypeError, IndexError, KeyError, FileNotFoundError,
     PermissionError, RuntimeError, BufferError, TimeoutError)
}


class AudioProcessError(RuntimeError):
    """The audio owner is unavailable; no command was silently accepted."""

class _OwnerCancelled(BaseException):
    """Unwind native ownership on deliberate control-process interruption."""



def _encode(message, limit):
    data = pickle.dumps(message, protocol=pickle.HIGHEST_PROTOCOL)
    if len(data) > limit:
        raise BufferError(f"audio protocol message exceeds {limit} bytes")
    return data


def _discover_devices():
    from core import audio_deps

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
            if index is None:
                self.engine.enqueue_param_change(index, payload[0], payload[1])
            else:
                self.pending_parameters.append((index, generation, payload[0], payload[1]))
        elif kind == "deck":
            self.pending_parameters.append((index, generation, payload[0], payload[1], "deck"))
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
                        raise
                    middle = len(current) // 2
                    batches.appendleft(current[middle:])
                    batches.appendleft(current[:middle])
                    continue
                self.updates.send_bytes(data)
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
            index, generation, name, value, *kind = self.pending_parameters.popleft()
            if generation != self.generations[index]:
                continue
            try:
                if kind:
                    self.track_request(index, "deck_command", action=name, value=value)
                    continue
                actual = self.track_request(index, "set_parameter", name=name, value=value)
                self._pending_parameter_logs[index, name] = (
                    generation, actual, time.monotonic() + self._parameter_log_quiet_seconds)
            except Exception as error:
                worker = self._track_worker(index)
                worker.failure = f"parameter update failed: {error}"
        self.send_updates()
        self._flush_parameter_logs()

    def _flush_parameter_logs(self, *, force=False):
        now = time.monotonic()
        for (index, name), (generation, actual, deadline) in tuple(
                self._pending_parameter_logs.items()):
            if generation != self.generations[index]:
                del self._pending_parameter_logs[index, name]
                continue
            if not force and now < deadline:
                continue
            logger.info("track %d %s -> %s (applied)", index + 1, name, actual)
            del self._pending_parameter_logs[index, name]

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
                track.deck.update(status)
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
            })
            tracks.append(stats)
        return {"output": output, "tracks": tracks}

    def execute(self, operation, arguments):
        if operation in {
            "load_instrument", "load_wav", "load_vcv", "load_vcv_patch",
            "load_pd", "load_pd_patch", "load_deck",
        }:
            from core.track_process import TrackWorker

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
                elif operation == "load_deck":
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
        from core.plugin_host import PluginHost

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


class RemoteAudioEngine:
    """Control-process facade: local snapshots, bounded inputs, explicit RPCs."""

    def __init__(self, sample_rate=44100, buffer_size=512):
        self.sample_rate = sample_rate
        self.buffer_size = buffer_size
        self.output_channels = 2
        self.tracks = [None] * NUM_TRACKS
        self.master_effects = []
        self.master_gain = 1.0
        self.channel_map = {}
        self.backend_label = "not opened"
        self.output_realtime = {}
        self.audio_output_name = None
        self.audio_output_index = None
        self.audio_devices = []
        self.native_dependencies = {}
        self.render_workers = min(os.cpu_count() or 2, NUM_TRACKS)
        self._epoch = -1
        self._failure = None
        self._ready = False
        self._closing = False
        self._closed = False
        self._rpc_lock = threading.Lock()
        self._input_lock = threading.Lock()
        self._state_condition = threading.Condition()
        self._state_listeners = []
        self._events = queue.Queue(maxsize=INPUT_CAPACITY)
        self._last_input = 0
        self._received_update = 0
        self._threads = []
        context = mp.get_context("spawn")
        self._health = context.RawArray("d", 9)
        self._cancelled = context.RawValue("b", 0)
        self._health[8] = buffer_size / sample_rate * 1000
        self._control, child_control = context.Pipe(duplex=True)
        child_inputs, self._input_writer = context.Pipe(duplex=False)
        self._updates, child_updates = context.Pipe(duplex=False)
        self._process = context.Process(
            target=_audio_owner,
            args=(sample_rate, buffer_size, self._health, child_control, child_inputs,
                  child_updates, self._cancelled, output_destination()),
            name="vcpi-audio-owner", daemon=False,
        )
        try:
            self._process.start()
            child_control.close()
            child_inputs.close()
            child_updates.close()
            if not self._control.poll(120):
                raise AudioProcessError("audio owner startup timed out")
            ready = pickle.loads(self._control.recv_bytes(STATE_LIMIT))
            if not ready.get("ok"):
                raise AudioProcessError(ready.get("error", "audio owner startup failed"))
            self._apply_state(ready["state"])
            self._ready = True
            for name, target in (("vcpi-audio-state", self._read_updates),
                                 ("vcpi-audio-input", self._send_inputs),
                                 ("vcpi-audio-watch", self._watch_owner)):
                thread = threading.Thread(target=target, name=name, daemon=True)
                self._threads.append(thread)
                thread.start()
        except BaseException:
            child_control.close()
            child_inputs.close()
            child_updates.close()
            self._cancelled.value = 1
            self.shutdown()
            raise

    @property
    def owner_pid(self):
        return self._process.pid

    @property
    def failure(self):
        if not self._closing and self._process.exitcode is not None and self._failure is None:
            self._fail(f"audio owner exited (code {self._process.exitcode})")
        return self._failure

    @property
    def running(self):
        return not self._closed and self.failure is None and bool(self._health[0])

    @property
    def render_tracks(self):
        return int(self._health[1]) if self.running else 0

    @property
    def underruns(self):
        return int(self._health[2])

    @property
    def render_errors(self):
        return int(self._health[3])

    @property
    def callback_count(self):
        return int(self._health[4])

    @property
    def last_callback_ms(self):
        return float(self._health[5])

    @property
    def peak_callback_ms(self):
        return float(self._health[6])

    @property
    def deadline_misses(self):
        return int(self._health[7])

    @property
    def block_period_ms(self):
        return float(self._health[8])

    def _check_owner(self, allow_closing=False):
        if self._closed or (self._closing and not allow_closing):
            raise AudioProcessError("audio owner is shut down")
        failure = self.failure
        if failure is not None:
            raise AudioProcessError(failure)

    def _fail(self, message, terminate=False):
        with self._state_condition:
            if self._failure is None:
                self._failure = message
                logger.error("[Audio owner] %s", message)
            self._state_condition.notify_all()
        if terminate and self._process.pid is not None and self._process.is_alive():
            self._process.terminate()

    def _cancel_operation(self, operation):
        # Do not kill the semaphore-owning process before it can retire tracks.
        # The terminal flag still forbids consuming a late reply as another RPC.
        self._cancelled.value = 1
        with self._state_condition:
            if self._failure is None:
                self._failure = f"audio operation '{operation}' interrupted"
                logger.info("[Audio owner] %s; closing native owners", self._failure)
            self._state_condition.notify_all()


    def add_state_listener(self, callback):
        with self._state_condition:
            self._state_listeners.append(callback)

    def remove_state_listener(self, callback):
        with self._state_condition:
            if callback in self._state_listeners:
                self._state_listeners.remove(callback)

    def _notify(self, indices):
        with self._state_condition:
            listeners = tuple(self._state_listeners)
        for listener in listeners:
            try:
                listener(indices)
            except Exception:
                logger.exception("audio state listener failed")

    def _apply_state(self, state):
        with self._state_condition:
            self._epoch = state["epoch"]
            self.tracks = hydrate_tracks(state["tracks"])
            self.master_effects = state["master_effects"]
            for name in ("master_gain", "channel_map", "backend_label", "audio_output_name",
                         "audio_output_index", "output_realtime", "output_channels", "render_workers",
                         "audio_devices", "native_dependencies"):
                setattr(self, name, state[name])
        self._notify(None)

    def _plugin(self, index, fx_index):
        if index is None:
            return self.master_effects[fx_index] if 0 <= fx_index < len(self.master_effects) else None
        track = self.tracks[index]
        if track is None:
            return None
        if fx_index is None:
            return track.plugin
        return track.effects[fx_index] if 0 <= fx_index < len(track.effects) else None

    def _apply_updates(self, epoch, serial, updates):
        changed_tracks = set()
        with self._state_condition:
            if epoch == self._epoch:
                for update in updates:
                    kind = update[0]
                    if kind == "parameter":
                        _, index, fx_index, name, value, raw, display = update
                        plugin = self._plugin(index, fx_index)
                        if plugin is not None:
                            plugin.values[name] = value
                            descriptor = plugin.parameters.get(name)
                            if descriptor is not None:
                                if raw is not None:
                                    descriptor.raw_value = raw
                                descriptor.string_value = str(value) if display is None else display
                    elif kind == "deck":
                        _, index, generation, status = update
                        track = self.tracks[index]
                        if track is not None and track.generation == generation:
                            apply_deck_status(track, status)
                            changed_tracks.add(index)
                    elif kind == "track":
                        _, index, fields = update
                        track = self.tracks[index]
                        if track is not None:
                            for name, value in fields.items():
                                setattr(track, name, value)
                            changed_tracks.add(index)
                    elif kind == "master_gain":
                        self.master_gain = update[1]
                    elif kind == "output_realtime":
                        self.output_realtime = update[1]
            self._received_update = serial
            self._state_condition.notify_all()
        if changed_tracks:
            self._notify(sorted(changed_tracks))

    def _read_updates(self):
        try:
            while True:
                message = pickle.loads(self._updates.recv_bytes(UPDATE_LIMIT))
                if message[0] == "failure":
                    self._fail(f"audio owner failed: {message[1]}", terminate=True)
                    return
                _, epoch, serial, updates = message
                self._apply_updates(epoch, serial, updates)
        except Exception as error:
            if not self._closing:
                self._fail(f"audio state channel closed: {error}", terminate=True)

    def _send_inputs(self):
        try:
            while not self._closing:
                try:
                    message = self._events.get(timeout=0.1)
                except queue.Empty:
                    continue
                self._input_writer.send_bytes(message)
        except (EOFError, OSError) as error:
            if not self._closing:
                self._fail(f"audio input channel closed: {error}", terminate=True)

    def _watch_owner(self):
        count = self.callback_count
        checked = progressed = time.monotonic()
        while not wait_connections([self._process.sentinel], timeout=0.25):
            now = time.monotonic()
            current = self.callback_count
            limit = max(CALLBACK_STALL_TIMEOUT, self.block_period_ms * 8 / 1000)
            if (self._closing or not self._health[0] or current != count
                    or now - checked > limit):
                # Do not mistake OS suspend or an unscheduled supervisor for
                # observed callback starvation. Paused lifecycle work is exempt.
                progressed = now
            elif now - progressed >= limit:
                self._fail("audio callback stopped making progress; owner stopped", terminate=True)
                if not wait_connections([self._process.sentinel], timeout=1):
                    self._process.kill()
            count, checked = current, now
        self._process.join()
        if not self._closing:
            self._fail(f"audio owner exited (code {self._process.exitcode})")

    def _enqueue(self, kind, index, payload, generation=None):
        self._check_owner()
        with self._input_lock:
            if index is not None and generation is None:
                track = self.tracks[index]
                generation = track.generation if track is not None else 0
            sequence = self._last_input + 1
            message = _encode((sequence, kind, index, generation, payload), INPUT_LIMIT)
            try:
                self._events.put_nowait(message)
            except queue.Full:
                self._fail("audio input queue overflow; owner stopped instead of dropping MIDI", terminate=True)
                raise AudioProcessError(self._failure)
            self._last_input = sequence

    def enqueue_midi(self, track_index, message, generation=None):
        self._enqueue("midi", track_index, message.dict(), generation)

    def enqueue_param_change(self, track_index, name, value, generation=None):
        self._enqueue("parameter", track_index, (name, value), generation)

    def enqueue_deck_command(self, track_index, action, value=0, generation=None):
        self._enqueue("deck", track_index, (action, value), generation)

    def enqueue_track_change(self, track_index, generation=None, **fields):
        self._enqueue("track", track_index, fields, generation)

    def enqueue_master_gain(self, value):
        self._enqueue("master_gain", None, value)

    def _rpc(self, operation, arguments, timeout):
        self._check_owner(allow_closing=operation == "shutdown")
        if not self._rpc_lock.acquire(timeout=timeout):
            if operation == "shutdown":
                self._fail("audio owner busy during shutdown", terminate=True)
            raise AudioProcessError("audio control channel is busy")
        try:
            return self._exchange(operation, arguments, timeout)
        except (KeyboardInterrupt, SystemExit):
            self._cancel_operation(operation)
            raise
        finally:
            self._rpc_lock.release()

    def _exchange_native(self, message, operation, timeout):
        # Large pipe writes and multipart reads can block after initial
        # readiness. Keep the entire transfer under the caller's deadline.
        done, outcome = threading.Event(), []

        def transfer():
            try:
                send_frame(self._control, message, CONTROL_LIMIT)
                outcome.append(receive_frame(
                    self._control, STATE_LIMIT,
                    native_state=operation == "sync_state"))
            except Exception as error:
                outcome.append(error)
            finally:
                done.set()

        thread = threading.Thread(target=transfer, daemon=True,
                                  name="vcpi-native-state-transfer")
        self._threads.append(thread)
        thread.start()
        try:
            if not done.wait(timeout):
                raise TimeoutError(f"audio operation '{operation}' timed out")
            result = outcome[0]
            if isinstance(result, Exception):
                raise result
            return result
        finally:
            if done.is_set():
                thread.join(timeout=1)
                if not thread.is_alive():
                    self._threads.remove(thread)

    def _exchange(self, operation, arguments, timeout):
        self._check_owner(allow_closing=operation == "shutdown")
        with self._input_lock:
            after = self._last_input
        request = {"operation": operation, "arguments": arguments, "after": after}
        message = (encode_native_message(request, CONTROL_LIMIT)
                   if operation == "load_plugin_state" else
                   encode_deck_message(request, CONTROL_LIMIT)
                   if operation == "load_deck" else _encode(request, CONTROL_LIMIT))
        try:
            if operation in {"sync_state", "load_plugin_state", "load_deck"}:
                reply = self._exchange_native(message, operation, timeout)
            else:
                send_frame(self._control, message, CONTROL_LIMIT)
                if not self._control.poll(timeout):
                    raise TimeoutError(f"audio operation '{operation}' timed out")
                reply = receive_frame(self._control, STATE_LIMIT)
        except (EOFError, OSError, TimeoutError, pickle.UnpicklingError) as error:
            self._fail(f"audio operation '{operation}' failed: {error}", terminate=True)
            raise AudioProcessError(self._failure) from error
        if "state" in reply:
            self._apply_state(reply["state"])
        if "audio" in reply:
            with self._state_condition:
                for name, value in reply["audio"].items():
                    setattr(self, name, value)
        expected = reply.get("update_serial", 0)
        with self._state_condition:
            delivered = self._state_condition.wait_for(
                lambda: self._received_update >= expected or self._failure is not None, timeout=5)
        if not delivered:
            self._fail("audio applied-state acknowledgment timed out", terminate=True)
            raise AudioProcessError(self._failure)
        if not reply.get("ok"):
            error_type = REMOTE_ERRORS.get(reply.get("error_type"), RuntimeError)
            message = reply.get("error", "audio operation failed")
            if reply.get("fatal"):
                self._fail(message, terminate=True)
                raise AudioProcessError(message)
            raise error_type(message)
        if self._failure is not None and not self._closing:
            raise AudioProcessError(self._failure)
        return reply.get("result")

    def request(self, operation, **arguments):
        timeout = (120 if operation in LIFECYCLE_OPERATIONS
                   or operation in {"start_audio", "sync_state", "load_plugin_state"} else 10)
        result = self._rpc(operation, arguments, timeout)
        if operation in {"route", "unroute"}:
            with self._state_condition:
                self.channel_map = result
                for index, track in enumerate(self.tracks):
                    if track is not None:
                        track.midi_channels = {channel for channel, target in result.items() if target == index}
        return result

    def set_parameter(self, track_index, name, value, fx_index=None):
        return self.request("set_parameter", track_index=track_index, name=name, value=value, fx_index=fx_index)

    def deck_command(self, track_index, action, value=0):
        return self.request("deck_command", track_index=track_index,
                            action=action, value=value)

    def load_plugin_state(self, track_index, state, fx_index=None):
        """Load one saved native chunk and return refreshed lightweight metadata."""
        validate_native_state(state)
        return self.request("load_plugin_state", track_index=track_index,
                            state=state, fx_index=fx_index)

    def set_parameters(self, track_index, params, fx_index=None):
        """Restore bounded ordered batches, returning applied values and errors."""
        values, errors = {}, {}
        entries = iter(params.items())
        while batch := dict(islice(entries, 64)):
            pending = deque([batch])
            while pending:
                current = pending.popleft()
                try:
                    result = self.request(
                        "set_parameters", track_index=track_index,
                        params=current, fx_index=fx_index)
                except BufferError as error:
                    # Oversized packets fail before transmission. Split without
                    # changing order; an oversized single value cannot be sent.
                    if len(current) == 1:
                        errors[next(iter(current))] = str(error)
                    else:
                        items = list(current.items())
                        middle = len(items) // 2
                        pending.appendleft(dict(items[middle:]))
                        pending.appendleft(dict(items[:middle]))
                    continue
                values.update(result["values"])
                errors.update(result["errors"])
        return {"values": values, "errors": errors}

    def set_track_state(self, track_index, **fields):
        self.request("set_track_state", track_index=track_index, **fields)
        return self.tracks[track_index]

    def set_master_gain(self, value):
        return self.request("set_master_gain", value=value)

    def route(self, midi_channel, track_index):
        self.request("route", midi_channel=midi_channel, track_index=track_index)

    def unroute(self, midi_channel):
        self.request("unroute", midi_channel=midi_channel)

    def any_solo(self):
        return any(track.solo for track in self.tracks if track is not None)

    def start(self, output_device=None):
        self.request("start_audio", output_device=output_device)

    def stop(self):
        self.request("stop_audio")

    @property
    def state_lock(self):
        """Hold a coherent control-side snapshot while copying session fields."""
        return self._state_condition

    def sync_state(self):
        self.request("sync_state")

    def shutdown(self):
        if self._closed:
            return
        self._closing = True
        try:
            if self._ready and self._failure is None and self._process.is_alive():
                try:
                    self._rpc("shutdown", {}, OWNER_SHUTDOWN_TIMEOUT)
                except Exception:
                    logger.warning("audio owner did not shut down gracefully", exc_info=True)
        finally:
            if self._process.pid is not None:
                self._process.join(
                    timeout=OWNER_SHUTDOWN_TIMEOUT if self._cancelled.value else 1)
                if self._process.is_alive():
                    self._process.terminate()
                    self._process.join(timeout=1)
                if self._process.is_alive():
                    self._process.kill()
                    self._process.join(timeout=1)
            self._closed = True
            for connection in (self._control, self._input_writer, self._updates):
                connection.close()
            for thread in self._threads:
                if thread is not threading.current_thread():
                    thread.join(timeout=1)
            self._state_listeners.clear()
