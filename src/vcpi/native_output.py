"""Process-wide native stdout/stderr routing without persistent output files."""

from __future__ import annotations

import ctypes
from contextlib import contextmanager
import logging
from multiprocessing.reduction import DupFd
import os
import select
import sys
import threading
import time


_FRAGMENT_SIZE = 4096
_READ_TIMEOUT = 0.05
_STOP_TIMEOUT = 0.5
_lock = threading.RLock()
_active = None
_debug_enabled = None
_libc = None


def _flush():
    for stream in (sys.stdout, sys.stderr, sys.__stdout__, sys.__stderr__):
        if stream is None:
            continue
        try:
            stream.flush()
        except (OSError, ValueError):
            pass
    if _libc is not None:
        _libc.fflush(None)


def _line_buffer_c_stdio():
    global _libc
    if _libc is not None or sys.platform not in ("linux", "darwin"):
        return
    libc = ctypes.CDLL(None)
    libc.fflush.argtypes = [ctypes.c_void_p]
    libc.fflush.restype = ctypes.c_int
    libc.setvbuf.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                            ctypes.c_int, ctypes.c_size_t]
    libc.setvbuf.restype = ctypes.c_int
    names = ("__stdoutp", "__stderrp") if sys.platform == "darwin" else ("stdout", "stderr")
    streams = tuple(ctypes.c_void_p.in_dll(libc, name) for name in names)
    libc.fflush(None)
    for stream in streams:
        libc.setvbuf(stream, None, 1, 0)  # _IOLBF on Linux and macOS.
    _libc = libc


class _Drain:
    """Own read/output fds until the reader exits, even after a join timeout."""

    def __init__(self, source, destination):
        self.stopping = threading.Event()
        self.prefix = f"[{source}] ".encode("utf-8", "replace")
        self.failed = False
        self.reader, writer = os.pipe()
        try:
            self.destination = os.dup(destination)
        except BaseException:
            os.close(self.reader)
            os.close(writer)
            raise
        self.thread = threading.Thread(target=self._run, name="native-output", daemon=True)
        try:
            self.thread.start()
        except BaseException:
            os.close(self.reader)
            os.close(self.destination)
            os.close(writer)
            raise
        self.writer = writer

    def _emit(self, fragment):
        if self.failed or not fragment:
            return
        data = self.prefix + fragment
        if not fragment.endswith(b"\n"):
            data += b"\n"
        offset = 0
        while offset < len(data):
            try:
                count = os.write(self.destination, data[offset:])
            except InterruptedError:
                continue
            except OSError:
                self.failed = True
                return
            if count == 0:
                self.failed = True
                return
            offset += count

    def _run(self):
        pending = bytearray()
        deadline = None
        try:
            while True:
                if self.stopping.is_set() and deadline is None:
                    deadline = time.monotonic() + _STOP_TIMEOUT / 2
                if deadline is not None and time.monotonic() >= deadline:
                    break
                try:
                    readable, _, _ = select.select([self.reader], [], [], _READ_TIMEOUT)
                    if not readable:
                        self._emit(bytes(pending))
                        pending.clear()
                        if deadline is not None:
                            break
                        continue
                    chunk = os.read(self.reader, _FRAGMENT_SIZE)
                except InterruptedError:
                    continue
                except OSError:
                    break
                if not chunk:
                    break
                pending.extend(chunk)
                while pending:
                    newline = pending.find(b"\n", 0, _FRAGMENT_SIZE)
                    if newline >= 0:
                        length = newline + 1
                    elif len(pending) >= _FRAGMENT_SIZE:
                        length = _FRAGMENT_SIZE
                    else:
                        break
                    self._emit(bytes(pending[:length]))
                    del pending[:length]
            self._emit(bytes(pending))
        finally:
            os.close(self.reader)
            os.close(self.destination)

    def stop(self):
        self.stopping.set()
        self.thread.join(_STOP_TIMEOUT)


class _Capture:
    def __init__(self, source, destination):
        self.source = source
        self.forwarded_destination = destination
        self.previous = None
        self.saved_stdout = None
        self.saved_stderr = None
        self.destination = None
        self.null = None
        self.stream = None
        self.drain = None
        self.enabled = False
        self.closed = False

    def start(self):
        global _active
        self.previous = _active
        try:
            _flush()
            _line_buffer_c_stdio()
            self.saved_stdout = os.dup(1)
            self.saved_stderr = os.dup(2)
            if self.forwarded_destination is not None:
                self.destination = self.forwarded_destination.detach()
            else:
                self.destination = os.dup(
                    self.previous.destination if self.previous else self.saved_stdout)
            self.null = os.open(os.devnull, os.O_WRONLY)
            self.stream = os.fdopen(os.dup(self.destination), "w", buffering=1,
                                    encoding=getattr(sys.stdout, "encoding", None) or "utf-8",
                                    errors="backslashreplace")
            enabled = (self.previous.enabled if self.previous else
                       _debug_enabled if _debug_enabled is not None else
                       os.environ.get("LOG_LEVEL", "").upper() == "DEBUG")
            self.switch(enabled)
            _active = self
        except BaseException:
            self.close()
            raise

    def switch(self, enabled):
        _flush()
        new_drain = _Drain(self.source, self.destination) if enabled else None
        target = new_drain.writer if new_drain else self.null
        try:
            os.dup2(target, 1)
            os.dup2(target, 2)
        except BaseException:
            if new_drain:
                os.close(new_drain.writer)
                new_drain.stop()
            raise
        if new_drain:
            os.close(new_drain.writer)
        old_drain, self.drain = self.drain, new_drain
        self.enabled = enabled
        if old_drain:
            old_drain.stop()

    def close(self):
        global _active
        if self.closed:
            return
        self.closed = True
        _flush()
        # Restore first: readers must not depend on the caller closing their fds.
        try:
            if self.saved_stdout is not None:
                os.dup2(self.saved_stdout, 1)
            if self.saved_stderr is not None:
                os.dup2(self.saved_stderr, 2)
        finally:
            if _active is self:
                _active = self.previous
            if self.drain:
                self.drain.stop()
            replacement = self.previous.stream if self.previous else sys.stdout
            if self.stream is not None:
                for handler in logging.getLogger().handlers:
                    if isinstance(handler, logging.StreamHandler) and handler.stream is self.stream:
                        try:
                            handler.setStream(replacement)
                        except (OSError, ValueError):
                            handler.stream = replacement
                try:
                    self.stream.close()
                except (OSError, ValueError):
                    pass
            for descriptor in (self.saved_stdout, self.saved_stderr, self.destination, self.null):
                if descriptor is not None:
                    os.close(descriptor)


@contextmanager
def native_output(source: str, destination=None):
    """Discard raw output, or prefix it at the real terminal when debug is on.

    Pass ``output_destination()`` to a spawned child's context to bypass the
    parent's capture. A supplied multiprocessing DupFd is detached exactly once.
    """
    capture = _Capture(source, destination)
    with _lock:
        capture.start()
    try:
        yield
    finally:
        with _lock:
            capture.close()


def output_destination():
    """Return a spawn-safe duplicate of the terminal, never the capture pipe."""
    with _lock:
        return DupFd(_active.destination if _active else 1)


def logging_stream():
    """Return the capture-independent stream for structured Python records."""
    with _lock:
        return _active.stream if _active else sys.stdout


def set_debug_output(enabled: bool):
    """Switch raw output routing, flushing buffered bytes before each switch."""
    global _debug_enabled
    with _lock:
        _debug_enabled = bool(enabled)
        if _active is not None and _active.enabled != _debug_enabled:
            capture = _active
            try:
                capture.switch(_debug_enabled)
            except BaseException:
                capture.close()
                raise
