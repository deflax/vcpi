"""Ableton Link adapter used by the shared musical timeline.

The installed aalink binding exposes tempo, beat, playing, force_beat and
start_stop_sync_enabled. External-clock phase is committed only when the
timeline explicitly publishes it; Link remains collaborative between commits.
"""

from __future__ import annotations

import asyncio
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
    """Thread-safe tempo, phase and transport access to aalink."""

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
            link.quantum = 4.0
            link.start_stop_sync_enabled = True
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

    @property
    def playing(self) -> bool | None:
        with self._clock_lock:
            return bool(self._link.playing) if self._link is not None else None

    def publish(self, bpm: float, beat: float, running: bool, *,
                phase: bool = False, transport: bool = False):
        """Commit external timing using aalink's actual pybind API.

        ``request_beat`` preserves existing peers' phase, so it cannot bridge
        an external clock. ``force_beat`` is used only on explicit alignment
        or a bounded phase correction, never on every sampling tick.
        """
        bpm = validate_bpm(bpm)
        if not math.isfinite(beat):
            raise ValueError("Link beat must be finite")
        with self._clock_lock:
            if self._link is None:
                return
            if abs(self._link.tempo - bpm) > 0.01:
                self._link.tempo = bpm
            if phase:
                self._link.force_beat(beat)
            if transport:
                self._link.playing = running

    def seed(self, bpm: float, beat: float, running: bool):
        """Offer the existing local clock to a newly enabled Link session.

        request_beat is cooperative: joining an existing session does not
        force its peers onto our previous local phase.
        """
        bpm = validate_bpm(bpm)
        if not math.isfinite(beat):
            raise ValueError("Link beat must be finite")
        with self._clock_lock:
            if self._link is None:
                return
            if not self._link.num_peers:
                self._link.tempo = bpm
                self._link.playing = running
            self._link.request_beat(beat)
