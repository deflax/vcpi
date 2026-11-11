"""BeatStep Pro input, without changing the device's stored controller map.

The explicit vcpi profile below is NOT an Arturia factory-map claim. The 2.0
manual, sections 2.2 and 11.1, lists controller numbers as 'Varies'. Match this
profile in code to the existing device template: no learning or SysEx uploads.
CONTROL must use a channel distinct from all sequencers (default here: 16).
Select CONTROL, hold CHAN and press step 16; controls must follow USER channel.
Step buttons and pads must send Gate messages, not Scenes.
The profile also accepts Gate notes and explicitly selected relative encodings.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import islice
import logging
import math
from threading import RLock

from vcpi.deps import HAS_MIDO, mido
from vcpi.midi import MidiInPort
from vcpi.controllers.bindings import (
    ControlBinding, cached_value, input_availability, parameter_range,
    parameter_value, supported_parameter,
)


logger = logging.getLogger(__name__)
PROFILE_NOTE = "Expected vcpi profile; device template not verified."
PAD_ACTIONS = (
    "play_toggle", "cue", "cue_set", "sync", "nudge_down", "nudge_up",
    "phase_reset",
)


@dataclass(frozen=True)
class BeatStepProProfile:
    """Existing-template expectations; MIDI channels are zero-based.

    Numbers are deliberately explicit application defaults, not factory values.
    relative modes describe wire encodings, not an unverified Arturia mode ID:
    twos_complement: 1/+1, 127/-1; offset: 65/+1, 63/-1;
    sign_magnitude: 1/+1, 65/-1. Absolute is the documented device default.
    """

    control_channel: int = 15
    sequence_channels: tuple[int, int, int] = (0, 1, 9)
    button_numbers: tuple[int, ...] = tuple(range(36, 52))
    button_type: str = "cc"
    pad_numbers: tuple[int, ...] = tuple(range(60, 67))
    pad_type: str = "note"
    rotary_ccs: tuple[int, ...] = tuple(range(20, 36))
    rotary_modes: tuple[str, ...] = ("absolute",) * 8 + ("twos_complement",) + ("absolute",) * 7
    fine_seek_seconds: float = 0.1
    coarse_seek_seconds: float = 5.0

    def __post_init__(self):
        channels = (self.control_channel, *self.sequence_channels)
        if (len(self.sequence_channels) != 3 or len(set(channels)) != 4
                or any(not 0 <= channel <= 15 for channel in channels)):
            raise ValueError("CONTROL, SEQ1, SEQ2 and DRUM need distinct MIDI channels")
        for numbers in (self.button_numbers, self.rotary_ccs):
            if (len(numbers) != 16 or len(set(numbers)) != 16
                    or any(not 0 <= number <= 127 for number in numbers)):
                raise ValueError("each control bank needs 16 distinct MIDI numbers")
        if self.button_type not in ("cc", "note"):
            raise ValueError("button_type must be cc or note")
        if self.button_type == "cc" and set(self.button_numbers) & set(self.rotary_ccs):
            raise ValueError("button and rotary CC numbers must not overlap")
        if (self.pad_type not in ("cc", "note") or len(self.pad_numbers) != len(PAD_ACTIONS)
                or len(set(self.pad_numbers)) != len(self.pad_numbers)
                or any(not 0 <= number <= 127 for number in self.pad_numbers)):
            raise ValueError("supply seven distinct Gate pad messages")
        if self.pad_type == self.button_type and set(self.pad_numbers) & set(self.button_numbers):
            raise ValueError("pad and step messages must not overlap")
        if self.pad_type == "cc" and set(self.pad_numbers) & set(self.rotary_ccs):
            raise ValueError("pad and rotary CC numbers must not overlap")
        modes = {"absolute", "twos_complement", "offset", "sign_magnitude"}
        if len(self.rotary_modes) != 16 or any(mode not in modes for mode in self.rotary_modes):
            raise ValueError("supply 16 supported rotary wire encodings")


DEFAULT_PROFILE = BeatStepProProfile()


def _parameter_items(plugin):
    return ((name, parameter) for name, parameter in plugin.parameter_items("controls")
            if not (name == "volume"
                    and plugin.manufacturer_name.casefold() == "plugdata"
                    and plugin.name.casefold().replace(" ", "") in {"plugdata", "plugdata-fx"}))


def _effect_binding(track, ordinal):
    candidates = ((fx_index, effect, name, parameter)
                  for fx_index, effect in enumerate(track.effects)
                  for name, parameter in _parameter_items(effect))
    return next(islice(candidates, ordinal, ordinal + 1), None)


class BeatStepProController:
    def __init__(self, engine, timeline, track_focus, *, profile: BeatStepProProfile = DEFAULT_PROFILE):
        self._engine = engine
        self._timeline = timeline
        self.profile = profile
        self._port = MidiInPort(receive_clock=True, receive_sysex=True)
        self._lock = RLock()
        self._routes = dict(zip(profile.sequence_channels, (2, 3, 4)))
        self._buttons = {number: index for index, number in enumerate(profile.button_numbers)}
        self._rotaries = {number: index for index, number in enumerate(profile.rotary_ccs)}
        self._pads = {number: index for index, number in enumerate(profile.pad_numbers)}
        self._track_focus = track_focus
        self._focus_revision = -1
        self._banks = {}  # track index -> (generation, bank)
        self._pad_targets = {}  # pad ordinal -> original (track, generation)
        self._holds = {}  # (channel, index, generation) -> pressed pad ordinals
        self._notes = {}  # (channel, note) -> (index, generation)
        self._sustain = {}  # channel -> (index, generation)
        self._pending = {}  # (index, generation, field) -> (observed, requested)
        self._last_rotary = {}  # (index, generation, rotary) -> absolute value
        self._last_transport = None

    @property
    def input_port_name(self):
        return self._port.name

    @property
    def port_name(self):
        return self.input_port_name

    def _source_bank(self, index, track):
        parameters = list(_parameter_items(track.plugin))
        generation, bank = self._banks.get(index, (track.generation, 0))
        if generation != track.generation:
            bank = 0
        last = max(0, (len(parameters) - 1) // 14)
        bank = min(bank, last)
        self._banks[index] = (track.generation, bank)
        return parameters, bank, last

    def _binding(self, index, track, rotary):
        if track.source_type == "deck":
            return _effect_binding(track, rotary - 9) if rotary >= 9 else None
        parameters, bank, _ = self._source_bank(index, track)
        ordinal = rotary - 1 if rotary < 8 else rotary - 2
        ordinal += bank * 14
        if 0 <= ordinal < len(parameters):
            name, parameter = parameters[ordinal]
            return None, track.plugin, name, parameter
        return None

    def _focused_index(self):
        index, revision = self._track_focus.snapshot()
        if revision != self._focus_revision:
            self._last_rotary.clear()
            self._focus_revision = revision
        return index

    def control_bindings(self) -> list[ControlBinding]:
        """Describe focused controls using cached snapshots, never live DSP."""
        with self._lock:
            return self._control_bindings()

    def _control_bindings(self):
        bindings = []
        channel = self.profile.control_channel + 1
        connected = input_availability(self.input_port_name)
        index = self._focused_index()
        track = self._engine.tracks[index]
        base = connected if track is not None else ("unavailable", "empty track")
        for button, number in enumerate(self.profile.button_numbers):
            bindings.append(ControlBinding(
                "beatstep", f"Button {button + 1}",
                f"ch {channel} {self.profile.button_type.upper()} {number}",
                f"Focus track {button + 1}", cached_value(button == index),
                *connected, button + 1, mode="Gate: press",
            ))
        for pad, number in enumerate(self.profile.pad_numbers):
            state = base
            if track is not None and track.source_type != "deck":
                state = ("unsupported", "deck-only performance control")
            bindings.append(ControlBinding(
                "beatstep", f"Pad {pad + 1}",
                f"ch {channel} {self.profile.pad_type.upper()} {number}",
                f"Track {index + 1}: {PAD_ACTIONS[pad]}", "", *state,
                index + 1, mode="Gate: press/release",
            ))
        for rotary, number in enumerate(self.profile.rotary_ccs):
            state, value, bounds = base, "", ""
            target = "unassigned"
            if track is not None:
                deck = track.source_type == "deck"
                if rotary == 0:
                    target, value, bounds = "gain", cached_value(track.gain), "0 .. 1"
                elif rotary == 8:
                    if deck:
                        target = "index"
                        value = cached_value(track.plugin.values.get("index", 1))
                        bounds = f"1 .. {len(track.deck.get('tracks', ()))}"
                    else:
                        _, bank, last = self._source_bank(index, track)
                        target, value, bounds = "parameter bank", str(bank + 1), f"1 .. {last + 1}"
                elif deck and rotary in (1, 2, 3, 4):
                    target = ("pitch", "coarse seek", "fine seek", "phase_offset")[rotary - 1]
                    field = "position" if rotary in (2, 3) else target
                    value = cached_value(track.deck.get(field, 0))
                    bounds = ("-16 .. 16" if rotary == 1 else "-0.5 .. 0.5"
                              if rotary == 4 else f"0 .. {track.deck.get('duration', 0)} s")
                else:
                    binding = self._binding(index, track, rotary)
                    if binding is None:
                        state = ("unbound", "no control assigned")
                    else:
                        fx_index, plugin, name, parameter = binding
                        target = f"FX {fx_index + 1}: {name}" if fx_index is not None else name
                        value, bounds = parameter_value(plugin, name, parameter), parameter_range(parameter)
                        if not supported_parameter(parameter):
                            state = ("unsupported", "unsupported parameter range")
                        elif parameter.kind not in ("bool", "enum"):
                            observed = plugin.values.get(name, parameter.range[0])
                            if not isinstance(observed, (int, float)) or not math.isfinite(observed):
                                state = ("unavailable", "cached parameter value is not finite")
            bindings.append(ControlBinding(
                "beatstep", f"Rotary {rotary + 1}", f"ch {channel} CC {number}",
                f"Track {index + 1}: {target}", value, *state, index + 1,
                bounds, self.profile.rotary_modes[rotary],
            ))
        return bindings

    def open_input(self, port_index: int) -> str:
        self.close()
        return self._port.open_input_port(port_index, self.on_midi)

    def close(self):
        self._port.close()
        self.clear_deck_holds()
        self._last_transport = None
        self._timeline.disconnect_midi()

    def clear_deck_holds(self, track_index=None, channel=None):
        """Release held controls AND sequence notes before source replacement.

        Every release carries the original generation, never the replacement's.
        Host lifecycle hooks call this before removing/replacing a track.
        """
        with self._lock:
            def matches(ch, index):
                return ((track_index is None or track_index == index)
                        and (channel is None or channel == ch))

            for key in list(self._holds):
                ch, index, generation = key
                if matches(ch, index):
                    held = self._holds.pop(key)
                    if held & {4, 5}:
                        self._engine.enqueue_param_change(index, "nudge", 0.0, generation=generation)
            for pad, (index, _) in list(self._pad_targets.items()):
                if matches(self.profile.control_channel, index):
                    self._pad_targets.pop(pad)
            for index in list(self._banks):
                if matches(self.profile.control_channel, index):
                    self._banks.pop(index)
            for key, (index, generation) in list(self._notes.items()):
                ch, note = key
                if matches(ch, index):
                    self._notes.pop(key)
                    self._engine.enqueue_midi(index, mido.Message(
                        "note_off", channel=ch, note=note), generation=generation)
            for ch, (index, generation) in list(self._sustain.items()):
                if matches(ch, index):
                    self._sustain.pop(ch)
                    self._engine.enqueue_midi(index, mido.Message(
                        "control_change", channel=ch, control=64, value=0), generation=generation)
            for mapping in (self._pending, self._last_rotary):
                for key in list(mapping):
                    index = key[0]
                    ch = self.profile.control_channel
                    if matches(ch, index):
                        mapping.pop(key)

    def on_midi(self, event, data=None):
        del data
        raw, _delta = event
        if not raw:
            return
        # rtmidi delta is NOT a monotonic timestamp. Timeline samples its clock.
        if raw[0] == 0xF0:
            # Universal realtime MMC: device ID may be specific or all-call.
            if (len(raw) == 6 and raw[1] == 0x7F and 0 <= raw[2] <= 0x7F
                    and raw[3] == 0x06 and raw[5] == 0xF7):
                command = raw[4]
                if command in (0x01, 0x09):  # Stop / Pause
                    self._transport([0xFC])
                elif command in (0x02, 0x03):  # Play / Deferred Play
                    # MMC Play contains no position. Never fabricate bar zero.
                    if self._last_transport not in (0xFA, 0xFB):
                        self._transport([0xFB])
            return
        if raw[0] in (0xFA, 0xFB, 0xFC):
            self._transport(raw)
            return
        if raw[0] in (0xF8, 0xF2):
            self._timeline.midi_message(raw)
            return
        if raw[0] >= 0xF0 or not HAS_MIDO:
            return
        try:
            message = mido.Message.from_bytes(raw)
        except (ValueError, TypeError) as error:
            logger.debug("BeatStep Pro malformed MIDI %s: %s", raw, error)
            return
        with self._lock:
            if message.channel == self.profile.control_channel:
                self._control(message)
            else:
                self._sequence(message)

    def _transport(self, raw):
        # Real MIDI Start is an explicit restart, even if Start was the last
        # transport message. Only the redundant MMC Play is filtered above.
        with self._lock:
            self._last_transport = raw[0]
            self._timeline.midi_message(raw)

    def _sequence(self, message):
        index = self._routes.get(message.channel)
        if index is None:
            return
        key = (message.channel, getattr(message, "note", None))
        released = (message.type == "note_off"
                    or message.type == "note_on" and message.velocity == 0)
        if released:
            original = self._notes.pop(key, None)
            if original is not None:
                index, generation = original
                self._engine.enqueue_midi(index, mido.Message(
                    "note_off", channel=message.channel, note=message.note,
                    velocity=message.velocity), generation=generation)
            return
        track = self._engine.tracks[index]
        if track is None or track.source_type == "deck":
            return
        generation = track.generation
        if message.type == "note_on":
            self._notes[key] = (index, generation)
        elif message.type == "control_change" and message.control == 64:
            if message.value >= 64:
                self._sustain[message.channel] = (index, generation)
            else:
                original = self._sustain.pop(message.channel, None)
                if original is not None:
                    index, generation = original
        self._engine.enqueue_midi(index, message, generation=generation)

    @staticmethod
    def _gate(message, kind):
        if kind == "cc" and message.type == "control_change":
            return message.control, message.value > 0
        if kind == "note" and message.type in ("note_on", "note_off"):
            return message.note, message.type == "note_on" and message.velocity > 0
        return None, False

    def _control(self, message):
        number, pressed = self._gate(message, self.profile.button_type)
        button = self._buttons.get(number)
        if button is not None:
            if pressed:
                self._track_focus.select(button)
                self._focused_index()
            return
        number, pressed = self._gate(message, self.profile.pad_type)
        pad = self._pads.get(number)
        if pad is not None:
            self._pad(pad, pressed)
        elif message.type == "control_change":
            rotary = self._rotaries.get(message.control)
            if rotary is not None:
                self._rotary(rotary, message.value)

    def _track(self, index):
        track = self._engine.tracks[index]
        if track is None:
            return None
        for mapping in (self._pending, self._last_rotary):
            for key in list(mapping):
                if key[0] == index and key[1] != track.generation:
                    mapping.pop(key)
        return track

    def _current(self, index, track, field, observed):
        key = (index, track.generation, field)
        pending = self._pending.get(key)
        if pending is None:
            return observed
        before, requested = pending
        if observed != before or observed == requested:
            self._pending.pop(key)
            return observed
        return requested

    def _set(self, index, track, field, value, observed):
        self._pending[(index, track.generation, field)] = (observed, value)
        self._engine.enqueue_param_change(index, field, value, generation=track.generation)

    def _pad(self, pad, pressed):
        original = self._pad_targets.get(pad)
        if pressed:
            if original is not None:
                return
            index = self._focused_index()
            track = self._track(index)
            if track is None or track.source_type != "deck":
                return
            generation = track.generation
            self._pad_targets[pad] = (index, generation)
        else:
            if original is None:
                return
            index, generation = self._pad_targets.pop(pad)
        key = (self.profile.control_channel, index, generation)
        held = self._holds.setdefault(key, set())
        if pressed:
            held.add(pad)
        else:
            held.discard(pad)
        action = PAD_ACTIONS[pad]
        if pad in (4, 5):
            value = float((5 in held) - (4 in held))
            self._engine.enqueue_param_change(index, "nudge", value, generation=generation)
        elif pressed:
            if action == "sync":
                observed = bool(track.deck.get("sync", False))
                self._set(index, track, "sync", not self._current(index, track, "sync", observed), observed)
            elif action == "phase_reset":
                observed = float(track.deck.get("phase_offset", 0))
                self._pending[(index, track.generation, "phase_offset")] = (observed, 0.0)
                self._engine.enqueue_deck_command(index, action, generation=generation)
            else:
                self._engine.enqueue_deck_command(index, action, generation=generation)
        if not held:
            self._holds.pop(key, None)

    @staticmethod
    def _delta(mode, value):
        if mode == "offset":
            return value - 64
        if mode == "sign_magnitude":
            return -(value - 64) if value >= 64 else value
        return value - 128 if value >= 64 else value

    def _absolute_delta(self, index, track, rotary, value):
        key = (index, track.generation, rotary)
        previous = self._last_rotary.get(key)
        self._last_rotary[key] = value
        return None if previous is None else value - previous

    def _rotary(self, rotary, value):
        index = self._focused_index()
        track = self._track(index)
        if track is None:
            return
        mode = self.profile.rotary_modes[rotary]
        delta = self._delta(mode, value) if mode != "absolute" else None
        if rotary == 8:
            if delta is None:
                delta = self._absolute_delta(index, track, rotary, value)
            if not delta:
                return
            if track.source_type == "deck":
                count = len(track.deck.get("tracks", ()))
                if count:
                    observed = int(track.plugin.values.get("index", 1))
                    current = self._current(index, track, "index", observed)
                    selected = max(1, min(count, current + delta))
                    if selected != current:
                        self._set(index, track, "index", selected, observed)
            else:
                _, bank, last = self._source_bank(index, track)
                selected = max(0, min(last, bank + delta))
                if selected != bank:
                    self._banks[index] = (track.generation, selected)
                    self._last_rotary.clear()
                    if mode == "absolute":
                        self._last_rotary[(index, track.generation, rotary)] = value
            return
        if rotary == 0:
            observed = track.gain
            current = self._current(index, track, "gain", observed)
            gain = value / 127.0 if delta is None else max(0.0, min(1.0, current + delta / 127.0))
            self._pending[(index, track.generation, "gain")] = (observed, gain)
            self._engine.enqueue_track_change(index, gain=gain, generation=track.generation)
        elif track.source_type != "deck" or rotary >= 9:
            self._parameter(index, track, rotary, value, delta)
        elif rotary in (1, 4):
            field = "pitch" if rotary == 1 else "phase_offset"
            low, high = (-16.0, 16.0) if rotary == 1 else (-0.5, 0.5)
            observed = float(track.deck.get(field, 0))
            current = self._current(index, track, field, observed)
            target = (low + (high - low) * value / 127.0 if delta is None
                      else max(low, min(high, current + delta * (high - low) / 127.0)))
            self._set(index, track, field, target, observed)
        elif rotary in (2, 3):
            observed = float(track.deck.get("position", 0))
            current = self._current(index, track, "seek", observed)
            duration = float(track.deck.get("duration", 0))
            if rotary == 2 and delta is None:
                target = duration * value / 127.0
            else:
                if delta is None:
                    delta = self._absolute_delta(index, track, rotary, value)
                    if delta is None:
                        return
                step = self.profile.fine_seek_seconds if rotary == 3 else self.profile.coarse_seek_seconds
                target = max(0.0, min(duration, current + delta * step))
            self._set(index, track, "seek", target, observed)

    def _parameter(self, index, track, rotary, value, delta):
        binding = self._binding(index, track, rotary)
        if binding is None:
            return
        fx_index, effect, name, parameter = binding
        if not supported_parameter(parameter):
            return
        if parameter.kind == "bool":
            normalized = float(bool(effect.values.get(name, False)))
        elif parameter.kind == "enum":
            observed = effect.values.get(name, parameter.valid_values[0])
            if observed not in parameter.valid_values:
                return
            normalized = (parameter.valid_values.index(observed)
                          / max(1, len(parameter.valid_values) - 1))
        else:
            # Infinite display bounds do not define a usable linear range.
            low, high = parameter.range
            observed = effect.values.get(name, low)
            if not isinstance(observed, (int, float)) or not math.isfinite(observed):
                return
            normalized = (observed - low) / (high - low) if high != low else 0.0
        field = ("effect", effect.token, name)
        current = self._current(index, track, field, normalized)
        fraction = value / 127.0 if delta is None else max(0.0, min(1.0, current + delta / 127.0))
        if parameter.kind == "bool":
            mapped = fraction >= 0.5
        elif parameter.kind == "enum":
            mapped = parameter.valid_values[round(fraction * (len(parameter.valid_values) - 1))]
        else:
            mapped = low + fraction * (high - low)
            if parameter.kind == "int":
                mapped = round(mapped)
        self._pending[(index, track.generation, field)] = (normalized, fraction)
        self._engine.enqueue_param_change(index, name, mapped, fx_index=fx_index,
                                         fx_token=effect.token if fx_index is not None else None,
                                         generation=track.generation)
