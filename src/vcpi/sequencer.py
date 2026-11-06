"""Step patterns scheduled on the host's shared musical timeline.

A bar is four beats. Each pattern divides that bar independently, so mixed
lengths need no common-grid polling or separate Link/freewheel loops. Newly
linked patterns launch on the next bar; explicit transport alignment can launch
at beat zero. Worker shutdown is independent of musical transport Stop.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import logging
import math
import re
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vcpi.host import VcpiCore

logger = logging.getLogger(__name__)
NUM_SEQ_BANKS = 16
_NOTE_NAMES = {
    "c": 0, "c#": 1, "db": 1, "d": 2, "d#": 3, "eb": 3,
    "e": 4, "fb": 4, "e#": 5, "f": 5, "f#": 6, "gb": 6,
    "g": 7, "g#": 8, "ab": 8, "a": 9, "a#": 10, "bb": 10,
    "b": 11, "cb": 11, "b#": 0,
}
_NOTE_RE = re.compile(r"^([A-Ga-g][#b]?)(\d)?$")
_MIDI_TO_NAME = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def note_name_to_midi(name: str, default_octave: int = 4) -> int:
    """Convert a note name such as C, Db3 or F#6 to a MIDI note number."""
    match = _NOTE_RE.match(name.strip())
    if not match:
        raise ValueError(f"invalid note name: {name!r}")
    octave = int(match.group(2)) if match.group(2) is not None else default_octave
    midi = (octave + 1) * 12 + _NOTE_NAMES[match.group(1).lower()]
    if not 0 <= midi <= 127:
        raise ValueError(f"MIDI note {midi} out of range for {name!r}")
    return midi


def midi_to_note_name(midi: int) -> str:
    return f"{_MIDI_TO_NAME[midi % 12]}{midi // 12 - 1}"


@dataclass
class SequenceBank:
    notes: list[int] = field(default_factory=list)
    velocity: int = 100
    linked_track: int | None = None


@dataclass
class _Playback:
    signature: tuple
    launch: float
    revision: int
    reset_revision: int
    last_step: int | None = None
    held: tuple[int, int, int, float] | None = None


class Sequencer:
    NOTE_OFF_RATIO = 0.9
    POLL_SECONDS = 0.01

    def __init__(self, host: VcpiCore):
        self._host = host
        self.banks: list[SequenceBank | None] = [None] * NUM_SEQ_BANKS
        self._running = False
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.RLock()
        self._playback: dict[int, _Playback] = {}

    @staticmethod
    def _check_index(index):
        if not 0 <= index < NUM_SEQ_BANKS:
            raise ValueError(f"bank must be 1-{NUM_SEQ_BANKS}")

    def set_bank(self, bank_index, note_names, velocity=100):
        self._check_index(bank_index)
        notes = [note_name_to_midi(name) for name in note_names]
        with self._lock:
            bank = self.banks[bank_index]
            if bank is None:
                bank = self.banks[bank_index] = SequenceBank()
            bank.notes = notes
            bank.velocity = velocity
            self._discard(bank_index)
            if bank.linked_track is not None:
                self._playback[bank_index] = self._pending_playback(
                    bank, self._host.timeline.snapshot())
            return bank

    def clear_bank(self, bank_index):
        self._check_index(bank_index)
        with self._lock:
            self._discard(bank_index)
            self.banks[bank_index] = None

    def link(self, bank_index, track_index):
        self._check_index(bank_index)
        with self._lock:
            bank = self.banks[bank_index]
            if bank is None:
                raise ValueError(f"sequence bank {bank_index + 1} is empty")
            self._discard(bank_index)
            bank.linked_track = track_index
            sample = self._host.timeline.snapshot()
            self._playback[bank_index] = self._pending_playback(bank, sample)
        self.start()

    def detach_track(self, track_index):
        with self._lock:
            for index, bank in enumerate(self.banks):
                if bank is not None and bank.linked_track == track_index:
                    self._discard(index)
                    bank.linked_track = None
            linked = any(b and b.linked_track is not None for b in self.banks)
        if not linked:
            self.stop()

    def detach_bank(self, bank_index):
        self._check_index(bank_index)
        with self._lock:
            self._discard(bank_index)
            bank = self.banks[bank_index]
            if bank is not None:
                bank.linked_track = None
            linked = any(b and b.linked_track is not None for b in self.banks)
        if not linked:
            self.stop()

    def start(self):
        """Start the worker, not musical transport."""
        if self._running:
            return
        with self._lock:
            sample = self._host.timeline.snapshot()
            for index, bank in enumerate(self.banks):
                if bank is not None and bank.linked_track is not None and index not in self._playback:
                    self._playback[index] = self._pending_playback(bank, sample)
        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="vcpi-sequencer", daemon=True)
        self._thread.start()

    def stop(self):
        """Shut down the worker and release every held note exactly once."""
        self._running = False
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None
        with self._lock:
            self._silence()
            self._playback.clear()

    @staticmethod
    def _signature(bank):
        return bank.linked_track, tuple(bank.notes), bank.velocity

    def _release(self, playback):
        if playback.held is not None:
            from vcpi import deps
            track, note, generation, _ = playback.held
            playback.held = None
            self._host.engine.enqueue_midi(
                track, deps.mido.Message("note_off", note=note), generation=generation)

    def _discard(self, index):
        playback = self._playback.pop(index, None)
        if playback is not None:
            self._release(playback)

    def _silence(self):
        first_error = None
        for playback in self._playback.values():
            try:
                self._release(playback)
            except Exception as error:
                if first_error is None:
                    first_error = error
        if first_error is not None:
            raise first_error

    def _pending_playback(self, bank, sample):
        bar = self._host.timeline.BAR
        return _Playback(self._signature(bank), (math.floor(sample.beat / bar) + 1) * bar,
                         sample.revision, sample.reset_revision)

    def _tick(self, sample, mido):
        """Schedule a single atomic timeline sample; return the next wait."""
        with self._lock:
            # A bank can be linked after the worker samples the timeline.
            # Never interpret an older sample as a new transport reset.
            if any(sample.revision < playback.revision
                   or sample.reset_revision < playback.reset_revision
                   for playback in self._playback.values()):
                return self.POLL_SECONDS
            beat = sample.beat
            bar = self._host.timeline.BAR
            if not sample.running:
                self._silence()
            next_beat = math.inf
            for index, bank in enumerate(self.banks):
                if bank is None or bank.linked_track is None or not bank.notes:
                    self._discard(index)
                    continue
                signature = self._signature(bank)
                playback = self._playback.get(index)
                if playback is None or playback.signature != signature:
                    self._discard(index)
                    playback = self._playback[index] = self._pending_playback(bank, sample)
                quantum = bar / len(bank.notes)
                if playback.revision != sample.revision:
                    self._release(playback)
                    if playback.reset_revision != sample.reset_revision:
                        playback.launch = math.floor(beat / bar) * bar
                        playback.last_step = None
                    elif playback.last_step is None:
                        # Acquiring a source is not a launch command.
                        playback.launch = (math.floor(beat / bar) + 1) * bar
                    else:
                        # A source phase jump never replays the current step.
                        playback.last_step = math.floor((beat + 1e-9) / quantum)
                    playback.revision = sample.revision
                    playback.reset_revision = sample.reset_revision
                if not sample.running:
                    continue
                if playback.held is not None and beat >= playback.held[3]:
                    self._release(playback)
                if playback.last_step is None and beat + 1e-9 < playback.launch:
                    next_beat = min(next_beat, playback.launch)
                    continue
                step = math.floor((beat + 1e-9) / quantum)
                # Continue retains the cursor: it does not retrigger a note
                # already played before Stop, or resurrect its canceled gate.
                if step != playback.last_step:
                    self._release(playback)
                    note = bank.notes[step % len(bank.notes)]
                    track_index = bank.linked_track
                    track = self._host.engine.tracks[track_index]
                    generation = track.generation if track is not None else 0
                    off_beat = (step + self.NOTE_OFF_RATIO) * quantum
                    # Late wakeups skip expired gates, never burst through
                    # missed steps or emit an on/off pair for a dead gate.
                    if beat < off_beat:
                        self._host.engine.enqueue_midi(
                            track_index, mido.Message("note_on", note=note, velocity=bank.velocity),
                            generation=generation)
                        playback.held = (track_index, note, generation, off_beat)
                    playback.last_step = step
                next_beat = min(next_beat, (step + 1) * quantum)
                if playback.held is not None:
                    next_beat = min(next_beat, playback.held[3])
            return min(self.POLL_SECONDS, max(0.0001, (next_beat - beat) * 60 / sample.bpm))

    def _run(self):
        from vcpi import deps
        try:
            if deps.mido is None:
                raise RuntimeError("mido not available, sequencer cannot run")
            while self._running:
                delay = self._tick(self._host.timeline.snapshot(), deps.mido)
                self._stop_event.wait(delay)
        except Exception:
            logger.exception("[SEQ] playback failed")
        finally:
            self._running = False
            self._stop_event.set()
            with self._lock:
                self._silence()

    def snapshot(self):
        with self._lock:
            return [None if bank is None else {
                "notes": [midi_to_note_name(n) for n in bank.notes],
                "velocity": bank.velocity,
                "linked_track": bank.linked_track + 1 if bank.linked_track is not None else None,
            } for bank in self.banks]

    def restore(self, data):
        with self._lock:
            self._silence()
            self._playback.clear()
            self.banks = [None] * NUM_SEQ_BANKS
            sample = self._host.timeline.snapshot()
            for index, entry in enumerate(data[:NUM_SEQ_BANKS]):
                if entry is None:
                    continue
                try:
                    linked = entry.get("linked_track")
                    self.banks[index] = SequenceBank(
                        notes=[note_name_to_midi(n) for n in entry.get("notes", [])],
                        velocity=entry.get("velocity", 100),
                        linked_track=int(linked) - 1 if linked is not None else None)
                    if linked is not None:
                        self._playback[index] = self._pending_playback(self.banks[index], sample)
                except Exception as error:
                    logger.warning("[SEQ] restore bank %d failed: %s", index + 1, error)
            linked = any(b and b.linked_track is not None for b in self.banks)
        if linked:
            self.start()
