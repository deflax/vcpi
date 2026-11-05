"""Control-process audio facade with cached state and bounded RPCs."""

from __future__ import annotations

import logging
import multiprocessing as mp
import os
import pickle
import queue
import threading
import time
from collections import deque
from itertools import islice
from multiprocessing.connection import wait as wait_connections

from vcpi.audio_state import apply_deck_status, hydrate_tracks
from vcpi.deck_bank import encode_deck_message
from vcpi.models import NUM_TRACKS
from vcpi.native_output import output_destination
from vcpi.state_transport import (
    encode_native_message, receive_frame, send_frame, validate_native_state,
)

from .owner import _audio_owner
from .protocol import (
    AudioProcessError, CALLBACK_STALL_TIMEOUT, CONTROL_LIMIT, INPUT_CAPACITY,
    INPUT_LIMIT, LIFECYCLE_OPERATIONS, OWNER_SHUTDOWN_TIMEOUT, REMOTE_ERRORS,
    STATE_LIMIT, UPDATE_LIMIT, _encode,
)

logger = logging.getLogger(__name__)


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
            timing = kind == "deck" and payload[0] in {"sync_tempo", "sync_beat"}
            # Clock refreshes are replaceable. Reserve input capacity for MIDI
            # and user commands instead of failing the owner on timing backlog.
            if timing and self._events.qsize() >= self._events.maxsize // 2:
                return False
            if index is not None and generation is None:
                track = self.tracks[index]
                generation = track.generation if track is not None else 0
            sequence = self._last_input + 1
            message = _encode((sequence, kind, index, generation, payload), INPUT_LIMIT)
            try:
                self._events.put_nowait(message)
            except queue.Full:
                if timing:
                    return False
                self._fail("audio input queue overflow; owner stopped instead of dropping MIDI", terminate=True)
                raise AudioProcessError(self._failure)
            self._last_input = sequence
            return True

    def enqueue_midi(self, track_index, message, generation=None):
        self._enqueue("midi", track_index, message.dict(), generation)

    def enqueue_param_change(self, track_index, name, value, generation=None):
        self._enqueue("parameter", track_index, (name, value), generation)

    def enqueue_deck_command(self, track_index, action, value=0, generation=None):
        return self._enqueue("deck", track_index, (action, value), generation)

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
                   if operation in {"load_deck", "update_deck_catalog"} else _encode(request, CONTROL_LIMIT))
        try:
            if operation in {"sync_state", "load_plugin_state", "load_deck", "update_deck_catalog"}:
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
