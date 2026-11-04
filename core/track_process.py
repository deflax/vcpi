"""Per-track process isolation with native mmap audio and MIDI transport.

Python handles deadline-bound control RPCs only. TrackRunner owns the DSP thread;
native output requests and audio publications never enter this interpreter.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import pickle
import queue
import threading
import signal
import time
import tempfile
from multiprocessing.connection import wait as wait_connections

from core.audio_state import apply_deck_status, capture_plugin
from core.audio_deps import require_native
from core.deck_bank import encode_deck_message
from core.models import InstrumentTrack, NUM_TRACKS
from core.logging_setup import configure_logging, set_log_level
from core.native_output import native_output, output_destination
from core.state_transport import (
    encode_native_message, receive_frame, send_frame, validate_native_message,
)


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
    "load_pd", "load_pd_patch", "load_deck",
))


class TrackProcessError(RuntimeError):
    """A terminal failure confined to a track, not the output device owner."""


def _encode(value, limit):
    data = pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
    if len(data) > limit:
        raise TrackProcessError(f"track protocol message exceeds {limit} bytes")
    return data



def _capture_native_plugin(plugin, values, readers, *, capture_native_state=False):
    snapshot = capture_plugin(plugin, values, readers,
                              capture_native_state=capture_native_state)
    # Native object addresses are unique only within a process. Snapshot
    # identities must not collide with another track or the owner's master FX.
    snapshot.token = (os.getpid() << 64) | snapshot.token
    return snapshot


def _track_state(owner, index, *, capture_native_state=False):
    track = owner.engine.tracks[index]
    values, readers = {}, {}
    if track.source_type == "deck":
        track.deck.update(track.plugin.native.deck_status())
        track.deck["status_time"] = time.monotonic()
    state = {
        "name": track.name,
        "path": track.path,
        "plugin": _capture_native_plugin(track.plugin, values, readers,
                                         capture_native_state=capture_native_state),
        "effects": [_capture_native_plugin(effect, values, readers,
                                           capture_native_state=capture_native_state)
                    for effect in track.effects],
        "midi_channels": set(track.midi_channels),
        "gain": float(track.gain),
        "muted": bool(track.muted),
        "solo": bool(track.solo),
        "enabled": bool(track.enabled),
        "source_type": track.source_type,
        "vcv_patch_path": track.vcv_patch_path,
        "pd_patch_path": track.pd_patch_path,
        "deck": dict(track.deck),
        "generation": track.generation,
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
    if operation == "load_pd":
        return {"patch_result": result[1], "plugdata_path": result[2]}
    if operation == "load_pd_patch":
        return {"patch_result": result[1], "plugdata_path": result[2],
                "patch_path": str(result[3])}
    return None


def _watch_parent(lifeline, transport_path, stopping):
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
            ready = watched
        if stopping.is_set():
            return
        if parent is not None and parent.sentinel in ready:
            try:
                os.unlink(transport_path)
            except FileNotFoundError:
                pass
            os._exit(1)
        if lifeline in ready:
            if parent is None:
                try:
                    os.unlink(transport_path)
                except FileNotFoundError:
                    pass
                os._exit(1)
            watched.remove(lifeline)




def _execute(owner, index, operation, arguments):
    arguments = dict(arguments)
    requested_index = arguments.pop("track_index", index)
    if requested_index != index:
        raise ValueError("control targets a different track")
    if operation == "set_log_level":
        return {"ok": True, "result": set_log_level(**arguments), "updates": []}
    if operation == "set_parameter":
        result = owner.engine.set_parameter(track_index=index, **arguments)
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
                    track_index=index, name=name, value=value, fx_index=fx_index)
            except (TimeoutError, BufferError, TrackProcessError):
                raise
            except Exception as error:
                errors[name] = str(error)
        result = {"values": values, "errors": errors}
    elif operation == "sync_state":
        if arguments:
            raise TypeError("sync_state takes no arguments")
        result = None
    elif operation == "deck_command":
        result = owner.engine.deck_command(index, **arguments)
    elif operation == "load_plugin_state":
        owner.engine.load_plugin_state(track_index=index, **arguments)
        result = None
    elif operation in ("load_effect", "remove_effect"):
        getattr(owner, operation)(track_index=index, **arguments)
        result = None
    elif operation == "shutdown":
        if arguments:
            raise TypeError("shutdown takes no arguments")
        owner.shutdown()
        return {"ok": True, "result": None, "updates": []}
    else:
        raise ValueError(f"unknown track operation: {operation}")
    updates = list(owner.engine._applied_updates)
    owner.engine._applied_updates.clear()
    reply = {"ok": True, "result": result, "updates": updates}
    track = owner.engine.tracks[index]
    if track.source_type == "deck" and operation in {"set_parameter", "set_parameters", "deck_command"}:
        reply["deck"] = dict(track.plugin.native.deck_status())
        reply["deck"]["status_time"] = time.monotonic()
    if operation not in {"set_parameter", "set_parameters", "deck_command"}:
        reply["state"] = _track_state(
            owner, index, capture_native_state=operation == "sync_state")
        if operation == "load_plugin_state":
            fx_index = arguments.get("fx_index")
            reply["result"] = (reply["state"]["plugin"] if fx_index is None
                               else reply["state"]["effects"][fx_index])
    return reply


def _track_owner(sample_rate, frames, index, operation, arguments,
                 transport_path, control, lifeline, output, status):
    with native_output(f"track {index + 1}", output):
        _run_track_owner(sample_rate, frames, index, operation, arguments,
                         transport_path, control, lifeline, status)


def _run_track_owner(sample_rate, frames, index, operation, arguments,
                     transport_path, control, lifeline, status):
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    configure_logging()
    owner = None
    transport = None
    ready = False
    stopping = threading.Event()
    parent_watch = threading.Thread(
        target=_watch_parent, args=(lifeline, transport_path, stopping),
        name="vcpi-track-parent", daemon=True)
    parent_watch.start()
    try:
        from core.plugin_host import PluginHost

        backend = require_native()
        transport = backend.Transport(transport_path, frames, False)
        if operation == "load_deck":
            request = receive_frame(control, CONTROL_LIMIT, native_state=True)
            if request.get("operation") != operation:
                raise ValueError("invalid initial deck catalog envelope")
            encode_deck_message(request, CONTROL_LIMIT)
            arguments = request["arguments"]
        owner = PluginHost(sample_rate=sample_rate, buffer_size=frames, track_mode=True)
        loaded = getattr(owner, operation)(track_index=index, **arguments)
        owner.engine.runner = backend.TrackRunner(
            transport, owner.engine.tracks[index].plugin.native, sample_rate, frames)
        owner.engine.runner.barrier()
        control.send_bytes(_encode({
            "ok": True, "result": _normalized_result(operation, loaded),
            "state": _track_state(owner, index), "updates": [],
        }, STATE_LIMIT))
        ready = True
        last_status = 0.0
        while True:
            now = time.monotonic()
            track = owner.engine.tracks[index]
            if track.source_type == "deck" and now - last_status >= 0.1:
                last_status = now
                current = dict(track.plugin.native.deck_status())
                current["status_time"] = now
                track.deck.update(current)
                # A single pending snapshot bounds memory and never stalls DSP
                # or control if the output owner is busy staging another source.
                try:
                    status.put_nowait(_encode(current, CONTROL_LIMIT))
                except queue.Full:
                    pass
            if not control.poll(0.05):
                continue
            request = receive_frame(control, CONTROL_LIMIT, native_state=True)
            operation = request["operation"]
            if operation == "load_plugin_state":
                validate_native_message(request, CONTROL_LIMIT)
            else:
                _encode(request, CONTROL_LIMIT)
            try:
                # DSP/control ownership is serialized by the native Plugin.
                # The barrier also orders snapshots after already-issued MIDI.
                owner.engine.runner.barrier()
                if operation == "shutdown":
                    stopping.set()
                reply = _execute(owner, index, operation, request["arguments"])
                data = (encode_native_message(reply, STATE_LIMIT)
                        if operation == "sync_state" else _encode(reply, STATE_LIMIT))
            except Exception as error:
                fatal = isinstance(error, (TimeoutError, BufferError, TrackProcessError))
                data = _encode({"ok": False, "error": str(error),
                                "error_type": type(error).__name__,
                                "fatal": fatal}, STATE_LIMIT)
                control.send_bytes(data)
                if fatal:
                    return
            else:
                send_frame(control, data, STATE_LIMIT)
                if operation == "shutdown":
                    return
    except (EOFError, BrokenPipeError):
        pass
    except Exception as error:
        try:
            control.send_bytes(_encode({"ok": False, "error": str(error),
                                       "error_type": type(error).__name__,
                                       "fatal": ready or isinstance(
                                           error, (TimeoutError, BufferError, TrackProcessError))
                                       }, STATE_LIMIT))
        except (OSError, TrackProcessError):
            pass
    finally:
        # Normal teardown must run native cleanup and Python finalizers; a
        # closed lifeline must not make the watchdog short-circuit either.
        stopping.set()
        control.close()
        status.cancel_join_thread()
        status.close()
        try:
            if owner is not None:
                owner.shutdown()
            if transport is not None and hasattr(transport, "close"):
                transport.close()
        finally:
            lifeline.close()


class TrackWorker:
    """Cached track facade; RPC/cleanup are owner-main-thread operations only."""

    def __init__(self, sample_rate, buffer_size, track_index, operation, arguments,
                 *, wait_hook=None):
        if sample_rate <= 0 or buffer_size <= 0:
            raise ValueError("sample rate and buffer size must be positive")
        if not isinstance(track_index, int) or not 0 <= track_index < NUM_TRACKS:
            raise ValueError("track index out of range")
        if operation not in _LOAD_OPERATIONS:
            raise ValueError(f"unknown track load operation: {operation}")
        arguments = dict(arguments)
        if arguments.pop("track_index", track_index) != track_index:
            raise ValueError("load targets a different track")
        # Check message size before spawning or allocating process resources.
        startup = {"operation": operation, "arguments": arguments}
        initial_data = (encode_deck_message(startup, CONTROL_LIMIT)
                        if operation == "load_deck" else None)
        if initial_data is None:
            _encode(startup, CONTROL_LIMIT)
        self.sample_rate = sample_rate
        self.buffer_size = buffer_size
        self.track_index = track_index
        self.track = None
        self.result = None
        self.misses = 0
        self.failure = None
        self.realtime = {}
        self._main_thread = threading.get_ident()
        self._wait_hook = wait_hook
        self._progress = 0
        self._progress_time = time.monotonic()
        self._closed = False
        self._rpc_active = False
        self._jobs = queue.Queue(maxsize=1)
        self._io_stop = threading.Event()
        context = mp.get_context("spawn")
        descriptor, self.transport_path = tempfile.mkstemp(prefix="vcpi-track-", suffix=".mmap")
        os.close(descriptor)
        try:
            self.transport = require_native().Transport(self.transport_path, buffer_size, True)
        except BaseException:
            os.unlink(self.transport_path)
            raise
        self._control, child_control = context.Pipe(duplex=True)
        child_lifeline, self._lifeline = context.Pipe(duplex=False)
        self._status = context.Queue(maxsize=1)
        self._process = context.Process(
            target=_track_owner,
            args=(sample_rate, buffer_size, track_index, operation,
                  {} if operation == "load_deck" else arguments,
                  self.transport_path, child_control, child_lifeline,
                  output_destination(), self._status),
            name=f"vcpi-track-{track_index + 1}", daemon=False,
        )
        self._io_thread = threading.Thread(target=self._io_loop,
                                           name=f"vcpi-track-io-{track_index + 1}",
                                           daemon=True)
        try:
            self._process.start()
            child_control.close()
            child_lifeline.close()
            self._io_thread.start()
            reply = self._exchange(initial_data, STARTUP_TIMEOUT, wait_hook)
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
            raise RuntimeError("track RPC/cleanup must run on the owner main thread")

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
                    send_frame(self._control, data, CONTROL_LIMIT)
                outcome.append(receive_frame(self._control, STATE_LIMIT, native_state=True))
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
                    self.failure = self.failure or "track control wait hook failed"
                    self._stop_process()
                    raise
            if time.monotonic() >= deadline:
                self.failure = self.failure or "track control acknowledgement timed out"
                self._stop_process()
                raise TrackProcessError(self.failure)
        result = outcome[0]
        if isinstance(result, Exception):
            self.failure = self.failure or f"track control channel failed: {result}"
            self._stop_process()
            raise TrackProcessError(self.failure) from result
        return result

    def _accept(self, reply):
        if not reply.get("ok"):
            message = reply.get("error", "track operation failed")
            if reply.get("fatal"):
                self.failure = self.failure or message
                self._stop_process()
                raise TrackProcessError(message)
            raise _REMOTE_ERRORS.get(reply.get("error_type"), RuntimeError)(message)
        state = reply.get("state")
        if state is not None:
            if self.track is None:
                self.track = InstrumentTrack(**state)
                self.track.render_owner = self
            else:
                # Gain, routing, generation and mute/solo are mixer-owned.
                self.track.plugin = state["plugin"]
                self.track.effects = state["effects"]
                self.track.deck = state.get("deck", {})
        if reply.get("deck") is not None:
            apply_deck_status(self.track, reply["deck"])
        for update in reply.get("updates", ()):
            if update[0] != "parameter":
                continue
            _, index, fx_index, name, value, raw, display = update
            if index != self.track_index or self.track is None:
                continue
            plugin = (self.track.plugin if fx_index is None
                      else self.track.effects[fx_index])
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
            raise TrackProcessError(self.failure or "track process is shut down")
        if self._rpc_active:
            raise RuntimeError("track requests cannot be nested")
        message = {"operation": operation, "arguments": arguments}
        data = (encode_native_message(message, CONTROL_LIMIT)
                if operation == "load_plugin_state" else _encode(message, CONTROL_LIMIT))
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


    def poll_deck_status(self):
        """Drain the bounded latest-status channel without a control RPC."""
        if self.track is None or self.track.source_type != "deck":
            return None
        latest = None
        try:
            while True:
                latest = pickle.loads(self._status.get_nowait())
        except queue.Empty:
            pass
        if (latest is not None
                and latest.get("status_time", 0) > self.track.deck.get("status_time", 0)):
            apply_deck_status(self.track, latest)
        else:
            latest = None
        return latest

    def poll_failure(self):
        """Detect local exit/stall and reap failed children outside the callback."""
        self._assert_main()
        if self._closed:
            return self.failure
        if self.failure is None and self._process.exitcode is not None:
            self.failure = f"track process exited (code {self._process.exitcode})"
        now = time.monotonic()
        stats = self.transport.stats()
        progress = int(stats["progress"])
        self.misses = int(stats["misses"])
        self.realtime = stats["realtime"]
        if stats.get("failure"):
            self.failure = self.failure or str(stats["failure"])
        if progress != self._progress or int(stats["sequence"]) <= int(stats["submitted"]):
            self._progress = progress
            self._progress_time = now
        if (self.failure is None and not self._rpc_active
                and int(stats["sequence"]) > int(stats["submitted"])
                and now - self._progress_time > RENDER_TIMEOUT):
            self.failure = "track rendering stalled"
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
        self._closed = True  # Caller must detach from native Engine first.
        if (self._process.pid is not None and self._process.is_alive()
                and self.failure is None and not self._rpc_active):
            try:
                data = _encode({"operation": "shutdown", "arguments": {}}, CONTROL_LIMIT)
                self._exchange(data, SHUTDOWN_TIMEOUT, None)
            except (TrackProcessError, OSError, queue.Full):
                pass
            self._process.join(SHUTDOWN_TIMEOUT)
        self._stop_process()
        self._io_stop.set()
        self._lifeline.close()
        self._control.close()
        self._status.close()
        if self._io_thread.is_alive():
            self._io_thread.join(SHUTDOWN_TIMEOUT)
        if hasattr(self.transport, "close"):
            self.transport.close()
        self.transport = None
        try:
            os.unlink(self.transport_path)
        except FileNotFoundError:
            pass
        if self.track is not None and self.track.render_owner is self:
            # Retired tracks must not retain a worker/track reference cycle.
            self.track.render_owner = None
