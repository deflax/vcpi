"""Ableton Link synchronisation (aalink wrapper)."""

from __future__ import annotations

import asyncio
import threading

from core.deps import HAS_LINK, aalink


class LinkSync:
    """Minimal wrapper around aalink for tempo sync."""

    def __init__(self, bpm: float = 120.0):
        self._link = None
        self._bpm = bpm
        self._enabled = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None

    def _start_loop_thread(self) -> asyncio.AbstractEventLoop:
        if self._loop is not None and self._loop_thread is not None and self._loop_thread.is_alive():
            return self._loop

        loop = asyncio.new_event_loop()

        def _runner():
            asyncio.set_event_loop(loop)
            loop.run_forever()

        thread = threading.Thread(target=_runner, name="vcpi-aalink-loop", daemon=True)
        thread.start()
        self._loop = loop
        self._loop_thread = thread
        return loop

    def _stop_loop_thread(self):
        if self._loop is None:
            return

        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
        except Exception:
            pass

        if self._loop_thread is not None:
            self._loop_thread.join(timeout=1.0)

        self._loop = None
        self._loop_thread = None

    def _make_link(self):
        """Construct aalink.Link across old/new constructor signatures."""
        # Older releases accepted a single bpm arg.
        try:
            return aalink.Link(self._bpm)
        except TypeError:
            pass

        # Newer releases require an event loop arg.
        loop = self._start_loop_thread()
        return aalink.Link(self._bpm, loop)

    def enable(self):
        if not HAS_LINK:
            raise RuntimeError("aalink not installed")
        if self._enabled:
            return

        try:
            self._link = self._make_link()
            self._link.enabled = True
            self._enabled = True
        except Exception:
            self._link = None
            self._enabled = False
            self._stop_loop_thread()
            raise

    def disable(self):
        if self._link:
            self._link.enabled = False
            self._link = None
        self._enabled = False
        self._stop_loop_thread()

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def bpm(self) -> float:
        return self._link.tempo if self._link else self._bpm

    @bpm.setter
    def bpm(self, value: float):
        self._bpm = value
        if self._link:
            self._link.tempo = value

    @property
    def num_peers(self) -> int:
        return self._link.num_peers if self._link else 0
