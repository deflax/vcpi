"""Internal step sequencer -- tempo-synced note patterns attached to slots.

Each *sequence bank* (1-based, up to NUM_SEQ_BANKS) holds a list of note
names that loop over one bar.  When a bank is linked to a slot the
sequencer thread sends note-on / note-off messages into that slot's MIDI
queue at the correct tempo-derived intervals.

Timing model
~~~~~~~~~~~~
  - One bar = 4 beats at the current BPM.
  - Notes are evenly spaced across the bar.  If there is 1 note it plays
    once per bar (on beat 1).  If there are 4 notes each plays on a
    quarter-note boundary, etc.

Ableton Link bar alignment
~~~~~~~~~~~~~~~~~~~~~~~~~~
When Ableton Link is enabled (``ableton link``), the sequencer uses
``LinkSync.sync()`` to sleep until the shared Link beat-grid boundary
rather than free-running on wall-clock time.  This means:

  - The first note of every bar aligns with the downbeat as seen by
    Ableton Live and all other Link peers.
  - Tempo changes from any peer are followed automatically.
  - If Link is disabled or aalink is not installed, the sequencer falls
    back to the original wall-clock timing (still tempo-synced via the
    ``bpm`` property, but not phase-aligned with external peers).

Note names are case-insensitive: c, C#, Db, d, ... b.  The default octave
is 4 (middle C = C4 = MIDI 60).  An explicit octave suffix is allowed:
``C5``, ``Bb3``, ``F#6``.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from core.host import VcpiCore

logger = logging.getLogger(__name__)

NUM_SEQ_BANKS = 16  # max sequence banks (1-16 user-facing)

# ---------------------------------------------------------------------------
# Note-name -> MIDI number conversion
# ---------------------------------------------------------------------------

_NOTE_NAMES = {
    "c": 0, "c#": 1, "db": 1,
    "d": 2, "d#": 3, "eb": 3,
    "e": 4, "fb": 4, "e#": 5,
    "f": 5, "f#": 6, "gb": 6,
    "g": 7, "g#": 8, "ab": 8,
    "a": 9, "a#": 10, "bb": 10,
    "b": 11, "cb": 11, "b#": 0,
}

_NOTE_RE = re.compile(
    r"^([A-Ga-g][#b]?)(\d)?$"
)

_MIDI_TO_NAME = [
    "C", "C#", "D", "D#", "E", "F",
    "F#", "G", "G#", "A", "A#", "B",
]


def note_name_to_midi(name: str, default_octave: int = 4) -> int:
    """Convert a note name like 'C', 'c#', 'Bb3', 'F#6' to a MIDI number.

    Raises ValueError on unrecognised input.
    """
    m = _NOTE_RE.match(name.strip())
    if not m:
        raise ValueError(f"invalid note name: {name!r}")
    pitch_str = m.group(1).lower()
    octave = int(m.group(2)) if m.group(2) is not None else default_octave
    semitone = _NOTE_NAMES.get(pitch_str)
    if semitone is None:
        raise ValueError(f"unknown pitch: {pitch_str!r}")
    midi = (octave + 1) * 12 + semitone
    if not 0 <= midi <= 127:
        raise ValueError(f"MIDI note {midi} out of range for {name!r}")
    return midi


def midi_to_note_name(midi: int) -> str:
    """Convert MIDI number to readable name like 'C4', 'F#5'."""
    octave = (midi // 12) - 1
    semitone = midi % 12
    return f"{_MIDI_TO_NAME[semitone]}{octave}"


# ---------------------------------------------------------------------------
# Sequence bank data
# ---------------------------------------------------------------------------

@dataclass
class SequenceBank:
    """One named sequence pattern."""
    notes: list[int] = field(default_factory=list)  # MIDI note numbers
    velocity: int = 100
    linked_slot: Optional[int] = None  # 0-based slot index, or None


# ---------------------------------------------------------------------------
# Sequencer engine (background thread)
# ---------------------------------------------------------------------------

class Sequencer:
    """Manages sequence banks and a tempo-synced playback thread.

    The thread wakes up at each step boundary and fires note-on events
    into the host's MIDI queue.  Note-off is sent just before the next
    step (95 % of step duration) to avoid overlapping sustain.
    """

    NOTE_OFF_RATIO = 0.9  # fraction of step duration before note-off

    def __init__(self, host: VcpiCore):
        self._host = host
        self.banks: list[Optional[SequenceBank]] = [None] * NUM_SEQ_BANKS

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        # Per-bank playback cursor (step index within the pattern).
        self._cursors: list[int] = [0] * NUM_SEQ_BANKS

    # -- bank management -----------------------------------------------------

    def set_bank(self, bank_index: int, note_names: list[str],
                 velocity: int = 100) -> SequenceBank:
        """Create / overwrite a sequence bank from note name strings."""
        if not 0 <= bank_index < NUM_SEQ_BANKS:
            raise ValueError(f"bank must be 1-{NUM_SEQ_BANKS}")
        midi_notes = [note_name_to_midi(n) for n in note_names]
        bank = self.banks[bank_index]
        if bank is None:
            bank = SequenceBank(notes=midi_notes, velocity=velocity)
            self.banks[bank_index] = bank
        else:
            bank.notes = midi_notes
            bank.velocity = velocity
        self._cursors[bank_index] = 0
        logger.info("[SEQ] bank %d set: %s", bank_index + 1,
                    [midi_to_note_name(n) for n in midi_notes])
        return bank

    def clear_bank(self, bank_index: int):
        """Remove a sequence bank and detach it from any slot."""
        if not 0 <= bank_index < NUM_SEQ_BANKS:
            raise ValueError(f"bank must be 1-{NUM_SEQ_BANKS}")
        self.banks[bank_index] = None
        self._cursors[bank_index] = 0

    def link(self, bank_index: int, slot_index: int):
        """Attach sequence bank to a slot."""
        if not 0 <= bank_index < NUM_SEQ_BANKS:
            raise ValueError(f"bank must be 1-{NUM_SEQ_BANKS}")
        bank = self.banks[bank_index]
        if bank is None:
            raise ValueError(f"sequence bank {bank_index + 1} is empty")
        bank.linked_slot = slot_index
        self._cursors[bank_index] = 0
        logger.info("[SEQ] bank %d -> slot %d", bank_index + 1, slot_index + 1)
        # Auto-start the playback thread when a link is made.
        self.start()

    def detach_slot(self, slot_index: int):
        """Remove any sequence link(s) from the given slot."""
        for bank in self.banks:
            if bank is not None and bank.linked_slot == slot_index:
                bank.linked_slot = None
        # If no banks are linked any more, stop the thread.
        if not any(b is not None and b.linked_slot is not None
                   for b in self.banks):
            self.stop()

    def detach_bank(self, bank_index: int):
        """Remove the link from a specific bank."""
        if not 0 <= bank_index < NUM_SEQ_BANKS:
            raise ValueError(f"bank must be 1-{NUM_SEQ_BANKS}")
        bank = self.banks[bank_index]
        if bank is not None:
            bank.linked_slot = None
        if not any(b is not None and b.linked_slot is not None
                   for b in self.banks):
            self.stop()

    # -- playback thread -----------------------------------------------------

    def start(self):
        """Start the sequencer playback thread (idempotent)."""
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="vcpi-sequencer", daemon=True)
        self._thread.start()
        logger.info("[SEQ] playback thread started")

    def stop(self):
        """Stop the sequencer playback thread (idempotent)."""
        if not self._running:
            return
        self._running = False
        self._stop_event.set()  # wake up the sleep immediately
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        logger.info("[SEQ] playback thread stopped")

    @property
    def _bpm(self) -> float:
        return self._host.link.bpm

    @property
    def _link_enabled(self) -> bool:
        return self._host.link.enabled

    # -- internal helpers ----------------------------------------------------

    def _smallest_quantum(self) -> float:
        """Return the smallest step size (in beats) across all active banks.

        When Link is enabled the sequencer sleeps in increments of this
        quantum so that every active bank's steps land on the grid.
        A bank with *n* notes has a step size of 4/n beats (4 beats per
        bar).  The GCD-style smallest value ensures we wake up often
        enough for the densest pattern while still aligning coarser
        patterns to the same grid.
        """
        from math import gcd
        nums: list[int] = []
        for bank in self.banks:
            if bank is not None and bank.linked_slot is not None and bank.notes:
                nums.append(len(bank.notes))
        if not nums:
            return 1.0  # quarter note fallback
        # LCM of all step counts gives the common grid divisor.
        lcm = nums[0]
        for n in nums[1:]:
            lcm = lcm * n // gcd(lcm, n)
        return 4.0 / lcm  # beats

    def _fire_banks(self, step_index: int, quantum_beats: float,
                    mido) -> list[tuple[int, int, float]]:
        """Fire notes for all banks whose step falls on *step_index*.

        Returns a list of (slot_idx, midi_note, note_dur_seconds) for
        note-off scheduling.
        """
        bpm = self._bpm
        beat_dur = 60.0 / bpm  # seconds per beat
        fired: list[tuple[int, int, float]] = []

        for bi, bank in enumerate(self.banks):
            if bank is None or bank.linked_slot is None or not bank.notes:
                continue

            n_steps = len(bank.notes)
            bank_quantum = 4.0 / n_steps  # beats per step for this bank

            # This bank should fire when the current beat position within
            # the bar is a multiple of its own step size.  We check
            # whether step_index (in units of the smallest quantum) is
            # a multiple of this bank's step ratio.
            steps_per_bank_step = round(bank_quantum / quantum_beats)
            if steps_per_bank_step < 1:
                steps_per_bank_step = 1
            if step_index % steps_per_bank_step != 0:
                continue

            cursor = self._cursors[bi]
            midi_note = bank.notes[cursor % n_steps]
            slot_idx = bank.linked_slot
            vel = bank.velocity

            on = mido.Message("note_on", note=midi_note, velocity=vel)
            self._host.engine.enqueue_midi(slot_idx, on)

            step_dur_secs = bank_quantum * beat_dur
            fired.append((slot_idx, midi_note, step_dur_secs))

            self._cursors[bi] = (cursor + 1) % n_steps

        return fired

    def _schedule_note_offs(self, fired: list[tuple[int, int, float]],
                            mido) -> None:
        """Schedule note-off messages for all recently fired notes."""
        for slot_idx, midi_note, step_dur in fired:
            off_delay = step_dur * self.NOTE_OFF_RATIO

            def _send_off(si=slot_idx, mn=midi_note, _mido=mido):
                off = _mido.Message("note_off", note=mn)
                self._host.engine.enqueue_midi(si, off)

            off_timer = threading.Timer(off_delay, _send_off)
            off_timer.daemon = True
            off_timer.start()

    # -- main playback loops -------------------------------------------------

    def _run(self):
        """Main sequencer entry point -- delegates to Link or wall-clock loop."""
        from core import deps  # late import to avoid circular deps

        mido = deps.mido
        if mido is None:
            logger.error("[SEQ] mido not available, sequencer cannot run")
            self._running = False
            return

        while self._running:
            if self._link_enabled:
                self._run_link(mido)
            else:
                self._run_freewheel(mido)

    def _run_link(self, mido):
        """Link-synced loop: sleep on beat-grid boundaries.

        Uses ``LinkSync.sync(quantum)`` to block until the shared Link
        timeline reaches the next grid point.  All banks fire at
        multiples of the smallest quantum so patterns stay phase-aligned
        with Ableton Live and other Link peers.
        """
        link = self._host.link
        step_index = 0

        logger.info("[SEQ] entering Link-synced loop")

        while self._running and self._link_enabled:
            quantum = self._smallest_quantum()

            # No active banks -- idle briefly.
            if quantum >= 4.0 and not any(
                b is not None and b.linked_slot is not None and b.notes
                for b in self.banks
            ):
                self._stop_event.wait(timeout=0.05)
                continue

            # Block until the next beat-grid boundary.
            try:
                link.sync(quantum, timeout=4.0)
            except RuntimeError:
                # Link was disabled while we were waiting.
                break
            except Exception as exc:
                logger.warning("[SEQ] link.sync error: %s", exc)
                self._stop_event.wait(timeout=0.01)
                continue

            if not self._running:
                break

            fired = self._fire_banks(step_index, quantum, mido)
            self._schedule_note_offs(fired, mido)
            step_index += 1

        logger.info("[SEQ] leaving Link-synced loop")

    def _run_freewheel(self, mido):
        """Wall-clock loop (original behaviour, no Link phase alignment).

        Used when Ableton Link is not enabled.  Still reads ``bpm`` from
        the LinkSync object for tempo, but times steps with
        ``time.monotonic()`` sleeps.
        """
        next_fire: list[float] = [0.0] * NUM_SEQ_BANKS
        now = time.monotonic()
        for bi in range(NUM_SEQ_BANKS):
            next_fire[bi] = now

        logger.info("[SEQ] entering freewheel loop")

        while self._running and not self._link_enabled:
            bpm = self._bpm
            bar_duration = 240.0 / bpm  # 4 beats in seconds
            now = time.monotonic()

            # Find the earliest fire time among active banks.
            earliest = None
            for bi, bank in enumerate(self.banks):
                if bank is None or bank.linked_slot is None or not bank.notes:
                    continue
                if earliest is None or next_fire[bi] < earliest:
                    earliest = next_fire[bi]

            if earliest is None:
                self._stop_event.wait(timeout=0.05)
                continue

            sleep_dur = earliest - now
            if sleep_dur > 0.0005:
                self._stop_event.wait(timeout=sleep_dur)
                if not self._running:
                    break

            now = time.monotonic()

            fired: list[tuple[int, int, float]] = []

            for bi, bank in enumerate(self.banks):
                if bank is None or bank.linked_slot is None or not bank.notes:
                    continue
                if next_fire[bi] > now + 0.001:
                    continue

                n_steps = len(bank.notes)
                step_dur = bar_duration / n_steps
                cursor = self._cursors[bi]

                midi_note = bank.notes[cursor % n_steps]
                slot_idx = bank.linked_slot
                vel = bank.velocity

                on = mido.Message("note_on", note=midi_note, velocity=vel)
                self._host.engine.enqueue_midi(slot_idx, on)
                fired.append((slot_idx, midi_note, step_dur))

                self._cursors[bi] = (cursor + 1) % n_steps
                next_fire[bi] += step_dur

                if next_fire[bi] < now:
                    next_fire[bi] = now

            self._schedule_note_offs(fired, mido)

        logger.info("[SEQ] leaving freewheel loop")

    # -- serialisation helpers -----------------------------------------------

    def snapshot(self) -> list[Optional[dict]]:
        """Serialise all banks for session persistence."""
        result = []
        for bank in self.banks:
            if bank is None:
                result.append(None)
                continue
            result.append({
                "notes": [midi_to_note_name(n) for n in bank.notes],
                "velocity": bank.velocity,
                "linked_slot": (bank.linked_slot + 1)
                               if bank.linked_slot is not None else None,
            })
        return result

    def restore(self, data: list[Optional[dict]]):
        """Restore banks from session data."""
        had_link = False
        for bi, entry in enumerate(data):
            if bi >= NUM_SEQ_BANKS:
                break
            if entry is None:
                self.banks[bi] = None
                continue
            try:
                note_names = entry.get("notes", [])
                midi_notes = [note_name_to_midi(n) for n in note_names]
                vel = entry.get("velocity", 100)
                linked = entry.get("linked_slot")
                slot_idx = (int(linked) - 1) if linked is not None else None
                self.banks[bi] = SequenceBank(
                    notes=midi_notes, velocity=vel, linked_slot=slot_idx)
                self._cursors[bi] = 0
                if slot_idx is not None:
                    had_link = True
            except Exception as exc:
                logger.warning("[SEQ] restore bank %d failed: %s", bi + 1, exc)
                self.banks[bi] = None
        if had_link:
            self.start()
