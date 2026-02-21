"""Ableton Link synchronisation (aalink wrapper).

LinkSync wraps the ``aalink`` library to provide:

* Shared tempo (``bpm`` property, bidirectional with Link peers).
* Beat-grid synchronisation via :meth:`sync` -- blocks the calling
  thread until the shared Link timeline reaches the next *n*-th beat
  boundary.  This is the mechanism the internal sequencer uses to
  align its bar start/end with Ableton Live and other Link peers.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading

from core.deps import HAS_LINK, aalink

logger = logging.getLogger(__name__)


class LinkSync:
    """Minimal wrapper around aalink for tempo sync.

    The async ``aalink.Link.sync(quantum)`` call is bridged to a
    blocking :meth:`sync` that any thread can call.  Internally it
    schedules the coroutine on the dedicated aalink event loop and
    waits on a :class:`concurrent.futures.Future`.
    """

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

    # -- beat-grid sync (thread-safe) ----------------------------------------

    def sync(self, beats: float, timeout: float = 4.0) -> float:
        """Block until the Link timeline reaches the next *beats* boundary.

        This is a thread-safe wrapper around ``await link.sync(beats)``.
        It schedules the coroutine on the aalink event loop and waits
        for the result.

        Args:
            beats:   Quantum value -- e.g. 4 for a full bar, 1 for a
                     quarter-note, 0.5 for an eighth-note.
            timeout: Maximum seconds to wait (safety net).

        Returns:
            The beat number at which the sync resolved (from aalink).

        Raises:
            RuntimeError: If Link is not enabled or the event loop is
                not running.
        """
        link = self._link
        loop = self._loop
        if link is None or loop is None or not self._enabled:
            raise RuntimeError("Link is not enabled")

        fut: concurrent.futures.Future[float] = concurrent.futures.Future()

        async def _sync():
            try:
                beat = await link.sync(beats)
                fut.set_result(beat)
            except Exception as exc:
                fut.set_exception(exc)

        loop.call_soon_threadsafe(asyncio.ensure_future, _sync())
        return fut.result(timeout=timeout)
