"""Control-only facade for native output, mixing and isolated slot DSP."""

from __future__ import annotations

import collections
import math
import threading
from contextlib import contextmanager

from core.audio_deps import require_native
from core.audio_state import plain_value
from core.models import InstrumentSlot, NUM_SLOTS


class _Control:
    __slots__ = ("operation", "arguments", "done", "result", "error", "cancelled")

    def __init__(self, operation, arguments, synchronous):
        self.operation = operation
        self.arguments = arguments
        self.done = threading.Event() if synchronous else None
        self.result = self.error = None
        self.cancelled = False


class AudioEngine:
    """Python owns commands and snapshots; native code owns every audio thread."""

    _slot_fields = frozenset(
        ("gain", "muted", "solo", "enabled", "source_type", "vcv_patch_path"))

    def __init__(self, sample_rate=44100, buffer_size=512, output_channels=2,
                 health_buffer=None, *, slot_mode=False):
        if output_channels != 2:
            raise ValueError("native audio output requires two channels")
        backend = require_native()
        self.sample_rate = sample_rate
        self.buffer_size = buffer_size
        self.output_channels = output_channels
        self._native = None if slot_mode else backend.Engine(sample_rate, buffer_size)
        self.runner = None
        self.slots = [None] * NUM_SLOTS
        self.master_effects = []
        self.master_gain = 1.0
        self.isolated_slots = not slot_mode
        self.channel_map = {}
        self.parameter_values = {}
        self.parameter_readers = {}
        self._midi_queue = collections.deque()
        self._control_queue = collections.deque()
        self._applied_updates = collections.deque()
        self._control_lock = threading.Lock()
        self._lock = threading.RLock()
        self._owner_thread = threading.get_ident()
        self._control_capacity = 4096
        self._control_timeout = 2.0
        self.control_error = None
        self.stream_error = None
        self.health_buffer = health_buffer
        self._running = False
        self._closed = False
        self._output_device = None
        self.render_workers = NUM_SLOTS
        self.render_slots = self.eligible_slots = 0
        self.underruns = self.render_errors = self.callback_count = 0
        self.last_callback_ms = self.peak_callback_ms = 0.0
        self.deadline_misses = 0
        self.block_period_ms = buffer_size * 1000.0 / sample_rate
        self.backend_label = "not opened"
        self.output_name = None
        self.output_realtime = {}
        self.refresh_health()

    def route(self, midi_channel, slot_index):
        if not 0 <= midi_channel < 16:
            raise ValueError("MIDI channel must be 1-16")
        if not 0 <= slot_index < NUM_SLOTS:
            raise ValueError(f"slot must be 1-{NUM_SLOTS}")
        previous = self.channel_map.get(midi_channel)
        if previous is not None and self.slots[previous] is not None:
            self.slots[previous].midi_channels.discard(midi_channel)
        self.channel_map[midi_channel] = slot_index
        if self.slots[slot_index] is not None:
            self.slots[slot_index].midi_channels.add(midi_channel)

    def unroute(self, midi_channel):
        index = self.channel_map.pop(midi_channel, None)
        if index is not None and self.slots[index] is not None:
            self.slots[index].midi_channels.discard(midi_channel)

    def _control_slot(self, index):
        if not isinstance(index, int) or not 0 <= index < NUM_SLOTS:
            raise ValueError(f"slot index must be 0-{NUM_SLOTS - 1}")
        slot = self.slots[index]
        if slot is None:
            raise ValueError(f"slot {index + 1} is empty")
        return slot

    def _send_midi(self, index, message):
        slot = self.slots[index]
        if slot is None:
            return
        worker = slot.render_owner
        if worker is None:
            raise RuntimeError("slot has no isolated render owner")
        if getattr(worker, "failure", None) is not None:
            return
        try:
            worker.transport.send_midi(bytes(message.bytes()))
        except (BufferError, ValueError) as error:
            # Overflow/oversized MIDI invalidates this instrument's note state,
            # not the device or other slots. Retain its visible failure snapshot.
            worker.failure = f"slot MIDI admission failed: {error}"
            if self._native is not None:
                self._native.attach(index, None)

    def enqueue_midi(self, slot_index, msg, generation=None):
        if not isinstance(slot_index, int) or not 0 <= slot_index < NUM_SLOTS:
            raise ValueError(f"slot index must be 0-{NUM_SLOTS - 1}")
        with self._control_lock:
            if self._closed:
                raise RuntimeError("audio engine is shut down")
            slot = self.slots[slot_index]
            if generation is not None and generation != (slot.generation if slot else 0):
                return False
            if self._running:
                self._send_midi(slot_index, msg)
            else:
                if len(self._midi_queue) >= self._control_capacity:
                    raise BufferError("audio MIDI queue is full")
                self._midi_queue.append((slot_index, msg))
        return True

    @property
    def pending_control_count(self):
        return len(self._control_queue)

    def _submit_control(self, operation, arguments, synchronous):
        control = _Control(operation, arguments, synchronous)
        with self._control_lock:
            if self._closed:
                raise RuntimeError("audio engine is shut down")
            if len(self._control_queue) >= self._control_capacity:
                raise BufferError("audio control queue is full")
            self._control_queue.append(control)
        if not synchronous:
            return
        if threading.get_ident() == self._owner_thread:
            self.apply_pending_controls()
        if not control.done.wait(self._control_timeout):
            with self._control_lock:
                control.cancelled = True
            raise TimeoutError("audio control acknowledgement timed out")
        if control.error is not None:
            raise control.error
        return control.result

    def _apply_control(self, control):
        operation, arguments = control.operation, control.arguments
        if operation == "publish_slot":
            index, slot = arguments
            if self._native is not None:
                transport = slot.render_owner.transport if slot is not None else None
                self._native.attach(index, transport, slot.gain if slot else 1.0,
                                    slot.muted if slot else False, slot.solo if slot else False)
            with self._control_lock:
                retired = self.slots[index]
                self.slots[index] = slot
                self._midi_queue = collections.deque(
                    item for item in self._midi_queue if item[0] != index)
            return retired
        if operation == "parameter":
            index, fx_index, name, value = arguments
            if index is None:
                if fx_index is None:
                    raise ValueError("master parameter requires an effect index")
                effects = self.master_effects
            else:
                slot = self._control_slot(index)
                if self.isolated_slots:
                    raise RuntimeError("isolated slot parameters must be changed by the slot owner")
                effects = slot.effects
            if fx_index is not None:
                if not isinstance(fx_index, int) or not 0 <= fx_index < len(effects):
                    raise ValueError("effect index out of range")
                plugin = effects[fx_index]
            else:
                plugin = slot.plugin
            setattr(plugin, name, value)
            actual = plain_value(getattr(plugin, name))
            self.parameter_values.setdefault(id(plugin), {})[name] = actual
            raw = display = None
            try:
                descriptor = (plugin.parameter_descriptor(name)
                              if hasattr(plugin, "parameter_descriptor")
                              else plugin.parameters[name])
                raw, display = descriptor.raw_value, descriptor.string_value
            finally:
                self._applied_updates.append(
                    ("parameter", index, fx_index, name, actual, raw, display))
            return actual
        if operation == "barrier":
            if self.control_error is not None:
                raise self.control_error
            if self._native is not None:
                self._native.barrier()
            if self.runner is not None:
                self.runner.barrier()
            return
        raise ValueError(f"unknown audio control: {operation}")

    def _apply_mixer_controls(self, controls):
        """Publish adjacent mixer edits with one callback acknowledgment."""
        states = [(slot.gain, slot.muted, slot.solo) if slot is not None
                  else (1.0, False, False) for slot in self.slots]
        master = self.master_gain
        updates = {}
        master_changed = False

        def gain(value):
            value = float(value)
            if not math.isfinite(value) or not 0 <= value <= 3.4028234663852886e38:
                raise ValueError("Gain must be a finite nonnegative float")
            return value

        for control in controls:
            try:
                if control.operation == "slot":
                    index, fields = control.arguments
                    self._control_slot(index)
                    unknown = fields.keys() - self._slot_fields
                    if unknown:
                        raise TypeError(f"unsupported slot fields: {', '.join(sorted(unknown))}")
                    applied = {name: (gain(value) if name == "gain" else bool(value)
                               if name in ("muted", "solo", "enabled") else str(value))
                               for name, value in fields.items()}
                    old_gain, muted, solo = states[index]
                    states[index] = (applied.get("gain", old_gain),
                                     applied.get("muted", muted), applied.get("solo", solo))
                    updates.setdefault(index, {}).update(applied)
                else:
                    value = gain(control.arguments[0])
                    master = value
                    master_changed = True
            except Exception as error:
                control.error = error
        if not updates and not master_changed:
            return
        if self._native is not None:
            self._native.set_mixer(states, master)
        # Cached state and completion notifications remain applied-only: publish
        # them after the entire snapshot has reached the native callback.
        for index, fields in updates.items():
            slot = self.slots[index]
            for name, value in fields.items():
                setattr(slot, name, value)
            self._applied_updates.append(("slot", index, fields))
        if master_changed:
            self.master_gain = master
            self._applied_updates.append(("master_gain", master))

    def apply_pending_controls(self):
        """Apply FIFO controls on the control owner, including during playback."""
        if threading.get_ident() != self._owner_thread:
            raise RuntimeError("pending controls must be applied by the audio owner")
        with self._lock:
            remaining = len(self._control_queue)
            while remaining:
                with self._control_lock:
                    control = self._control_queue.popleft()
                    remaining -= 1
                    controls = [control]
                    mixer = control.operation in ("slot", "master_gain")
                    if mixer:
                        while remaining and self._control_queue[0].operation in ("slot", "master_gain"):
                            controls.append(self._control_queue.popleft())
                            remaining -= 1
                    controls = [item for item in controls if not item.cancelled]
                try:
                    if mixer:
                        self._apply_mixer_controls(controls)
                    elif controls:
                        control.result = self._apply_control(control)
                except Exception as error:
                    for item in controls:
                        if item.error is None:
                            item.error = error
                finally:
                    for item in controls:
                        if item.error is not None and item.done is None and self.control_error is None:
                            self.control_error = item.error
                        if item.done is not None:
                            item.done.set()
            if self.control_error is not None:
                raise self.control_error

    def publish_slot(self, index, slot):
        if not isinstance(index, int) or not 0 <= index < NUM_SLOTS:
            raise ValueError(f"slot index must be 0-{NUM_SLOTS - 1}")
        return self._submit_control("publish_slot", (index, slot), True)

    def clear_slot(self, index):
        return self.publish_slot(index, None)

    def set_parameter(self, slot_index, name, value, fx_index=None):
        return self._submit_control("parameter", (slot_index, fx_index, name, value), True)

    def load_plugin_state(self, slot_index, state, fx_index=None):
        """Restore an opaque chunk on its native control owner."""
        from core.state_transport import validate_native_state

        validate_native_state(state)
        if threading.get_ident() != self._owner_thread:
            raise RuntimeError("plugin state must be loaded by the audio owner")
        if slot_index is None:
            if self.running:
                raise RuntimeError("master plugin state requires stopped audio")
            effects = self.master_effects
            if fx_index is None:
                raise ValueError("master plugin state requires an effect index")
        else:
            slot = self._control_slot(slot_index)
            if self.isolated_slots:
                raise RuntimeError("isolated plugin state must be loaded by the slot owner")
            effects = slot.effects
        if fx_index is not None:
            if not isinstance(fx_index, int) or not 0 <= fx_index < len(effects):
                raise ValueError("effect index out of range")
            plugin = effects[fx_index]
        else:
            plugin = slot.plugin
        if self.runner is not None:
            self.runner.barrier()
        plugin.load_saved_state(state)

    def set_slot_state(self, slot_index, **fields):
        return self._submit_control("slot", (slot_index, fields), True)

    def set_master_gain(self, value):
        return self._submit_control("master_gain", (value,), True)

    def enqueue_param_change(self, slot_index, param_name, value, fx_index=None):
        self._submit_control("parameter", (slot_index, fx_index, param_name, value), False)

    def enqueue_slot_change(self, index, **fields):
        self._submit_control("slot", (index, fields), False)

    def enqueue_master_gain(self, value):
        self._submit_control("master_gain", (value,), False)

    def flush_controls(self):
        return self._submit_control("barrier", (), True)

    def any_solo(self):
        return any(slot.solo for slot in self.slots if slot is not None)

    def set_effects(self, effects, slot_index=None):
        plugins = [effect.native for effect in effects]
        if slot_index is None:
            self._native.set_master_effects(plugins)
            self.master_effects = list(effects)
        else:
            if self.runner is None:
                raise RuntimeError("slot native runner is not initialized")
            self.runner.set_effects(plugins)
            self._control_slot(slot_index).effects = list(effects)

    @contextmanager
    def edit_effects(self):
        with self._lock:
            resume, device = self._running, self._output_device
            if resume:
                self.stop()
            if self.runner is not None:
                self.runner.barrier()
            try:
                yield
            finally:
                if resume:
                    self.start(device)

    def start(self, output_device=None):
        with self._lock:
            if self._closed:
                raise RuntimeError("audio engine is shut down")
            if self._native is None:
                raise RuntimeError("slot owner cannot open an output device")
            self.stop()
            self.apply_pending_controls()
            while self._midi_queue:
                index, message = self._midi_queue[0]
                self._send_midi(index, message)
                self._midi_queue.popleft()
            try:
                self._native.start(output_device)
            except require_native().OwnershipError as error:
                # Failed device teardown cannot establish callback quiescence.
                # Supervision terminates this owner instead of reusing it.
                self.stream_error = error
                raise
            self._output_device = output_device
            self.refresh_health()

    def stop(self):
        with self._lock:
            try:
                if self._native is not None:
                    self._native.stop()
            except BaseException as error:
                self.stream_error = error
                raise
            self.refresh_health()

    def refresh_health(self):
        """Copy native counters on the control tick, never from an audio callback."""
        if self._native is not None:
            stats = self._native.stats()
            self._running = bool(stats["running"])
            for name in ("eligible_slots", "underruns", "render_errors", "callback_count",
                         "last_callback_ms", "peak_callback_ms", "deadline_misses",
                         "block_period_ms", "backend_label", "output_name"):
                if name in stats:
                    setattr(self, name, stats[name])
            self.output_realtime = stats["realtime"]
        self.render_slots = sum(slot is not None for slot in self.slots)
        self._publish_health()

    def _publish_health(self):
        if self.health_buffer is not None:
            for index, value in enumerate((self._running, self.eligible_slots, self.underruns,
                    self.render_errors, self.callback_count, self.last_callback_ms,
                    self.peak_callback_ms, self.deadline_misses, self.block_period_ms)):
                self.health_buffer[index] = float(value)

    def stats(self):
        self.refresh_health()
        return self._native.stats() if self._native is not None else {"running": False}

    def render_block(self):
        self.apply_pending_controls()
        while self._midi_queue:
            index, message = self._midi_queue[0]
            self._send_midi(index, message)
            self._midi_queue.popleft()
        result = self._native.render_block()
        self.refresh_health()
        return result

    def shutdown(self):
        with self._lock:
            if self._closed:
                return
            if self.runner is not None:
                self.runner.shutdown()
                self.runner = None
            if self._native is not None:
                self._native.shutdown()
            self._closed = True
            self._running = False
            self.master_effects.clear()
            self.slots[:] = [None] * NUM_SLOTS
            self._midi_queue.clear()
            with self._control_lock:
                while self._control_queue:
                    control = self._control_queue.popleft()
                    control.error = RuntimeError("audio engine is shut down")
                    if control.done is not None:
                        control.done.set()
            self._applied_updates.clear()
            self.parameter_values.clear()
            self.parameter_readers.clear()
            self.channel_map.clear()
            self._publish_health()

    @property
    def running(self):
        return self._running
