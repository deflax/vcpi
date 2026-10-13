"""Real-time audio engine (sounddevice output callback)."""

from __future__ import annotations

import collections
import gc
import logging
import os
import threading
from time import perf_counter_ns
from contextlib import contextmanager
from typing import Optional

from core.audio_deps import HAS_SOUNDDEVICE, sd, np
from core.audio_state import plain_value
from core.models import InstrumentSlot, NUM_SLOTS
from sampler import WavSamplerPlugin


logger = logging.getLogger(__name__)


class _Control:
    """One owner-applied operation and its optional synchronous acknowledgement."""

    __slots__ = ("operation", "arguments", "done", "result", "error",
                 "cancelled", "started")

    def __init__(self, operation, arguments, synchronous):
        self.operation = operation
        self.arguments = arguments
        self.done = threading.Event() if synchronous else None
        self.result = None
        self.error = None
        self.cancelled = False
        self.started = False


class _RenderWorker:
    """Persistent worker with a reusable handoff and bounded block job array."""

    def __init__(self, engine, index):
        self.engine = engine
        self.index = index
        self.frames = 0
        self.stopping = False
        self.ready = threading.Event()
        self.done = threading.Event()
        self.thread = threading.Thread(
            target=self._run, name=f"vcpi-render-{index + 1}", daemon=True)
        self.thread.start()
        self.done.wait()  # Pay thread startup before opening the audio stream.

    def _run(self):
        self.done.set()
        while True:
            self.ready.wait()
            self.ready.clear()
            if self.stopping:
                return
            try:
                while True:
                    with self.engine._job_lock:
                        cursor = self.engine._job_cursor
                        if cursor >= self.engine.render_slots:
                            break
                        idx = self.engine._render_indices[cursor]
                        self.engine._job_cursor = cursor + 1
                    self.engine._rendered[idx] = self.engine._render_slot(
                        idx, self.engine._block_slots[idx],
                        self.engine._midi_lists[idx], self.frames)
            finally:
                self.done.set()

    def submit(self, frames):
        self.frames = frames
        self.done.clear()
        self.ready.set()

    def close(self):
        self.stopping = True
        self.ready.set()
        self.thread.join()


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

    _slot_fields = frozenset(
        ("gain", "muted", "solo", "enabled", "source_type", "vcv_patch_path"))

    def __init__(self, sample_rate: int = 44100, buffer_size: int = 512,
                 output_channels: int = 2, health_buffer=None):
        self.sample_rate = sample_rate
        self.buffer_size = buffer_size
        self.output_channels = output_channels

        self.slots: list[Optional[InstrumentSlot]] = [None] * NUM_SLOTS
        self.master_effects: list = []  # pedalboard plugin instances
        self.master_gain: float = 1.0
        self.isolated_slots = False

        self._midi_queue: collections.deque = collections.deque()  # (slot_index, msg)
        self._control_queue: collections.deque = collections.deque()
        self._applied_updates: collections.deque = collections.deque()
        self._control_lock = threading.Lock()
        self._control_capacity = 4096
        self._control_timeout = 2.0
        self.control_error = None
        self.parameter_values: dict[int, dict] = {}
        self.parameter_readers: dict[int, dict] = {}
        self._owner_thread = threading.get_ident()
        self._lock = threading.RLock()  # serialize stream start/stop and stopped edits
        self._stream = None
        self.stream_error = None  # failed close/stop makes the ownership process terminal
        self._mixed_buf = None
        self._gain_buf = None
        self._silence_buf = None
        self._master_input = None
        self._master_board = None
        self._block_slots = [None] * NUM_SLOTS
        self._render_indices = [0] * NUM_SLOTS
        self._rendered = [None] * NUM_SLOTS
        self._job_cursor = 0
        self._job_lock = threading.Lock()
        self._midi_lists = [[] for _ in range(NUM_SLOTS)]
        # Snapshot queue length and cap work; arrivals during a callback wait
        # for the next block. Never discard MIDI (including note-offs).
        self._midi_budget = 256
        self._control_budget = 64
        self._workers = []
        self.render_workers = min(os.cpu_count() or 2, NUM_SLOTS)
        self.render_slots = 0
        self.eligible_slots = 0
        self.underruns = 0
        self.render_errors = 0
        self.backend_label = "not opened"
        self.output_name = None
        self.callback_count = 0
        self.last_callback_ms = 0.0
        self.peak_callback_ms = 0.0
        self.deadline_misses = 0
        self.block_period_ms = buffer_size * 1000.0 / sample_rate
        self.health_buffer = health_buffer
        self._running = False
        self._closed = False
        self._output_device = None

        # Unified MIDI channel -> slot routing (shared by all controllers)
        self.channel_map: dict[int, int] = {}  # MIDI channel (0-15) -> slot index (0-7)
        self._publish_health()

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

    def enqueue_midi(self, slot_index: int, msg, generation=None):
        """Enqueue MIDI without discarding notes; overflow is an explicit error."""
        if not isinstance(slot_index, int) or not 0 <= slot_index < NUM_SLOTS:
            raise ValueError(f"slot index must be 0-{NUM_SLOTS - 1}")
        with self._control_lock:
            if self._closed:
                raise RuntimeError("audio engine is shut down")
            if generation is not None:
                slot = self.slots[slot_index]
                current = slot.generation if slot is not None else 0
                if generation != current:
                    return False
            if len(self._midi_queue) >= self._control_capacity:
                raise BufferError("audio MIDI queue is full")
            self._midi_queue.append((slot_index, msg))
        return True

    # -- Owner-applied controls ----------------------------------------------

    @property
    def pending_control_count(self):
        return len(self._control_queue)

    def _enqueue_control(self, control):
        with self._control_lock:
            if self._closed:
                raise RuntimeError("audio engine is shut down")
            if len(self._control_queue) >= self._control_capacity:
                raise BufferError("audio control queue is full")
            self._control_queue.append(control)

    def _submit_control(self, operation, arguments, synchronous):
        control = _Control(operation, arguments, synchronous)
        if not synchronous:
            self._enqueue_control(control)
            return None
        with self._lock:
            if self._closed:
                raise RuntimeError("audio engine is shut down")
            self._enqueue_control(control)
            if (not self._running
                    and threading.get_ident() == self._owner_thread):
                self.apply_pending_controls()
        if not control.done.wait(self._control_timeout):
            with self._control_lock:
                # Never apply a timed-out queued operation in a later block.
                # An already-started native call cannot be interrupted: the
                # transport treats this timeout as fatal and kills the owner.
                control.cancelled = True
            raise TimeoutError("audio control acknowledgement timed out")
        if control.error is not None:
            raise control.error
        return control.result

    @staticmethod
    def _plain_parameter(value):
        if isinstance(value, np.generic):
            value = value.item()
        return plain_value(value)

    def _control_slot(self, index):
        if not isinstance(index, int) or not 0 <= index < NUM_SLOTS:
            raise ValueError(f"slot index must be 0-{NUM_SLOTS - 1}")
        slot = self.slots[index]
        if slot is None:
            raise ValueError(f"slot {index + 1} is empty")
        return slot

    def _apply_control(self, control):
        operation = control.operation
        arguments = control.arguments
        if operation == "publish_slot":
            index, slot = arguments
            with self._control_lock:
                retired = self.slots[index]
                self.slots[index] = slot
                # Bounded, in-place filtering preserves other slots' MIDI and
                # drops even backlog beyond this callback's MIDI budget.
                for _ in range(len(self._midi_queue)):
                    queued = self._midi_queue.popleft()
                    if queued[0] != index:
                        self._midi_queue.append(queued)
                self._midi_lists[index].clear()
            # Keep retirement with the synchronous acknowledgement; never
            # close a child process or its shared memory on the callback.
            return retired
        if operation == "parameter":
            slot_index, fx_index, name, value = arguments
            if slot_index is None:
                if fx_index is None:
                    raise ValueError("master parameter requires an effect index")
                effects = self.master_effects
            else:
                slot = self._control_slot(slot_index)
                if self.isolated_slots:
                    raise RuntimeError(
                        "isolated slot parameters must be changed by the slot owner")
                effects = slot.effects
            if fx_index is not None:
                if not isinstance(fx_index, int) or not 0 <= fx_index < len(effects):
                    raise ValueError("effect index out of range")
                plugin = effects[fx_index]
            else:
                plugin = slot.plugin
            if plugin is None:
                raise ValueError("slot has no instrument")
            setattr(plugin, name, value)
            actual = self._plain_parameter(getattr(plugin, name))
            values = self.parameter_values.get(id(plugin))
            if values is None:
                values = self.parameter_values[id(plugin)] = {}
            values[name] = actual
            readers = self.parameter_readers.get(id(plugin))
            reader = readers.get(name) if readers is not None else None
            raw = string = None
            try:
                if reader is not None:
                    raw = float(reader.raw_value)
                    string = str(reader.string_value)
            finally:
                # Even an introspection failure must not hide a native change
                # which has already happened.
                self._applied_updates.append(
                    ("parameter", slot_index, fx_index, name, actual, raw, string))
            return actual
        if operation == "slot":
            index, fields = arguments
            slot = self._control_slot(index)
            unknown = fields.keys() - self._slot_fields
            if unknown:
                raise TypeError(f"unsupported slot fields: {', '.join(sorted(unknown))}")
            applied = {}
            for name, value in fields.items():
                if name == "gain":
                    applied[name] = float(value)
                elif name in ("muted", "solo", "enabled"):
                    applied[name] = bool(value)
                else:
                    applied[name] = str(value)
            for name, value in applied.items():
                setattr(slot, name, value)
            self._applied_updates.append(("slot", index, applied))
            return None
        if operation == "master_gain":
            value = float(arguments[0])
            self.master_gain = value
            self._applied_updates.append(("master_gain", value))
            return None
        if operation == "barrier":
            if self.control_error is not None:
                raise self.control_error
            return None
        raise ValueError(f"unknown audio control: {operation}")

    def _drain_controls(self, budget):
        for _ in range(min(len(self._control_queue), budget)):
            with self._control_lock:
                if not self._control_queue:
                    break
                control = self._control_queue.popleft()
                if control.cancelled:
                    continue
                control.started = True
            try:
                control.result = self._apply_control(control)
            except Exception as exc:
                control.error = exc
                if control.done is None and self.control_error is None:
                    self.control_error = exc
            finally:
                if control.done is not None:
                    control.done.set()

    def publish_slot(self, index: int, slot: InstrumentSlot | None):
        """Swap at a block boundary and return the retired slot to its owner.

        Discard prior MIDI for this index. The caller owns retired worker
        shutdown after acknowledgement; a stopped engine applies this directly
        on its owner thread through the same control queue.
        """
        if not isinstance(index, int) or not 0 <= index < NUM_SLOTS:
            raise ValueError(f"slot index must be 0-{NUM_SLOTS - 1}")
        return self._submit_control("publish_slot", (index, slot), True)

    def clear_slot(self, index: int):
        """Clear at a block boundary, returning the slot for owner cleanup."""
        return self.publish_slot(index, None)

    def set_parameter(self, slot_index: int | None, name: str, value,
                      fx_index: int | None = None):
        return self._submit_control(
            "parameter", (slot_index, fx_index, name, value), True)

    def set_slot_state(self, slot_index: int, **fields):
        return self._submit_control("slot", (slot_index, fields), True)

    def set_master_gain(self, value):
        return self._submit_control("master_gain", (value,), True)

    def enqueue_param_change(self, slot_index: int | None, param_name: str, value,
                             fx_index: int | None = None):
        """Queue a change even while stopped; the owner drains before restart."""
        self._submit_control("parameter", (slot_index, fx_index, param_name, value), False)

    def enqueue_slot_change(self, index: int, **fields):
        self._submit_control("slot", (index, fields), False)

    def enqueue_master_gain(self, value):
        self._submit_control("master_gain", (value,), False)

    def flush_controls(self):
        """Acknowledge all controls ahead of this FIFO barrier."""
        return self._submit_control("barrier", (), True)

    def apply_pending_controls(self):
        """Drain on the stopped owner, never concurrently with native rendering."""
        with self._lock:
            if self._running:
                raise RuntimeError("cannot apply pending controls while audio is running")
            if threading.get_ident() != self._owner_thread:
                raise RuntimeError("pending controls must be applied by the audio owner")
            self._drain_controls(len(self._control_queue))
            if self.control_error is not None:
                raise self.control_error

    # -- solo logic ----------------------------------------------------------

    def any_solo(self) -> bool:
        return any(s.solo for s in self.slots if s is not None)

    # -- per-slot rendering --------------------------------------------------

    def _render_slot(self, idx: int, slot: InstrumentSlot,
                     midi_msgs: list, frames: int) -> Optional[np.ndarray]:
        """Render inline for one slot, otherwise on its persistent worker."""
        try:
            if isinstance(slot.plugin, WavSamplerPlugin):
                for msg in midi_msgs:
                    try:
                        slot.plugin.send_midi(msg)
                    except Exception:
                        self.render_errors += 1
                rendered = slot.plugin.process(self._silence_buf, self.sample_rate)
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
            board = slot._effects_board
            if board is not None:
                rendered = board(rendered, self.sample_rate, reset=False)

            if (rendered.ndim != 2 or rendered.shape[0] == 0
                    or rendered.shape[1] > frames
                    or (rendered.shape[0] < self.output_channels
                        and rendered.shape[0] != 1)):
                raise ValueError("plugin returned an incompatible audio block")
            # rendered: (channels, frames) -> transpose to (frames, channels)
            rt = rendered.T
            if rt.shape[1] > self.output_channels:
                rt = rt[:, :self.output_channels]
            return rt

        except Exception:
            self.render_errors += 1
            return None

    def _render_remote_slot(self, slot, midi_msgs, frames):
        """Consume/submit shared blocks without native slot access or waiting."""
        try:
            owner = slot.render_owner
            if owner is None:
                raise RuntimeError("isolated slot has no render owner")
            rendered = owner.render(midi_msgs, frames)
            if rendered is not None and (
                    rendered.ndim != 2
                    or rendered.shape != (frames, self.output_channels)):
                raise ValueError("slot owner returned an incompatible audio block")
            return rendered
        except Exception:
            self.render_errors += 1
            return None

    # -- audio callback ------------------------------------------------------

    def _prepare_buffers(self, frames):
        self._mixed_buf = np.zeros((frames, self.output_channels), dtype=np.float32)
        self._gain_buf = np.empty_like(self._mixed_buf)
        self._silence_buf = np.zeros((self.output_channels, frames), dtype=np.float32)
        self._master_input = np.empty_like(self._silence_buf)

    def _mix_slot(self, rendered, gain):
        if rendered is None:
            return
        count = min(rendered.shape[0], self._mixed_buf.shape[0])
        scratch = self._gain_buf[:count]
        # Mono broadcasts into the output channels without column_stack.
        np.multiply(rendered[:count], gain, out=scratch)
        np.add(self._mixed_buf[:count], scratch, out=self._mixed_buf[:count])

    def _callback(self, outdata, frames: int, time_info, status):
        started = perf_counter_ns()
        try:
            self._render_block(outdata, frames, status)
        except Exception:
            self.render_errors += 1
            outdata.fill(0)
        finally:
            for idx in range(NUM_SLOTS):
                self._block_slots[idx] = None
            elapsed = (perf_counter_ns() - started) / 1_000_000.0
            self.callback_count += 1
            self.last_callback_ms = elapsed
            if elapsed > self.peak_callback_ms:
                self.peak_callback_ms = elapsed
            self.block_period_ms = frames * 1000.0 / self.sample_rate
            if elapsed > self.block_period_ms:
                self.deadline_misses += 1
            self._publish_health()

    def _publish_health(self):
        """Fixed scalar writes only: no IPC, native discovery, logs or locks."""
        health = self.health_buffer
        if health is None:
            return
        health[0] = float(self._running)
        health[1] = float(self.eligible_slots)
        health[2] = float(self.underruns)
        health[3] = float(self.render_errors)
        health[4] = float(self.callback_count)
        health[5] = self.last_callback_ms
        health[6] = self.peak_callback_ms
        health[7] = float(self.deadline_misses)
        health[8] = self.block_period_ms

    def _render_block(self, outdata, frames, status):
        if status and status.output_underflow:
            self.underruns += 1

        # Shared worker rings have a fixed frame capacity. Never silently
        # resize callback scratch and submit an incompatible shared block.
        if self.isolated_slots:
            if (frames != self.buffer_size or self._mixed_buf is None
                    or self._mixed_buf.shape[0] != frames):
                raise RuntimeError("isolated rendering requires the configured block size")
        elif self._mixed_buf is None or self._mixed_buf.shape[0] != frames:
            self._prepare_buffers(frames)
        mixed = self._mixed_buf
        mixed.fill(0)
        for messages in self._midi_lists:
            messages.clear()
        for _ in range(min(len(self._midi_queue), self._midi_budget)):
            slot_idx, msg = self._midi_queue.popleft()
            self._midi_lists[slot_idx].append(msg)
        self._drain_controls(self._control_budget)

        # Hold one block's slot references through rendering and mixing.
        # Muting/soloing affects eligibility, not the number of live threads.
        has_solo = self.any_solo()
        if self.isolated_slots:
            self.render_slots = 0
            self.eligible_slots = 0
            for idx, slot in enumerate(self.slots):
                self._block_slots[idx] = slot
                if slot is None:
                    continue
                self.render_slots += 1
                # Always advance remote tails and drain MIDI, even for a
                # muted or solo-excluded slot. Eligibility only affects mix.
                rendered = self._render_remote_slot(
                    slot, self._midi_lists[idx], frames)
                if not slot.muted and (not has_solo or slot.solo):
                    self.eligible_slots += 1
                    self._mix_slot(rendered, slot.gain)
        else:
            active = 0
            single_index = 0
            for idx, slot in enumerate(self.slots):
                if slot is not None and not slot.muted and (not has_solo or slot.solo):
                    self._block_slots[idx] = slot
                    self._render_indices[active] = idx
                    active += 1
                    single_index = idx
                else:
                    self._block_slots[idx] = None
            self.render_slots = active
            self.eligible_slots = active
            if active == 1:
                slot = self._block_slots[single_index]
                rendered = self._render_slot(
                    single_index, slot, self._midi_lists[single_index], frames)
                self._mix_slot(rendered, slot.gain)
            elif active > 1:
                self._job_cursor = 0
                worker_count = min(active, len(self._workers))
                for idx in range(worker_count):
                    self._workers[idx].submit(frames)
                # Complete every job before reusing MIDI lists or processing a
                # subsequent block. A timeout would allow overlapping plugin calls.
                for idx in range(worker_count):
                    self._workers[idx].done.wait()
                for idx, slot in enumerate(self._block_slots):
                    if slot is not None:
                        self._mix_slot(self._rendered[idx], slot.gain)
                        self._rendered[idx] = None

        board = self._master_board
        if board is not None:
            np.copyto(self._master_input, mixed.T)
            try:
                rendered = board(self._master_input, self.sample_rate, reset=False)
                np.copyto(mixed, rendered.T)
            except Exception:
                self.render_errors += 1
                mixed.fill(0)

        np.multiply(mixed, self.master_gain, out=mixed)
        np.clip(mixed, -1.0, 1.0, out=mixed)
        outdata[:] = mixed

    # -- start / stop --------------------------------------------------------

    def _cache_backend(self, stream):
        """PortAudio discovery belongs to startup, never status or the callback."""
        self.output_name = None
        try:
            device = stream.device
            if isinstance(device, (tuple, list)):
                device = device[-1]
            info = sd.query_devices(device)
            self.output_name = str(info["name"])
            api = sd.query_hostapis(info["hostapi"])
            self.backend_label = f'{api["name"]} ({info["name"]})'
        except Exception:
            self.backend_label = "unknown"

    @contextmanager
    def edit_effects(self):
        """Control-thread edit: quiesce all processing, then resume the device."""
        with self._lock:
            resume = self._running
            device = self._output_device
            if resume:
                self.stop()
            try:
                yield
            finally:
                if resume:
                    self.start(device)

    def start(self, output_device=None):
        if not HAS_SOUNDDEVICE:
            raise RuntimeError("sounddevice not installed")
        with self._lock:
            if self._closed:
                raise RuntimeError("audio engine is shut down")
            # PortAudio must stop using the old CFFI callback before its
            # owning stream can be replaced and garbage-collected.
            self.stop()
            self.apply_pending_controls()
            self._prepare_buffers(self.buffer_size)
            if self.isolated_slots:
                # Switching a stopped native engine to remote ownership must
                # not leave unused native render threads behind.
                for worker in self._workers:
                    worker.close()
                self._workers.clear()
            elif not self._workers:
                self._workers = [
                    _RenderWorker(self, idx) for idx in range(self.render_workers)]
            stream = sd.OutputStream(
                samplerate=self.sample_rate,
                blocksize=self.buffer_size,
                channels=self.output_channels,
                dtype="float32",
                callback=self._callback,
                device=output_device,
                finished_callback=self._stream_finished,
            )
            self._stream = stream
            self._output_device = stream.device
            self._cache_backend(stream)
            self._running = True
            self._publish_health()
            try:
                stream.start()
            except BaseException:
                try:
                    stream.close()
                except BaseException as error:
                    # Keep the CFFI callback owner alive until the process is
                    # terminated; a failed close is not proof of quiescence.
                    self.stream_error = error
                    raise
                self._stream = None
                self._running = False
                self.render_slots = 0
                self.eligible_slots = 0
                self._publish_health()
                raise
        logger.info(
            "[Audio] Started sr=%d buf=%d ch=%d",
            self.sample_rate,
            self.buffer_size,
            self.output_channels,
        )

    def _stream_finished(self):
        self._running = False
        self.render_slots = 0
        self.eligible_slots = 0
        self._publish_health()

    def stop(self):
        with self._lock:
            stream = self._stream
            if stream is not None:
                try:
                    try:
                        if self._running:
                            stream.stop()
                    finally:
                        stream.close()
                except BaseException as error:
                    # Do not release a possibly active callback or edit plugins
                    # after failed native teardown. Transport terminates owner.
                    self.stream_error = error
                    raise
                logger.info("[Audio] Stopped")
            self._running = False
            self._stream = None
            self.render_slots = 0
            self.eligible_slots = 0
            self._publish_health()

    def shutdown(self):
        """Stop native rendering; callers separately shut down slot processes."""
        with self._lock:
            self.stop()
            for worker in self._workers:
                worker.close()
            self._workers.clear()
            self._closed = True
            self._master_board = None
            self.master_effects.clear()
            self.slots[:] = [None] * NUM_SLOTS
            self._midi_queue.clear()
            with self._control_lock:
                while self._control_queue:
                    control = self._control_queue.popleft()
                    control.cancelled = True
                    control.error = RuntimeError("audio engine is shut down")
                    if control.done is not None:
                        control.done.set()
            self._applied_updates.clear()
            self.parameter_values.clear()
            self.parameter_readers.clear()
            self.control_error = None
            self.channel_map.clear()
            # Pedalboard's parameter cache holds parameters which retain
            # their plugin. Collect those cycles on the shutdown thread,
            # while the interpreter and native libraries are still live.
            gc.collect()
        logger.info("[Audio] Render workers shut down")

    @property
    def render_workers(self):
        """Process concurrency in isolated mode; native thread capacity otherwise."""
        return NUM_SLOTS if self.isolated_slots else self._render_workers

    @render_workers.setter
    def render_workers(self, value):
        self._render_workers = value

    @property
    def running(self) -> bool:
        return self._running
