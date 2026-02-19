"""Real-time audio engine (sounddevice output callback)."""

from __future__ import annotations

import collections
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from core.deps import HAS_SOUNDDEVICE, HAS_PEDALBOARD, Pedalboard, sd, np
from core.models import InstrumentSlot, NUM_SLOTS
from core.sampler import WavSamplerPlugin


logger = logging.getLogger(__name__)


class AudioEngine:
    """
    Renders all instrument slots into a summed stereo output each audio block.

    Per callback:
      1. Flush queued MIDI into each instrument plugin
      2. Apply queued parameter changes
      3. Render each instrument
      4. Apply per-slot insert effects
      5. Mix according to gain / mute / solo
      6. Apply master effects chain
      7. Write to output buffer
    """

    def __init__(self, sample_rate: int = 44100, buffer_size: int = 512,
                 output_channels: int = 2):
        self.sample_rate = sample_rate
        self.buffer_size = buffer_size
        self.output_channels = output_channels

        self.slots: list[Optional[InstrumentSlot]] = [None] * NUM_SLOTS
        self.master_effects: list = []  # pedalboard plugin instances
        self.master_gain: float = 1.0

        self._midi_queue: collections.deque = collections.deque()  # (slot_index, msg)
        self._param_queue: collections.deque = collections.deque()  # (slot_idx, name, val)
        self._lock = threading.Lock()  # kept for potential external use
        self._stream = None
        self._mixed_buf: Optional[np.ndarray] = None  # pre-allocated mix buffer
        self._master_board = None  # cached Pedalboard for master effects

        # Persistent thread pool for parallel slot rendering.
        # pedalboard releases the GIL during process(), so threads give
        # real parallelism across CPU cores.
        n_workers = min(os.cpu_count() or 2, NUM_SLOTS)
        self._render_pool = ThreadPoolExecutor(
            max_workers=n_workers,
            thread_name_prefix="vcpi-render",
        )
        logger.info("[Audio] render pool: %d workers", n_workers)

        # Unified MIDI channel -> slot routing (shared by all controllers)
        self.channel_map: dict[int, int] = {}  # MIDI channel (0-15) -> slot index (0-7)

    # -- routing -------------------------------------------------------------

    def route(self, midi_channel: int, slot_index: int):
        """Map a MIDI channel (0-15) to a slot index (0-7)."""
        if not 0 <= midi_channel < 16:
            raise ValueError("MIDI channel must be 1-16")
        if not 0 <= slot_index < NUM_SLOTS:
            raise ValueError(f"slot must be 1-{NUM_SLOTS}")

        prev_idx = self.channel_map.get(midi_channel)
        if prev_idx is not None and prev_idx != slot_index:
            prev_slot = self.slots[prev_idx]
            if prev_slot:
                prev_slot.midi_channels.discard(midi_channel)
            logger.info(
                "route update: ch %d slot %d -> slot %d",
                midi_channel + 1, prev_idx + 1, slot_index + 1,
            )

        self.channel_map[midi_channel] = slot_index
        slot = self.slots[slot_index]
        if slot:
            slot.midi_channels.add(midi_channel)

        if prev_idx is None:
            logger.info("route set: ch %d -> slot %d",
                        midi_channel + 1, slot_index + 1)

    def unroute(self, midi_channel: int):
        """Remove a MIDI channel routing."""
        idx = self.channel_map.pop(midi_channel, None)
        if idx is not None:
            slot = self.slots[idx]
            if slot:
                slot.midi_channels.discard(midi_channel)
            logger.info("route removed: ch %d (slot %d)",
                        midi_channel + 1, idx + 1)

    # -- MIDI queueing -------------------------------------------------------

    def enqueue_midi(self, slot_index: int, msg):
        """Thread-safe enqueue of a mido.Message for a given slot.

        Uses collections.deque which is thread-safe for append/popleft
        under CPython (no lock needed).
        """
        self._midi_queue.append((slot_index, msg))

    # -- Parameter change queueing -------------------------------------------

    def enqueue_param_change(self, slot_index: int, param_name: str, value):
        """Thread-safe enqueue of a parameter change (called from controller threads)."""
        self._param_queue.append((slot_index, param_name, value))

    # -- solo logic ----------------------------------------------------------

    def any_solo(self) -> bool:
        return any(s.solo for s in self.slots if s is not None)

    # -- per-slot rendering (called from worker threads) ----------------------

    def _render_slot(self, idx: int, slot: InstrumentSlot,
                     midi_msgs: list, frames: int) -> Optional[np.ndarray]:
        """Render one slot and return (frames, channels) audio or None.

        This runs on a pool worker thread.  pedalboard releases the GIL
        during process(), so multiple slots render in true parallel.
        """
        try:
            if isinstance(slot.plugin, WavSamplerPlugin):
                for msg in midi_msgs:
                    try:
                        slot.plugin.send_midi(msg)
                    except Exception:
                        logger.debug("[Audio] send_midi error slot %d", idx,
                                     exc_info=True)
                silence = np.zeros((self.output_channels, frames),
                                   dtype=np.float32)
                rendered = slot.plugin.process(silence, self.sample_rate)
            else:
                duration = frames / self.sample_rate
                for msg in midi_msgs:
                    msg.time = 0
                rendered = slot.plugin.process(
                    midi_msgs,
                    duration=duration,
                    sample_rate=self.sample_rate,
                    num_channels=self.output_channels,
                    buffer_size=frames,
                    reset=False,
                )

            # Per-slot insert effects
            if slot.effects and HAS_PEDALBOARD:
                if not hasattr(slot, '_effects_board') or slot._effects_board is None:
                    slot._effects_board = Pedalboard(slot.effects)
                rendered = slot._effects_board(rendered, self.sample_rate, reset=False)

            # rendered: (channels, frames) -> transpose to (frames, channels)
            rt = rendered.T
            if rt.shape[1] == 1 and self.output_channels == 2:
                rt = np.column_stack([rt, rt])
            elif rt.shape[1] > self.output_channels:
                rt = rt[:, :self.output_channels]
            return rt

        except Exception:
            logger.debug("[Audio] render error slot %d", idx, exc_info=True)
            return None

    # -- audio callback ------------------------------------------------------

    def _callback(self, outdata, frames: int, time_info, status):
        if status:
            logger.warning("[Audio] %s", status)

        # Use pre-allocated mix buffer (avoid allocation in RT path)
        if (self._mixed_buf is None
                or self._mixed_buf.shape != (frames, self.output_channels)):
            self._mixed_buf = np.zeros((frames, self.output_channels),
                                       dtype=np.float32)
        mixed = self._mixed_buf
        mixed[:] = 0.0

        # Drain lock-free MIDI queue into per-slot lists
        queues: dict[int, list] = {}
        while self._midi_queue:
            try:
                slot_idx, msg = self._midi_queue.popleft()
            except IndexError:
                break
            queues.setdefault(slot_idx, []).append(msg)

        # Apply queued parameter changes (drain lock-free deque).
        # Deduplicate: when a rotary floods CCs, only the final value per
        # (slot, param) matters — avoids redundant C++ setattr calls.
        pending_params: dict[tuple[int, str], float] = {}
        while self._param_queue:
            try:
                slot_idx, param_name, value = self._param_queue.popleft()
            except IndexError:
                break
            pending_params[(slot_idx, param_name)] = value
        for (slot_idx, param_name), value in pending_params.items():
            slot = self.slots[slot_idx]
            if slot is not None and slot.plugin is not None:
                try:
                    setattr(slot.plugin, param_name, value)
                except Exception:
                    pass

        has_solo = self.any_solo()

        # -- Parallel slot rendering -----------------------------------------
        # Dispatch active slots to the thread pool.  Each worker calls
        # _render_slot() which does process() + insert FX.  pedalboard
        # releases the GIL in C++, so slots render across CPU cores.
        futures = []
        for idx, slot in enumerate(self.slots):
            if slot is None:
                continue
            if slot.muted or (has_solo and not slot.solo):
                continue
            midi_msgs = queues.get(idx, [])
            fut = self._render_pool.submit(
                self._render_slot, idx, slot, midi_msgs, frames)
            futures.append((idx, slot, fut))

        for idx, slot, fut in futures:
            rt = fut.result()  # blocks until this slot is done
            if rt is not None:
                mixed[:rt.shape[0]] += rt * slot.gain

        # Master effects
        if self.master_effects and HAS_PEDALBOARD:
            if not hasattr(self, '_master_board') or self._master_board is None:
                self._master_board = Pedalboard(self.master_effects)
            mt = mixed.T.copy()
            mt = self._master_board(mt, self.sample_rate, reset=False)
            mixed = mt.T

        mixed *= self.master_gain
        np.clip(mixed, -1.0, 1.0, out=mixed)
        outdata[:] = mixed

    # -- start / stop --------------------------------------------------------

    def start(self, output_device=None):
        if not HAS_SOUNDDEVICE:
            raise RuntimeError("sounddevice not installed")
        self._stream = sd.OutputStream(
            samplerate=self.sample_rate,
            blocksize=self.buffer_size,
            channels=self.output_channels,
            dtype="float32",
            callback=self._callback,
            device=output_device,
        )
        self._stream.start()
        logger.info(
            "[Audio] Started sr=%d buf=%d ch=%d",
            self.sample_rate,
            self.buffer_size,
            self.output_channels,
        )

    def stop(self):
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None
            logger.info("[Audio] Stopped")

    def shutdown(self):
        """Stop audio and release the render thread pool."""
        self.stop()
        self._render_pool.shutdown(wait=False)
        logger.info("[Audio] Render pool shut down")

    @property
    def running(self) -> bool:
        return self._stream is not None and self._stream.active
