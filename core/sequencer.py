"""Internal step sequencer -- tempo-synced note patterns attached to slots.

Each *sequence bank* (1-based, up to NUM_SEQ_BANKS) holds a list of note
names that loop over one bar.  When a bank is linked to a slot the
sequencer thread sends note-on / note-off messages into that slot's MIDI
queue at the correct tempo-derived intervals.

Timing model:
  - One bar = 4 beats at the current BPM.
  - Notes are evenly spaced across the bar.  If there is 1 note it plays
    once per bar (on beat 1).  If there are 4 notes each plays on a
    quarter-note boundary, etc.

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

    def _run(self):
        """Main sequencer loop with per-bank timing.

        Each bank has its own next-fire timestamp so that a 1-note bank
        fires once per bar while a 4-note bank fires every quarter-note,
        even when both are active simultaneously.

        The loop sleeps until the earliest upcoming bank step, fires all
        banks that are due, and schedules note-off timers.
        """
        from core import deps  # late import to avoid circular deps

        mido = deps.mido
        if mido is None:
            logger.error("[SEQ] mido not available, sequencer cannot run")
            self._running = False
            return

        # Per-bank next-fire timestamps (initialised on first active tick).
        next_fire: list[float] = [0.0] * NUM_SEQ_BANKS
        now = time.monotonic()
        for bi in range(NUM_SEQ_BANKS):
            next_fire[bi] = now  # all banks start immediately

        while self._running:
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
                # No active banks -- sleep briefly and re-check.
                self._stop_event.wait(timeout=0.05)
                continue

            # Sleep until the earliest bank is due.
            sleep_dur = earliest - now
            if sleep_dur > 0.0005:  # > 0.5 ms
                self._stop_event.wait(timeout=sleep_dur)
                if not self._running:
                    break

            now = time.monotonic()

            # Fire all banks whose step time has arrived (within 1 ms tolerance).
            fired: list[tuple[int, int, float]] = []  # (slot_idx, midi_note, step_dur)

            for bi, bank in enumerate(self.banks):
                if bank is None or bank.linked_slot is None or not bank.notes:
                    continue
                if next_fire[bi] > now + 0.001:
                    continue  # not due yet

                n_steps = len(bank.notes)
                step_dur = bar_duration / n_steps
                cursor = self._cursors[bi]

                midi_note = bank.notes[cursor % n_steps]
                slot_idx = bank.linked_slot
                vel = bank.velocity

                on = mido.Message("note_on", note=midi_note, velocity=vel)
                self._host.engine.enqueue_midi(slot_idx, on)
                fired.append((slot_idx, midi_note, step_dur))

                # Advance cursor and schedule next fire.
                self._cursors[bi] = (cursor + 1) % n_steps
                next_fire[bi] += step_dur

                # Guard against drift: if we fell behind, snap forward.
                if next_fire[bi] < now:
                    next_fire[bi] = now

            # Schedule note-off for each fired note at 90% of its step duration.
            for slot_idx, midi_note, step_dur in fired:
                off_delay = step_dur * self.NOTE_OFF_RATIO

                def _send_off(si=slot_idx, mn=midi_note, _mido=mido):
                    off = _mido.Message("note_off", note=mn)
                    self._host.engine.enqueue_midi(si, off)

                off_timer = threading.Timer(off_delay, _send_off)
                off_timer.daemon = True
                off_timer.start()

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
