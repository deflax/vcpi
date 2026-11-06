"""One transport and beat clock shared by sequencers, decks and controllers.

MIDI clock is 24 pulses per quarter note. A rolling, outlier-checked estimate
must settle before automatic acquisition; clock loss freewheels at the last
accepted tempo rather than masquerading as Stop. Source acquisition is deferred
to the next local bar, except explicit transport/position commands.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import math
import statistics
import threading
import time

from vcpi.link import LinkSync, validate_bpm


@dataclass(frozen=True)
class TimelineSnapshot:
    bpm: float
    beat: float
    time: float
    running: bool
    revision: int
    position_confirmed: bool
    source: str
    active_source: str
    status: str
    reset_revision: int


class MusicalTimeline:
    SOURCES = ("auto", "internal", "link", "beatstep")
    BAR = 4.0
    CLOCK_WINDOW = 24
    CLOCK_MINIMUM = 12

    def __init__(self, link: LinkSync, *, clock=None):
        self.link = link
        self._clock = clock or time.monotonic
        self._lock = threading.RLock()
        self._bpm = validate_bpm(link.bpm)
        self._beat = 0.0
        self._time = self._clock()
        self._phase_beat = 0.0
        self._phase_time = self._time
        self._phase_offset = 0.0
        self._running = True
        self._revision = 0
        self._reset_revision = 0
        self._confirmed = True
        self._source = "auto"
        self._active = "internal"
        self._pending = None
        self._intervals = deque(maxlen=self.CLOCK_WINDOW)
        self._last_pulse = None
        self._midi_bpm = self._bpm
        self._midi_beat = 0.0
        self._midi_time = self._time
        self._midi_running = True
        self._midi_confirmed = False
        self._first_pulse = True
        self._stable = False
        self._link_playing = None
        self._link_offset = 0.0
        self._last_publish = -math.inf
        self._last_phase_publish = -math.inf
        self._publish_phase = False
        self._publish_transport = False
        self._link_was_enabled = link.enabled
        self._cached = self._make_snapshot(self._time)

    def _position(self, now):
        return self._beat + ((now - self._time) * self._bpm / 60 if self._running else 0)

    def _phase_position(self, now):
        return self._phase_beat + (now - self._phase_time) * self._bpm / 60

    def _anchor(self, now, beat=None, bpm=None):
        phase = self._phase_position(now)
        if beat is not None and self._running:
            phase = beat + self._phase_offset
        self._phase_beat, self._phase_time = phase, now
        self._beat = self._position(now) if beat is None else beat
        self._time = now
        if bpm is not None:
            self._bpm = bpm

    def _midi_valid(self, now):
        return (self._stable and self._last_pulse is not None
                and now - self._last_pulse <= max(0.5, 8 * 60 / self._midi_bpm / 24))

    def _wanted(self, now):
        if self._source != "auto":
            if self._source == "beatstep" and not self._midi_valid(now):
                return "internal"
            if self._source == "link" and not self.link.enabled:
                return "internal"
            return self._source
        if self._midi_valid(now):
            return "beatstep"
        if self.link.enabled and self.link.num_peers:
            return "link"
        return "internal"

    def _midi_position(self, now):
        if not self._midi_running:
            return self._midi_beat
        return self._midi_beat + (now - self._midi_time) * self._midi_bpm / 60

    def _acquire(self, source, now):
        old = beat = self._position(now)
        if self._active == "beatstep" and source == "internal" and self._midi_running:
            self._midi_confirmed = False
            self._first_pulse = True
        self._anchor(now)
        self._active = source
        self._pending = None
        if source == "beatstep":
            beat = self._midi_position(now)
            self._anchor(now, beat if self._running else old, self._midi_bpm)
            self._confirmed = self._midi_confirmed
            # Acquisition never implicitly starts or stops transport.
            self._publish_phase = True
            self._publish_transport = True
        elif source == "link":
            bpm, beat, sampled = self.link.timeline()
            beat += (now - sampled) * bpm / 60
            self._link_offset = 0.0
            self._anchor(now, beat if self._running else old, bpm)
            self._link_playing = self.link.playing
            self._confirmed = True
        if source in ("beatstep", "link"):
            # Deck phase follows the acquired clock, independently of a
            # stopped sequence position or a previous Stop/Continue offset.
            self._phase_beat, self._phase_time = beat, now
            self._phase_offset = beat - self._beat
        if abs(self._beat - old) > 1e-6:
            self._revision += 1

    def _advance(self, now):
        if self.link.enabled and not self._link_was_enabled and self._active == "beatstep":
            self._publish_phase = self._publish_transport = True
        self._link_was_enabled = self.link.enabled
        wanted = self._wanted(now)
        if wanted != self._active:
            lost = ((self._active == "beatstep" and not self._midi_valid(now))
                    or (self._active == "link" and not self.link.enabled))
            if wanted == "internal" or lost:
                self._acquire("internal", now)
            if wanted != self._active:
                if self._pending is None or self._pending[0] != wanted:
                    boundary = (math.floor(self._position(now) / self.BAR) + 1) * self.BAR
                    self._pending = (wanted, boundary)
                if not self._running or self._position(now) >= self._pending[1]:
                    self._acquire(wanted, now)
        else:
            self._pending = None
        if self._active == "beatstep" and self._midi_valid(now):
            # Pulse phase corrections are bounded: jitter never runs time back.
            expected = self._midi_position(now)
            current = self._position(now)
            correction = max(-0.02, min(0.02, expected - current))
            if self._running:
                self._anchor(now, max(self._beat, current + correction), self._midi_bpm)
            else:
                self._anchor(now, bpm=self._midi_bpm)
            if self.link.enabled and (self._publish_phase or now - self._last_publish >= 0.25):
                phase = self._publish_phase
                # Re-align at most once per bar, and only for material drift.
                if not phase and now - self._last_phase_publish >= 240 / self._bpm:
                    peer_beat = self.link.timeline()[1]
                    phase = abs((peer_beat - self._phase_position(now) + 2) % 4 - 2) > 0.125
                self.link.publish(self._bpm, self._phase_position(now), self._running,
                                  phase=phase, transport=self._publish_transport)
                self._last_publish = now
                if phase:
                    self._last_phase_publish = now
                self._publish_phase = self._publish_transport = False
        elif self._active == "link":
            bpm, beat, sampled = self.link.timeline()
            playing = self.link.playing
            if playing is not None and self._link_playing is not None and playing != self._link_playing:
                self._anchor(now)
                self._running = playing
                if playing:
                    self._link_offset = self._beat - beat
                    self._phase_offset = self._phase_position(now) - self._beat
            self._link_playing = playing
            if self._running:
                target = beat + (now - sampled) * bpm / 60 + self._link_offset
                if abs(target - self._position(now)) > 0.25:
                    self._revision += 1
                self._anchor(now, target, bpm)
            else:
                self._anchor(now, bpm=bpm)

    def _make_snapshot(self, now):
        if self._active == "beatstep":
            status = "locked" if self._confirmed else "clock locked; position unconfirmed"
        elif self._source == "beatstep" or (self._last_pulse is not None and not self._midi_valid(now)):
            status = "holdover" if self._last_pulse is not None else "waiting for clock"
        else:
            status = self._active
        if self._pending:
            status += f"; acquiring {self._pending[0]} at bar"
        return TimelineSnapshot(self._bpm, self._position(now), now, self._running,
                                self._revision, self._confirmed, self._source,
                                self._active, status, self._reset_revision)

    def snapshot(self):
        with self._lock:
            now = self._clock()
            self._advance(now)
            self._cached = self._make_snapshot(now)
            return self._cached

    @property
    def cached_snapshot(self):
        return self._cached

    def timeline(self):
        sample = self.snapshot()
        return sample.bpm, sample.beat, sample.time

    def clock_timeline(self):
        """Continuous deck phase; transport Stop only freezes sequence position."""
        with self._lock:
            sample = self.snapshot()
            return sample.bpm, self._phase_position(sample.time), sample.time

    @property
    def bpm(self):
        return self.snapshot().bpm

    @bpm.setter
    def bpm(self, value):
        value = validate_bpm(value)
        with self._lock:
            now = self._clock()
            self._advance(now)
            self._anchor(now, bpm=value)
            self.link.bpm = value
            self._cached = self._make_snapshot(now)

    @property
    def running(self):
        return self.snapshot().running

    @property
    def revision(self):
        return self.snapshot().revision

    @property
    def position_confirmed(self):
        return self.snapshot().position_confirmed

    @property
    def source(self):
        return self._source

    @property
    def active_source(self):
        return self.snapshot().active_source

    @property
    def status(self):
        return self.snapshot().status

    def select_source(self, name):
        if name not in self.SOURCES:
            raise ValueError(f"clock source must be one of {', '.join(self.SOURCES)}")
        with self._lock:
            self._source = name
            self._pending = None
            self.snapshot()

    def _transport(self, *, running=None, beat=None, midi=False, timestamp=None):
        now = self._clock() if timestamp is None else timestamp
        was_running = self._running
        self._anchor(now, beat)
        if midi and self._active == "link":
            self._active = "internal"
            self._pending = None
        if running is not None:
            self._running = running
        if beat is not None:
            self._revision += 1
            self._reset_revision += 1
            self._confirmed = True
            self._phase_beat, self._phase_time = beat, now
            self._phase_offset = 0.0
        elif running and not was_running:
            self._phase_offset = self._phase_position(now) - self._beat
        if not midi:
            self._midi_beat = self._beat
            self._midi_running = self._running
            if beat is not None:
                self._midi_confirmed = True
            self._first_pulse = True
            self._midi_time = now
            if self._active == "link":
                self._link_offset = 0.0 if beat is not None else self._beat - self.link.timeline()[1]
                self._link_playing = self._running
        self.link.publish(self._bpm, self._beat, self._running,
                          phase=beat is not None, transport=True)
        self._cached = self._make_snapshot(now)

    def start(self):
        with self._lock:
            self._transport(running=True, beat=0.0)

    def stop(self):
        with self._lock:
            self._transport(running=False)

    def resume(self):
        with self._lock:
            self._transport(running=True)

    def align(self):
        """Declare the current instant to be beat one, without starting playback."""
        with self._lock:
            self._transport(beat=0.0)

    def midi_message(self, raw, timestamp=None):
        if not raw:
            return
        status = int(raw[0])
        if status not in (0xF8, 0xFA, 0xFB, 0xFC, 0xF2):
            return
        with self._lock:
            now = self._clock() if timestamp is None else float(timestamp)
            if not math.isfinite(now):
                return
            authority = self._source in ("auto", "beatstep")
            if status == 0xF8:
                if self._last_pulse is not None:
                    interval = now - self._last_pulse
                    if interval <= 0:
                        return
                    if interval > max(0.5, 8 * 60 / self._midi_bpm / 24):
                        self._intervals.clear()
                        self._stable = False
                        if not self._midi_confirmed:
                            self._midi_beat = self._position(now)
                    self._intervals.append(interval)
                    if len(self._intervals) >= self.CLOCK_MINIMUM:
                        median = statistics.median(self._intervals)
                        good = [x for x in self._intervals if abs(x - median) <= median * 0.2]
                        self._stable = len(good) >= len(self._intervals) * 0.8
                        if self._stable:
                            self._midi_bpm = validate_bpm(60 / (statistics.mean(good) * 24))
                elif not self._midi_confirmed:
                    self._midi_beat = self._position(now)
                if self._midi_running and not self._first_pulse:
                    self._midi_beat += 1 / 24
                self._first_pulse = False
                self._last_pulse = now
                self._midi_time = now
                self.snapshot()
                return
            if status == 0xF2:
                if len(raw) < 3 or not 0 <= raw[1] < 128 or not 0 <= raw[2] < 128:
                    return
                self._midi_beat = (raw[1] | raw[2] << 7) / 4
                self._midi_confirmed = True
                self._first_pulse = True
                if authority:
                    self._transport(beat=self._midi_beat, midi=True, timestamp=now)
            elif status == 0xFA:
                self._midi_beat = 0.0
                self._midi_running = self._midi_confirmed = True
                self._first_pulse = True
                if authority:
                    self._transport(running=True, beat=0.0, midi=True, timestamp=now)
            elif status == 0xFC:
                self._midi_beat = self._midi_position(now)
                self._midi_running = False
                if authority:
                    self._transport(running=False, midi=True, timestamp=now)
                    self._midi_beat = self._beat
            else:
                self._midi_running = True
                self._first_pulse = True
                if authority:
                    self._transport(running=True, midi=True, timestamp=now)
            self._midi_time = now

    def disconnect_midi(self):
        """Lose clock authority, not transport: preserve beat and tempo."""
        with self._lock:
            self._stable = False
            self._intervals.clear()
            self._midi_confirmed = False
            self._first_pulse = True
            self.snapshot()
