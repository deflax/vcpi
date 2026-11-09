"""Output-owner track facade with cached snapshots and deadline-bound RPCs."""

from __future__ import annotations

import multiprocessing as mp
import os
import queue
import tempfile
import threading
import time

from vcpi.audio_state import apply_deck_status
from vcpi.deck_bank import encode_deck_message, receive_deck_status
from vcpi.models import InstrumentTrack, NUM_TRACKS
from vcpi.native_output import output_destination
from vcpi.state_transport import encode_native_message, receive_frame, send_frame

from .track_owner import _track_owner
from .track_protocol import (
    CONTROL_LIMIT, PARAMETER_TIMEOUT, RENDER_TIMEOUT, REQUEST_TIMEOUT,
    SHUTDOWN_TIMEOUT, STARTUP_TIMEOUT, STATE_LIMIT, TrackProcessError,
    _LOAD_OPERATIONS, _REMOTE_ERRORS, _encode,
)


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
            from vcpi.audio_deps import require_native

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
                self.track.name = state["name"]
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
                if operation == "load_plugin_state" else
                encode_deck_message(message, CONTROL_LIMIT)
                if operation == "update_deck_catalog" else _encode(message, CONTROL_LIMIT))
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
                latest = receive_deck_status(self._status.get_nowait(), CONTROL_LIMIT)
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
