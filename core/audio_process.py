"""Spawned audio owner with bounded control/input channels and cached diagnostics.

Only the child imports PluginHost/native audio libraries. Nothing in the audio
callback serializes data, waits for IPC, or services CLI/diagnostic requests.
"""

from __future__ import annotations

import logging
import multiprocessing as mp
import os
import pickle
import queue
import threading
import time
from collections import deque
from multiprocessing.connection import wait as wait_connections

from core.audio_state import capture_state, hydrate_slots
from core.models import NUM_SLOTS

logger = logging.getLogger(__name__)

INPUT_CAPACITY = 4096
CONTROL_LIMIT = 64 * 1024
STATE_LIMIT = 16 * 1024 * 1024
INPUT_LIMIT = 16 * 1024
UPDATE_LIMIT = 64 * 1024
CALLBACK_STALL_TIMEOUT = 2.0
LIFECYCLE_OPERATIONS = frozenset({
    "load_instrument", "load_wav", "remove_instrument", "load_effect",
    "remove_effect", "load_vcv", "load_vcv_patch",
})
REMOTE_ERRORS = {
    cls.__name__: cls for cls in
    (ValueError, TypeError, IndexError, KeyError, FileNotFoundError,
     PermissionError, RuntimeError, BufferError, TimeoutError)
}


class AudioProcessError(RuntimeError):
    """The audio owner is unavailable; no command was silently accepted."""


def _encode(message, limit):
    data = pickle.dumps(message, protocol=pickle.HIGHEST_PROTOCOL)
    if len(data) > limit:
        raise BufferError(f"audio protocol message exceeds {limit} bytes")
    return data


def _discover_devices():
    from core import audio_deps

    if not audio_deps.HAS_SOUNDDEVICE:
        return []
    devices = []
    for index, info in enumerate(audio_deps.sd.query_devices()):
        entry = dict(info)
        entry["index"] = index
        try:
            entry["hostapi_name"] = str(audio_deps.sd.query_hostapis(entry["hostapi"])["name"])
        except Exception:
            entry["hostapi_name"] = "unknown"
        devices.append(entry)
    return devices


class _OwnerRuntime:
    """Child main-thread dispatcher. Lifecycle work is performed with audio stopped."""

    def __init__(self, owner, control, inputs, updates):
        self.owner = owner
        self.engine = owner.engine
        self.control = control
        self.inputs = inputs
        self.updates = updates
        self.epoch = 0
        self.generations = [0] * NUM_SLOTS
        self.previous_slots = [None] * NUM_SLOTS
        self.received_input = 0
        self.update_serial = 0
        self.devices = _discover_devices()
        try:
            import mido
        except ImportError:
            mido = None
        self.mido = mido

    def pending(self):
        return (self.engine.pending_control_count + len(self.engine._midi_queue)
                + len(self.engine._applied_updates))

    def receive_input(self):
        sequence, kind, index, generation, payload = pickle.loads(
            self.inputs.recv_bytes(INPUT_LIMIT))
        if sequence != self.received_input + 1:
            raise RuntimeError("audio input sequence is discontinuous")
        self.received_input = sequence
        if index is not None:
            if not 0 <= index < NUM_SLOTS:
                raise ValueError("invalid audio input slot")
            if generation != self.generations[index]:
                return  # Retired instrument: never deliver its events to a replacement.
        if kind == "midi":
            if self.mido is None:
                raise RuntimeError("mido not installed")
            self.engine.enqueue_midi(index, self.mido.Message.from_dict(payload))
        elif kind == "parameter":
            self.engine.enqueue_param_change(index, payload[0], payload[1])
        elif kind == "slot":
            self.engine.enqueue_slot_change(index, **payload)
        elif kind == "master_gain":
            self.engine.enqueue_master_gain(payload)
        else:
            raise ValueError(f"unknown audio input kind: {kind}")

    def await_inputs(self, sequence):
        deadline = time.monotonic() + 5
        while self.received_input < sequence:
            if not self.engine.running:
                self.engine.apply_pending_controls()
            self.send_updates()
            if self.pending() >= INPUT_CAPACITY:
                if not self.engine.running:
                    raise BufferError("audio input backlog is full while audio is stopped")
                time.sleep(0.001)
            elif self.inputs.poll(0.01):
                self.receive_input()
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
                self.update_serial = serial

    def refresh_generations(self):
        changed = set()
        for index, slot in enumerate(self.engine.slots):
            if slot is not self.previous_slots[index]:
                self.generations[index] += 1
                if slot is not None:
                    slot.generation = self.generations[index]
                self.previous_slots[index] = slot
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
            "audio_devices": self.devices,
        }

    def execute(self, operation, arguments):
        if operation == "sync_state" and self.engine.running:
            # A save must include plugin-side MIDI/program changes, not only
            # cached setter echoes. Finish prior inputs, then inspect stopped.
            self.engine.flush_controls()
            deadline = time.monotonic() + 2.0
            while self.engine._midi_queue and self.engine.running:
                if time.monotonic() >= deadline:
                    raise TimeoutError("MIDI state capture barrier timed out")
                time.sleep(0.001)
        if operation in LIFECYCLE_OPERATIONS or operation == "sync_state":
            with self.engine.edit_effects():
                self.engine.apply_pending_controls()
                self.send_updates()
                result = (None if operation == "sync_state"
                          else getattr(self.owner, operation)(**arguments))
                self.refresh_generations()
                self.epoch += 1
                state = capture_state(self.owner, self.generations, self.epoch, self.devices)
                self.engine._applied_updates.clear()
                if operation == "load_vcv":
                    result = {"patch_result": result[1], "cardinal_path": result[2]}
                elif operation == "load_vcv_patch":
                    result = {"patch_result": result[1], "cardinal_path": result[2],
                              "patch_path": str(result[3])}
                else:
                    result = None  # Native slots/plugin instances never leave this process.
                # Encode full metadata before resuming the callback. Sending
                # already-encoded bytes may block this thread, not the callback.
                return _encode({"ok": True, "result": result, "state": state,
                                "update_serial": self.update_serial}, STATE_LIMIT)
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
            result = self.engine.set_parameter(**arguments)
        elif operation == "set_slot_state":
            self.engine.set_slot_state(**arguments)
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
        elif operation == "shutdown":
            if self.engine.running:
                self.engine.flush_controls()
            else:
                self.engine.apply_pending_controls()
            self.send_updates()
            self.previous_slots = [None] * NUM_SLOTS
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
        state = capture_state(self.owner, self.generations, self.epoch, self.devices)
        self.control.send_bytes(_encode({"ok": True, "state": state}, STATE_LIMIT))
        while True:
            if not self.engine.running:
                self.engine.apply_pending_controls()
            self.send_updates()
            if self.control.poll():
                request = pickle.loads(self.control.recv_bytes(CONTROL_LIMIT))
                operation = request["operation"]
                try:
                    self.await_inputs(request["after"])
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
                self.control.send_bytes(reply)
                if operation == "shutdown":
                    return
            for _ in range(64):
                if self.pending() >= INPUT_CAPACITY or not self.inputs.poll():
                    break
                self.receive_input()
            # poll() releases the GIL; never spin while idle or compete with
            # rendering merely because a client is displaying diagnostics.
            self.control.poll(0.005)


def _audio_owner(sample_rate, buffer_size, health, control, inputs, updates):
    owner = None
    try:
        from core.plugin_host import PluginHost

        owner = PluginHost(sample_rate=sample_rate, buffer_size=buffer_size)
        owner.engine.health_buffer = health
        owner.engine._publish_health()
        _OwnerRuntime(owner, control, inputs, updates).run()
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
        self.slots = [None] * NUM_SLOTS
        self.master_effects = []
        self.master_gain = 1.0
        self.channel_map = {}
        self.backend_label = "not opened"
        self.audio_output_name = None
        self.audio_devices = []
        self.native_dependencies = {}
        self.render_workers = min(os.cpu_count() or 2, NUM_SLOTS)
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
        self._health[8] = buffer_size / sample_rate * 1000
        self._control, child_control = context.Pipe(duplex=True)
        child_inputs, self._input_writer = context.Pipe(duplex=False)
        self._updates, child_updates = context.Pipe(duplex=False)
        self._process = context.Process(
            target=_audio_owner,
            args=(sample_rate, buffer_size, self._health, child_control, child_inputs, child_updates),
            name="vcpi-audio-owner", daemon=True,
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
    def render_slots(self):
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
            self.slots = hydrate_slots(state["slots"])
            self.master_effects = state["master_effects"]
            for name in ("master_gain", "channel_map", "backend_label", "audio_output_name",
                         "output_channels", "render_workers", "audio_devices", "native_dependencies"):
                setattr(self, name, state[name])
        self._notify(None)

    def _plugin(self, index, fx_index):
        if index is None:
            return self.master_effects[fx_index] if 0 <= fx_index < len(self.master_effects) else None
        slot = self.slots[index]
        if slot is None:
            return None
        if fx_index is None:
            return slot.plugin
        return slot.effects[fx_index] if 0 <= fx_index < len(slot.effects) else None

    def _apply_updates(self, epoch, serial, updates):
        changed_slots = set()
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
                    elif kind == "slot":
                        _, index, fields = update
                        slot = self.slots[index]
                        if slot is not None:
                            for name, value in fields.items():
                                setattr(slot, name, value)
                            changed_slots.add(index)
                    elif kind == "master_gain":
                        self.master_gain = update[1]
            self._received_update = serial
            self._state_condition.notify_all()
        if changed_slots:
            self._notify(sorted(changed_slots))

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
                slot = self.slots[index]
                generation = slot.generation if slot is not None else 0
            sequence = self._last_input + 1
            message = _encode((sequence, kind, index, generation, payload), INPUT_LIMIT)
            try:
                self._events.put_nowait(message)
            except queue.Full:
                self._fail("audio input queue overflow; owner stopped instead of dropping MIDI", terminate=True)
                raise AudioProcessError(self._failure)
            self._last_input = sequence

    def enqueue_midi(self, slot_index, message, generation=None):
        self._enqueue("midi", slot_index, message.dict(), generation)

    def enqueue_param_change(self, slot_index, name, value, generation=None):
        self._enqueue("parameter", slot_index, (name, value), generation)

    def enqueue_slot_change(self, slot_index, generation=None, **fields):
        self._enqueue("slot", slot_index, fields, generation)

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
            # An interrupted request may still be executing in the owner.
            # Terminate it rather than consuming its reply as a later command.
            self._fail(f"audio operation '{operation}' interrupted", terminate=True)
            raise
        finally:
            self._rpc_lock.release()

    def _exchange(self, operation, arguments, timeout):
        self._check_owner(allow_closing=operation == "shutdown")
        with self._input_lock:
            after = self._last_input
        message = _encode({"operation": operation, "arguments": arguments, "after": after}, CONTROL_LIMIT)
        try:
            self._control.send_bytes(message)
            if not self._control.poll(timeout):
                raise TimeoutError(f"audio operation '{operation}' timed out")
            reply = pickle.loads(self._control.recv_bytes(STATE_LIMIT))
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
        timeout = 120 if operation in LIFECYCLE_OPERATIONS or operation == "start_audio" else 10
        result = self._rpc(operation, arguments, timeout)
        if operation in {"route", "unroute"}:
            with self._state_condition:
                self.channel_map = result
                for index, slot in enumerate(self.slots):
                    if slot is not None:
                        slot.midi_channels = {channel for channel, target in result.items() if target == index}
        return result

    def set_parameter(self, slot_index, name, value, fx_index=None):
        return self.request("set_parameter", slot_index=slot_index, name=name, value=value, fx_index=fx_index)

    def set_slot_state(self, slot_index, **fields):
        self.request("set_slot_state", slot_index=slot_index, **fields)
        return self.slots[slot_index]

    def set_master_gain(self, value):
        return self.request("set_master_gain", value=value)

    def route(self, midi_channel, slot_index):
        self.request("route", midi_channel=midi_channel, slot_index=slot_index)

    def unroute(self, midi_channel):
        self.request("unroute", midi_channel=midi_channel)

    def any_solo(self):
        return any(slot.solo for slot in self.slots if slot is not None)

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
                    self._rpc("shutdown", {}, 3)
                except Exception:
                    logger.warning("audio owner did not shut down gracefully", exc_info=True)
        finally:
            if self._process.pid is not None:
                self._process.join(timeout=1)
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
