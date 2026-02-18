"""Real-time audio engine (sounddevice output callback)."""

from __future__ import annotations

import logging
import threading
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
      2. Render each instrument
      3. Apply per-slot insert effects
      4. Mix according to gain / mute / solo
      5. Apply master effects chain
      6. Write to output buffer
    """

    def __init__(self, sample_rate: int = 44100, buffer_size: int = 512,
                 output_channels: int = 2):
        self.sample_rate = sample_rate
        self.buffer_size = buffer_size
        self.output_channels = output_channels

        self.slots: list[Optional[InstrumentSlot]] = [None] * NUM_SLOTS
        self.master_effects: list = []  # pedalboard plugin instances
        self.master_gain: float = 1.0

        self._midi_queues: dict[int, list] = {}  # slot_index -> [mido.Message]
        self._lock = threading.Lock()
        self._stream = None

    # -- MIDI queueing -------------------------------------------------------

    def enqueue_midi(self, slot_index: int, msg):
        """Thread-safe enqueue of a mido.Message for a given slot."""
        with self._lock:
            self._midi_queues.setdefault(slot_index, []).append(msg)

    # -- solo logic ----------------------------------------------------------

    def any_solo(self) -> bool:
        return any(s.solo for s in self.slots if s is not None)

    # -- audio callback ------------------------------------------------------

    def _callback(self, outdata, frames: int, time_info, status):
        if status:
            logger.warning("[Audio] %s", status)

        mixed = np.zeros((frames, self.output_channels), dtype=np.float32)

        with self._lock:
            queues = {k: list(v) for k, v in self._midi_queues.items()}
            self._midi_queues.clear()

        has_solo = self.any_solo()

        for idx, slot in enumerate(self.slots):
            if slot is None:
                continue

            # Determine audibility
            if slot.muted:
                audible = False
            elif has_solo:
                audible = slot.solo
            else:
                audible = True

            # Deliver MIDI + render audio
            midi_msgs = queues.get(idx, [])

            if isinstance(slot.plugin, WavSamplerPlugin):
                # WavSamplerPlugin: two-step send_midi + process
                for msg in midi_msgs:
                    try:
                        slot.plugin.send_midi(msg)
                    except Exception:
                        logger.debug("[Audio] send_midi error slot %d", idx,
                                     exc_info=True)
                silence = np.zeros((self.output_channels, frames),
                                   dtype=np.float32)
                try:
                    rendered = slot.plugin.process(silence, self.sample_rate)
                except Exception:
                    continue
            else:
                # pedalboard ExternalPlugin: pass MIDI list into process()
                duration = frames / self.sample_rate
                # Stamp all messages at time=0 (start of this block)
                for msg in midi_msgs:
                    msg.time = 0
                try:
                    rendered = slot.plugin.process(
                        midi_msgs,
                        duration=duration,
                        sample_rate=self.sample_rate,
                        num_channels=self.output_channels,
                        buffer_size=frames,
                        reset=False,
                    )
                except Exception:
                    logger.debug("[Audio] VST3 process error slot %d", idx,
                                 exc_info=True)
                    continue

            if not audible:
                continue

            # Per-slot insert effects
            if slot.effects and HAS_PEDALBOARD:
                board = Pedalboard(slot.effects)
                rendered = board(rendered, self.sample_rate)

            # rendered: (channels, frames) -> transpose for mixing
            rt = rendered.T  # (frames, channels)
            if rt.shape[1] == 1 and self.output_channels == 2:
                rt = np.column_stack([rt, rt])
            elif rt.shape[1] > self.output_channels:
                rt = rt[:, :self.output_channels]

            mixed[:rt.shape[0]] += rt * slot.gain

        # Master effects
        if self.master_effects and HAS_PEDALBOARD:
            board = Pedalboard(self.master_effects)
            mt = mixed.T.copy()
            mt = board(mt, self.sample_rate)
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

    @property
    def running(self) -> bool:
        return self._stream is not None and self._stream.active
