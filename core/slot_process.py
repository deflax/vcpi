"""Persistent native slot owners with deadline-bound, one-block audio handoff.

Only the spawned main thread touches plugins. Audio/MIDI storage is fixed at
startup; the output callback never uses a pipe or waits for a lock. Request
cells are latest-wins, but MIDI has its own FIFO: skipping a late audio job
cannot skip its note-offs. Cell locks provide ownership and memory ordering
(on ARM as well as x86), and exact sequence matching rejects late audio.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import pickle
import queue
import threading
import signal
import time
from collections import deque
from multiprocessing.connection import wait as wait_connections

from core.audio_state import capture_plugin
from core.models import InstrumentSlot, NUM_SLOTS


RING_SIZE = 3
_REQUEST_CELLS = ((0, 1, 2), (1, 2, 0), (2, 0, 1))
MIDI_CAPACITY = 4096
MIDI_MESSAGE_LIMIT = 1024
CONTROL_LIMIT = 64 * 1024
PARAMETER_BATCH_LIMIT = 64
STATE_LIMIT = 16 * 1024 * 1024
STARTUP_TIMEOUT = 90.0  # Leave cleanup time inside the outer 120-second RPC.
REQUEST_TIMEOUT = 90.0
PARAMETER_TIMEOUT = 5.0
RENDER_TIMEOUT = 2.0
SHUTDOWN_TIMEOUT = 0.5
_REMOTE_ERRORS = {cls.__name__: cls for cls in (
    ValueError, TypeError, IndexError, KeyError, FileNotFoundError,
    PermissionError, RuntimeError,
)}
_LOAD_OPERATIONS = frozenset((
    "load_instrument", "load_wav", "load_vcv", "load_vcv_patch",
))


class SlotProcessError(RuntimeError):
    """A terminal failure confined to a slot, not the output device owner."""


def _encode(value, limit):
    data = pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
    if len(data) > limit:
        raise SlotProcessError(f"slot protocol message exceeds {limit} bytes")
    return data


class _SharedBlocks:
    """All cross-process data, with separate ownership for each audio cell."""

    def __init__(self, context, frames):
        self.requests = context.RawArray("q", RING_SIZE * 2)
        self.request_locks = [context.Lock() for _ in range(RING_SIZE)]
        self.outputs = context.RawArray("q", RING_SIZE)
        self.output_locks = [context.Lock() for _ in range(RING_SIZE)]
        self.audio = context.RawArray("f", RING_SIZE * frames * 2)
        self.midi = context.RawArray("B", MIDI_CAPACITY * MIDI_MESSAGE_LIMIT)
        self.midi_lengths = context.RawArray("H", MIDI_CAPACITY)
        self.midi_cursors = context.RawArray("q", 2)  # published, consumed
        self.midi_lock = context.Lock()
        self.progress = context.RawArray("q", 1)
        self.progress_lock = context.Lock()


def _capture_native_plugin(plugin, values, readers):
    snapshot = capture_plugin(plugin, values, readers)
    # Native object addresses are unique only within a process. Snapshot
    # identities must not collide with another slot or the owner's master FX.
    snapshot.token = (os.getpid() << 64) | snapshot.token
    return snapshot


def _slot_state(owner, index):
    slot = owner.engine.slots[index]
    values, readers = {}, {}
    state = {
        "name": slot.name,
        "path": slot.path,
        "plugin": _capture_native_plugin(slot.plugin, values, readers),
        "effects": [_capture_native_plugin(effect, values, readers) for effect in slot.effects],
        "midi_channels": set(slot.midi_channels),
        "gain": float(slot.gain),
        "muted": bool(slot.muted),
        "solo": bool(slot.solo),
        "enabled": bool(slot.enabled),
        "source_type": slot.source_type,
        "vcv_patch_path": slot.vcv_patch_path,
        "generation": slot.generation,
    }
    owner.engine.parameter_values = values
    owner.engine.parameter_readers = readers
    return state


def _normalized_result(operation, result):
    if operation == "load_vcv":
        return {"patch_result": result[1], "cardinal_path": result[2]}
    if operation == "load_vcv_patch":
        return {"patch_result": result[1], "cardinal_path": result[2],
                "patch_path": str(result[3])}
    return None


def _watch_parent(lifeline, shared, stopping):
    # EOF can precede the parent's sentinel during teardown. Keep watching the
    # sentinel rather than exiting before orphan semaphore cleanup is possible.
    parent = mp.parent_process()
    watched = [lifeline]
    if parent is not None:
        watched.append(parent.sentinel)
    while not stopping.is_set():
        try:
            ready = wait_connections(watched, timeout=0.05)
        except OSError:
            if stopping.is_set():
                return
            watched = [parent.sentinel] if parent is not None else []
            continue
        if stopping.is_set():
            return
        if parent is not None and parent.sentinel in ready:
            # Sentinel readiness, unlike is_alive immediately after pipe EOF,
            # proves the creator is gone and cannot run its lock finalizers.
            from multiprocessing.synchronize import SemLock

            locks = (*shared.request_locks, *shared.output_locks,
                     shared.midi_lock, shared.progress_lock)
            for lock in locks:
                name = lock._semlock.name
                if name is not None:
                    try:
                        SemLock._cleanup(name)
                    except FileNotFoundError:
                        pass  # An orderly parent exit already unlinked it.
            os._exit(1)
            return
        if lifeline in ready:
            if parent is None:
                os._exit(1)
                return
            watched.remove(lifeline)

def _next_job(shared, previous):
    sequence, endpoint = previous, 0
    for index, lock in enumerate(shared.request_locks):
        if not lock.acquire(False):
            continue
        try:
            current = shared.requests[index * 2]
            if current > sequence:
                sequence = current
                endpoint = shared.requests[index * 2 + 1]
            shared.requests[index * 2] = 0
        finally:
            lock.release()
    return (sequence, endpoint) if sequence > previous else None


def _take_midi(shared, endpoint, mido):
    messages = []
    with shared.midi_lock:
        published, consumed = shared.midi_cursors
        if not 0 <= endpoint <= published:
            raise SlotProcessError("slot MIDI sequence is discontinuous")
        while consumed < endpoint:
            index = consumed % MIDI_CAPACITY
            length = shared.midi_lengths[index]
            start = index * MIDI_MESSAGE_LIMIT
            messages.append(mido.Message.from_bytes(shared.midi[start:start + length]))
            consumed += 1
        shared.midi_cursors[1] = consumed
    return messages


def _render_native(owner, index, midi, frames):
    errors = owner.engine.render_errors
    rendered = owner.engine._render_slot(
        index, owner.engine.slots[index], midi, frames)
    if rendered is None or owner.engine.render_errors != errors:
        raise SlotProcessError("native slot rendering failed")
    return rendered

def _complete_job(owner, index, frames, shared, audio, sequence, endpoint, mido):
    """Render an issued block once, including RPC ordering barriers."""
    midi = _take_midi(shared, endpoint, mido)
    rendered = _render_native(owner, index, midi, frames)
    cell = sequence % RING_SIZE
    lock = shared.output_locks[cell]
    if lock.acquire(False):
        try:
            audio[cell].fill(0)
            count = min(frames, rendered.shape[0])
            audio[cell, :count] = rendered[:count]
            shared.outputs[cell] = sequence
        finally:
            lock.release()
    with shared.progress_lock:
        shared.progress[0] = sequence



def _execute(owner, index, operation, arguments):
    arguments = dict(arguments)
    requested_index = arguments.pop("slot_index", index)
    if requested_index != index:
        raise ValueError("control targets a different slot")
    if operation == "set_parameter":
        result = owner.engine.set_parameter(slot_index=index, **arguments)
    elif operation == "set_parameters":
        params = arguments.pop("params")
        fx_index = arguments.pop("fx_index", None)
        if arguments:
            raise TypeError("set_parameters takes only params and fx_index")
        if not isinstance(params, dict):
            raise TypeError("params must be a dict")
        if len(params) > PARAMETER_BATCH_LIMIT:
            raise ValueError(
                f"parameter batch exceeds {PARAMETER_BATCH_LIMIT} entries")
        values, errors = {}, {}
        for name, value in params.items():
            try:
                values[name] = owner.engine.set_parameter(
                    slot_index=index, name=name, value=value, fx_index=fx_index)
            except (TimeoutError, BufferError, SlotProcessError):
                raise
            except Exception as error:
                errors[name] = str(error)
        result = {"values": values, "errors": errors}
    elif operation == "sync_state":
        if arguments:
            raise TypeError("sync_state takes no arguments")
        result = None
    elif operation in ("load_effect", "remove_effect"):
        getattr(owner, operation)(slot_index=index, **arguments)
        result = None
    elif operation == "shutdown":
        if arguments:
            raise TypeError("shutdown takes no arguments")
        owner.shutdown()
        return {"ok": True, "result": None, "updates": []}
    else:
        raise ValueError(f"unknown slot operation: {operation}")
    updates = list(owner.engine._applied_updates)
    owner.engine._applied_updates.clear()
    reply = {"ok": True, "result": result, "updates": updates}
    if operation not in {"set_parameter", "set_parameters"}:
        reply["state"] = _slot_state(owner, index)
    return reply


def _slot_owner(sample_rate, frames, index, operation, arguments,
                shared, control, lifeline):
    # Foreground Ctrl-C belongs to the control process, never a native owner.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    owner = None
    ready = False
    stopping = threading.Event()
    parent_watch = threading.Thread(
        target=_watch_parent, args=(lifeline, shared, stopping),
        name="vcpi-slot-parent", daemon=True)
    parent_watch.start()
    try:
        import mido
        import numpy as np
        from core.plugin_host import PluginHost

        owner = PluginHost(sample_rate=sample_rate, buffer_size=frames)
        owner.engine._prepare_buffers(frames)
        loaded = getattr(owner, operation)(slot_index=index, **arguments)
        control.send_bytes(_encode({
            "ok": True, "result": _normalized_result(operation, loaded),
            "state": _slot_state(owner, index), "updates": [],
        }, STATE_LIMIT))
        ready = True
        audio = np.frombuffer(shared.audio, dtype=np.float32).reshape(
            RING_SIZE, frames, 2)
        previous = 0
        while True:
            # Controls and native rendering run sequentially on this thread.
            if control.poll():
                request = pickle.loads(control.recv_bytes(CONTROL_LIMIT))
                operation = request["operation"]
                try:
                    sequence = request.get("after_block", previous)
                    if sequence > previous:
                        _complete_job(
                            owner, index, frames, shared, audio, sequence,
                            request.get("after_midi", 0), mido)
                        previous = sequence
                    if operation == "shutdown":
                        stopping.set()
                    reply = _execute(owner, index, operation, request["arguments"])
                    data = _encode(reply, STATE_LIMIT)
                except Exception as error:
                    fatal = isinstance(error, (TimeoutError, BufferError, SlotProcessError))
                    data = _encode({"ok": False, "error": str(error),
                                    "error_type": type(error).__name__,
                                    "fatal": fatal}, STATE_LIMIT)
                    control.send_bytes(data)
                    if fatal:
                        return
                else:
                    control.send_bytes(data)
                    if operation == "shutdown":
                        return
            job = _next_job(shared, previous)
            if job is None:
                control.poll(0.0005)
                continue
            sequence, endpoint = job
            _complete_job(owner, index, frames, shared, audio, sequence, endpoint, mido)
            previous = sequence
    except (EOFError, BrokenPipeError):
        pass
    except Exception as error:
        try:
            control.send_bytes(_encode({"ok": False, "error": str(error),
                                       "error_type": type(error).__name__,
                                       "fatal": ready or isinstance(
                                           error, (TimeoutError, BufferError, SlotProcessError))
                                       }, STATE_LIMIT))
        except (OSError, SlotProcessError):
            pass
    finally:
        # Normal teardown must run native cleanup and Python finalizers; a
        # closed lifeline must not make the watchdog short-circuit either.
        stopping.set()
        control.close()
        try:
            if owner is not None:
                owner.shutdown()
        finally:
            lifeline.close()


class SlotWorker:
    """Cached slot facade; RPC/cleanup are owner-main-thread operations only."""

    def __init__(self, sample_rate, buffer_size, slot_index, operation, arguments,
                 *, wait_hook=None):
        import numpy as np

        if sample_rate <= 0 or buffer_size <= 0:
            raise ValueError("sample rate and buffer size must be positive")
        if not isinstance(slot_index, int) or not 0 <= slot_index < NUM_SLOTS:
            raise ValueError("slot index out of range")
        if operation not in _LOAD_OPERATIONS:
            raise ValueError(f"unknown slot load operation: {operation}")
        arguments = dict(arguments)
        if arguments.pop("slot_index", slot_index) != slot_index:
            raise ValueError("load targets a different slot")
        # Check message size before spawning or allocating process resources.
        _encode({"operation": operation, "arguments": arguments}, CONTROL_LIMIT)
        self.sample_rate = sample_rate
        self.buffer_size = buffer_size
        self.slot_index = slot_index
        self.slot = None
        self.result = None
        self.misses = 0
        self.failure = None
        self._main_thread = threading.get_ident()
        self._wait_hook = wait_hook
        self._sequence = 0
        self._submitted = 0
        self._published_midi = 0
        self._pending_midi = deque()
        self._progress = 0
        self._progress_time = time.monotonic()
        self._callback_time = self._progress_time
        self._closed = False
        self._rpc_active = False
        self._jobs = queue.Queue(maxsize=1)
        self._io_stop = threading.Event()
        context = mp.get_context("spawn")
        self._shared = _SharedBlocks(context, buffer_size)
        self._audio = np.frombuffer(self._shared.audio, dtype=np.float32).reshape(
            RING_SIZE, buffer_size, 2)
        self._output = np.empty((buffer_size, 2), dtype=np.float32)
        self._control, child_control = context.Pipe(duplex=True)
        child_lifeline, self._lifeline = context.Pipe(duplex=False)
        self._process = context.Process(
            target=_slot_owner,
            args=(sample_rate, buffer_size, slot_index, operation, arguments,
                  self._shared, child_control, child_lifeline),
            name=f"vcpi-slot-{slot_index + 1}", daemon=False,
        )
        self._io_thread = threading.Thread(target=self._io_loop,
                                           name=f"vcpi-slot-io-{slot_index + 1}",
                                           daemon=True)
        try:
            self._process.start()
            child_control.close()
            child_lifeline.close()
            self._io_thread.start()
            reply = self._exchange(None, STARTUP_TIMEOUT, wait_hook)
            self._accept(reply)
            self.result = reply.get("result")
        except BaseException:
            child_control.close()
            child_lifeline.close()
            self.shutdown()
            raise

    @property
    def pid(self):
        return self._process.pid

    def _assert_main(self):
        if threading.get_ident() != self._main_thread:
            raise RuntimeError("slot RPC/cleanup must run on the owner main thread")

    def _io_loop(self):
        # Pipes may block on a partial frame even after poll reports readable.
        # Only this helper can block; the main thread enforces the whole RPC
        # deadline (including send and snapshot receive), and can kill the child.
        while not self._io_stop.is_set():
            try:
                job = self._jobs.get(timeout=0.05)
            except queue.Empty:
                continue
            data, done, outcome = job
            try:
                if data is not None:
                    self._control.send_bytes(data)
                outcome.append(pickle.loads(self._control.recv_bytes(STATE_LIMIT)))
            except Exception as error:
                outcome.append(error)
            finally:
                done.set()

    def _exchange(self, data, timeout, wait_hook):
        done, outcome = threading.Event(), []
        self._jobs.put_nowait((data, done, outcome))
        deadline = time.monotonic() + timeout
        while not done.wait(0.001):
            if wait_hook is not None:
                try:
                    wait_hook()
                except BaseException:
                    self.failure = self.failure or "slot control wait hook failed"
                    self._stop_process()
                    raise
            if time.monotonic() >= deadline:
                self.failure = self.failure or "slot control acknowledgement timed out"
                self._stop_process()
                raise SlotProcessError(self.failure)
        result = outcome[0]
        if isinstance(result, Exception):
            self.failure = self.failure or f"slot control channel failed: {result}"
            self._stop_process()
            raise SlotProcessError(self.failure) from result
        return result

    def _accept(self, reply):
        if not reply.get("ok"):
            message = reply.get("error", "slot operation failed")
            if reply.get("fatal"):
                self.failure = self.failure or message
                self._stop_process()
                raise SlotProcessError(message)
            raise _REMOTE_ERRORS.get(reply.get("error_type"), RuntimeError)(message)
        state = reply.get("state")
        if state is not None:
            if self.slot is None:
                self.slot = InstrumentSlot(**state)
                self.slot.render_owner = self
            else:
                # Gain, routing, generation and mute/solo are mixer-owned.
                self.slot.plugin = state["plugin"]
                self.slot.effects = state["effects"]
        for update in reply.get("updates", ()):
            if update[0] != "parameter":
                continue
            _, index, fx_index, name, value, raw, display = update
            if index != self.slot_index or self.slot is None:
                continue
            plugin = (self.slot.plugin if fx_index is None
                      else self.slot.effects[fx_index])
            if plugin is not None:
                plugin.values[name] = value
                descriptor = plugin.parameters.get(name)
                if descriptor is not None:
                    if raw is not None:
                        descriptor.raw_value = raw
                    descriptor.string_value = str(value) if display is None else display

    def request(self, operation, *, wait_hook=None, **arguments):
        self._assert_main()
        if self._closed or self.failure is not None:
            raise SlotProcessError(self.failure or "slot process is shut down")
        if self._rpc_active:
            raise RuntimeError("slot requests cannot be nested")
        data = _encode({"operation": operation, "arguments": arguments,
                        "after_block": self._sequence,
                        "after_midi": self._published_midi}, CONTROL_LIMIT)
        timeout = (PARAMETER_TIMEOUT if operation in {
            "set_parameter", "set_parameters", "sync_state"} else REQUEST_TIMEOUT)
        self._rpc_active = True
        try:
            reply = self._exchange(data, timeout,
                                   self._wait_hook if wait_hook is None else wait_hook)
            self._accept(reply)
            return reply
        finally:
            self._rpc_active = False
            self._progress_time = time.monotonic()

    def _fail_render(self, message):
        if self.failure is None:
            self.failure = message
        return None

    def _publish_midi(self, midi_messages):
        # All note bytes, including SysEx, use bounded shared storage. A busy
        # MIDI lock defers publication to a later block rather than dropping.
        for message in midi_messages:
            if len(self._pending_midi) >= MIDI_CAPACITY:
                return self._fail_render("slot MIDI backlog is full")
            try:
                data = message.bytes()
            except Exception as error:
                return self._fail_render(f"slot MIDI encoding failed: {error}")
            if not 0 < len(data) <= MIDI_MESSAGE_LIMIT:
                return self._fail_render("slot MIDI message exceeds bounded storage")
            self._pending_midi.append(data)
        if not self._pending_midi:
            return True
        shared = self._shared
        if not shared.midi_lock.acquire(False):
            return True
        try:
            published, consumed = shared.midi_cursors
            available = MIDI_CAPACITY - (published - consumed)
            if len(self._pending_midi) > available:
                return self._fail_render("slot shared MIDI FIFO is full")
            while self._pending_midi:
                data = self._pending_midi.popleft()
                cell = published % MIDI_CAPACITY
                start = cell * MIDI_MESSAGE_LIMIT
                shared.midi[start:start + len(data)] = data
                shared.midi_lengths[cell] = len(data)
                published += 1
            shared.midi_cursors[0] = published
            self._published_midi = published
        finally:
            shared.midi_lock.release()
        return True

    def render(self, midi_messages, frames):
        """Consume exactly the previous block and offer the next; never wait."""
        if self._closed or self.failure is not None:
            return None
        if frames != self.buffer_size:
            return self._fail_render("slot callback block size changed")
        now = time.monotonic()
        if now - self._callback_time > RENDER_TIMEOUT:
            self._progress_time = now
        self._callback_time = now
        self._sequence += 1
        sequence = self._sequence
        expected = sequence - 1
        rendered = None
        cell = expected % RING_SIZE
        lock = self._shared.output_locks[cell]
        if expected and lock.acquire(False):
            try:
                if self._shared.outputs[cell] == expected:
                    self._output[:] = self._audio[cell]
                    self._shared.outputs[cell] = 0
                    rendered = self._output
            finally:
                lock.release()
        if expected and rendered is None:
            self.misses += 1
        if not self._publish_midi(midi_messages):
            return None
        # Request cells are sequence-tagged, not tied to an audio cell. A
        # transient reader on one cell must not lose an otherwise timely job.
        for cell in _REQUEST_CELLS[sequence % RING_SIZE]:
            lock = self._shared.request_locks[cell]
            if not lock.acquire(False):
                continue
            try:
                self._shared.requests[cell * 2 + 1] = self._published_midi
                self._shared.requests[cell * 2] = sequence
                self._submitted = sequence
            finally:
                lock.release()
            break
        return rendered

    def poll_failure(self):
        """Detect local exit/stall and reap failed children outside the callback."""
        self._assert_main()
        if self._closed:
            return self.failure
        if self.failure is None and self._process.exitcode is not None:
            self.failure = f"slot process exited (code {self._process.exitcode})"
        now = time.monotonic()
        lock = self._shared.progress_lock
        if lock.acquire(False):
            try:
                progress = self._shared.progress[0]
            finally:
                lock.release()
            if progress != self._progress or self._submitted <= progress:
                self._progress = progress
                self._progress_time = now
        if (self.failure is None and not self._rpc_active
                and self._submitted > self._progress
                and now - self._progress_time > RENDER_TIMEOUT):
            self.failure = "slot rendering stalled"
        if self.failure is not None:
            self._stop_process()
        return self.failure

    def _stop_process(self):
        process = self._process
        if process.pid is None:
            return
        if process.is_alive():
            process.terminate()
        process.join(SHUTDOWN_TIMEOUT)
        if process.is_alive():
            process.kill()
            process.join(SHUTDOWN_TIMEOUT)

    def shutdown(self):
        """Bounded idempotent cleanup, never callable from the audio callback."""
        self._assert_main()
        if self._closed:
            return
        self._closed = True  # Callback stops touching storage before teardown.
        if (self._process.pid is not None and self._process.is_alive()
                and self.failure is None and not self._rpc_active):
            try:
                data = _encode({"operation": "shutdown", "arguments": {}}, CONTROL_LIMIT)
                self._exchange(data, SHUTDOWN_TIMEOUT, None)
            except (SlotProcessError, OSError, queue.Full):
                pass
            self._process.join(SHUTDOWN_TIMEOUT)
        self._stop_process()
        self._io_stop.set()
        self._lifeline.close()
        self._control.close()
        if self._io_thread.is_alive():
            self._io_thread.join(SHUTDOWN_TIMEOUT)
        self._pending_midi.clear()
        if self.slot is not None and self.slot.render_owner is self:
            # Retired slots must not retain a worker/slot reference cycle;
            # stream ownership can keep Python cyclic GC disabled.
            self.slot.render_owner = None
