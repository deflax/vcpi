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
import math
import threading
import time

from vcpi.deps import HAS_LINK, aalink

logger = logging.getLogger(__name__)


def validate_bpm(value) -> float:
    """Return a positive, finite tempo in beats per minute."""
    try:
        bpm = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("BPM must be a positive finite number") from exc
    if not math.isfinite(bpm) or bpm <= 0:
        raise ValueError("BPM must be a positive finite number")
    return bpm


class LinkSync:
    """Minimal wrapper around aalink for tempo sync.

    The async ``aalink.Link.sync(quantum)`` call is bridged to a
    blocking :meth:`sync` that any thread can call.  Internally it
    schedules the coroutine on the dedicated aalink event loop and
    waits on a :class:`concurrent.futures.Future`.
    """

    def __init__(self, bpm: float = 120.0):
        self._link = None
        self._bpm = validate_bpm(bpm)
        self._enabled = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None
        self._clock_lock = threading.RLock()
        self._beat = 0.0
        self._beat_time = time.monotonic()

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
        """Construct Link on its running event loop, including older bindings."""
        loop = self._start_loop_thread()

        async def construct():
            try:
                return aalink.Link(self._bpm)
            except TypeError:
                # Some supported releases require the explicit loop argument.
                return aalink.Link(self._bpm, loop)

        return asyncio.run_coroutine_threadsafe(construct(), loop).result(timeout=2.0)

    def enable(self):
        self._bpm = validate_bpm(self.bpm)
        if not HAS_LINK:
            raise RuntimeError("aalink not installed")
        if self._enabled:
            return

        try:
            link = self._make_link()
            link.enabled = True
            with self._clock_lock:
                self._link = link
                self._enabled = True
        except Exception:
            self._link = None
            self._enabled = False
            self._stop_loop_thread()
            raise

    def disable(self):
        with self._clock_lock:
            if self._link is not None:
                self._bpm, self._beat, self._beat_time = self.timeline()
                self._link.enabled = False
                self._link = None
            self._enabled = False
        self._stop_loop_thread()

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def bpm(self) -> float:
        with self._clock_lock:
            return self._link.tempo if self._link is not None else self._bpm

    @bpm.setter
    def bpm(self, value: float):
        value = validate_bpm(value)
        with self._clock_lock:
            _, beat, now = self.timeline()
            if self._link is not None:
                self._link.tempo = value
            self._beat, self._beat_time = beat, now
            self._bpm = value

    @property
    def num_peers(self) -> int:
        return self._link.num_peers if self._link else 0

    def timeline(self) -> tuple[float, float, float]:
        """Sample shared BPM, beat and local monotonic time without waiting.

        Local tempo changes retain phase. Disabling Link continues from its
        last beat and tempo; no common epoch with the native clock is assumed.
        """
        with self._clock_lock:
            now = time.monotonic()
            if self._link is not None:
                bpm = validate_bpm(self._link.tempo)
                beat = float(self._link.beat)
                if not math.isfinite(beat):
                    raise ValueError("Link beat must be finite")
            else:
                bpm = self._bpm
                beat = self._beat + (now - self._beat_time) * bpm / 60.0
            return bpm, beat, now

    @property
    def beat(self) -> float:
        return self.timeline()[1]

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
