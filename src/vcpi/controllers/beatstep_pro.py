"""BeatStep Pro input, without changing the device's stored controller map.

The explicit vcpi profile below is NOT an Arturia factory-map claim. The 2.0
manual, sections 2.2 and 11.1, lists controller numbers as 'Varies'. Match this
profile in code to the existing device template: no learning or SysEx uploads.
CONTROL must use a channel distinct from all sequencers (default here: 16).
Select CONTROL, hold CHAN and press step 16; controls must follow USER channel.
Step buttons must send Gate CC (not Scenes), rotaries absolute CC, range 0..127.
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
BUTTON_ACTIONS = (
    "play_toggle", "cue", "cue_set", "sync", "previous", "next",
    "nudge_down", "nudge_up",
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
    rotary_ccs: tuple[int, ...] = tuple(range(20, 36))
    rotary_modes: tuple[str, ...] = ("absolute",) * 16
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
        modes = {"absolute", "twos_complement", "offset", "sign_magnitude"}
        if len(self.rotary_modes) != 16 or any(mode not in modes for mode in self.rotary_modes):
            raise ValueError("supply 16 supported rotary wire encodings")


DEFAULT_PROFILE = BeatStepProProfile()


def _deck_target(engine, index):
    track = engine.tracks[index]
    return track if track is not None and track.source_type == "deck" else None


def _effect_binding(track, ordinal):
    """Resolve one exposed insert parameter, retaining unsupported ordinals."""
    candidates = ((fx_index, effect, name, parameter)
                  for fx_index, effect in enumerate(track.effects)
                  for name, parameter in effect.parameter_items("controls")
                  # Preserve this profile's preference for patch controls over volume.
                  if not (name == "volume"
                          and effect.manufacturer_name.casefold() == "plugdata"
                          and effect.name.casefold().replace(" ", "")
                          in {"plugdata", "plugdata-fx"}))
    return next(islice(candidates, ordinal, ordinal + 1), None)


class BeatStepProController:
    def __init__(self, engine, timeline, *, profile: BeatStepProProfile = DEFAULT_PROFILE):
        self._engine = engine
        self._timeline = timeline
        self.profile = profile
        self._port = MidiInPort(receive_clock=True, receive_sysex=True)
        self._lock = RLock()
        self._routes = dict(zip(profile.sequence_channels, (2, 3, 4)))
        self._buttons = {number: index for index, number in enumerate(profile.button_numbers)}
        self._rotaries = {number: index for index, number in enumerate(profile.rotary_ccs)}
        self._holds = {}  # (channel, index, generation) -> pressed button ordinals
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

    def control_bindings(self) -> list[ControlBinding]:
        """Describe expected profile bindings using only cached snapshots."""
        bindings = []
        channel = self.profile.control_channel + 1

        def state(index):
            if self._engine.tracks[index] is None:
                return "unavailable", "empty track"
            if _deck_target(self._engine, index) is None:
                return "unsupported", "deck-only binding; target is not a deck"
            return input_availability(self.input_port_name)

        for button, number in enumerate(self.profile.button_numbers):
            index, action = divmod(button, 8)
            availability, reason = state(index)
            track = _deck_target(self._engine, index)
            field = ("play", "cue", "cue", "sync", "track_select", "track_select",
                     "nudge", "nudge")[action]
            value = "(not cached)"
            if track is not None:
                values = track.plugin.values if field == "track_select" else track.deck
                if field in values:
                    value = cached_value(values[field])
            bindings.append(ControlBinding(
                "beatstep", f"Button {button + 1}",
                f"ch {channel} {self.profile.button_type.upper()} {number}",
                f"Track {index + 1} deck: {BUTTON_ACTIONS[action]}",
                value, availability, reason, index + 1, mode="Gate: press/release",
            ))
        for rotary, number in enumerate(self.profile.rotary_ccs):
            index = rotary // 4 if rotary < 8 else (rotary - 8) // 4
            availability, reason = state(index)
            track = _deck_target(self._engine, index)
            value, bounds = "(not cached)", ""
            if rotary < 8:
                control = rotary % 4
                target = ("gain", "pitch", "coarse seek", "fine seek")[control]
                bounds = ("0 .. 1", "-16 .. 16", "", "")[control]
                if track is not None:
                    if control == 0:
                        value = cached_value(track.gain)
                    else:
                        field = "pitch" if control == 1 else "position"
                        if field in track.deck:
                            value = cached_value(track.deck[field])
                        if control >= 2 and "duration" in track.deck:
                            bounds = f"0 .. {cached_value(track.deck['duration'])} s"
            else:
                ordinal = (rotary - 8) % 4
                binding = _effect_binding(track, ordinal) if track is not None else None
                target = f"effect parameter {ordinal + 1} (unbound)"
                if binding is None:
                    if track is not None:
                        availability, reason = "unbound", "no exposed effect parameter"
                else:
                    fx_index, effect, name, parameter = binding
                    target = f"FX {fx_index + 1}: {name}"
                    value = parameter_value(effect, name, parameter)
                    bounds = parameter_range(parameter)
                    if not supported_parameter(parameter):
                        availability, reason = "unsupported", "unsupported effect parameter range"
                    elif parameter.kind not in ("bool", "enum"):
                        observed = effect.values.get(name, parameter.range[0])
                        if not isinstance(observed, (int, float)) or not math.isfinite(observed):
                            availability, reason = "unavailable", "cached effect value is not finite"
            bindings.append(ControlBinding(
                "beatstep", f"Rotary {rotary + 1}", f"ch {channel} CC {number}",
                f"Track {index + 1} deck: {target}", value, availability, reason,
                index + 1, bounds, self.profile.rotary_modes[rotary],
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
                    if any(button % 8 in (6, 7) for button in held):
                        self._engine.enqueue_param_change(index, "nudge", 0.0, generation=generation)
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

    def _control(self, message):
        button = None
        if self.profile.button_type == "cc" and message.type == "control_change":
            button = self._buttons.get(message.control)
            pressed = message.value > 0
        elif self.profile.button_type == "note" and message.type in ("note_on", "note_off"):
            button = self._buttons.get(message.note)
            pressed = message.type == "note_on" and message.velocity > 0
        if button is not None:
            self._button(button, pressed)
        elif message.type == "control_change":
            rotary = self._rotaries.get(message.control)
            if rotary is not None:
                self._rotary(rotary, message.value)

    def _deck(self, index):
        track = _deck_target(self._engine, index)
        if track is None:
            return None
        # Source replacement without an explicit lifecycle hook must not retain
        # stale optimism or suppress the new source's first physical press.
        for mapping in (self._pending, self._last_rotary):
            for key in list(mapping):
                if key[0] == index and key[1] != track.generation:
                    mapping.pop(key)
        for key in list(self._holds):
            if key[1] == index and key[2] != track.generation:
                self._holds.pop(key)
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

    def _button(self, button, pressed):
        index, action_index = divmod(button, 8)
        track = self._deck(index)
        if track is None:
            return
        key = (self.profile.control_channel, index, track.generation)
        held = self._holds.setdefault(key, set())
        was_pressed = button in held
        if pressed:
            held.add(button)
        else:
            held.discard(button)
        if pressed == was_pressed:
            return
        action = BUTTON_ACTIONS[action_index]
        if action_index in (6, 7):
            value = float((index * 8 + 7 in held) - (index * 8 + 6 in held))
            self._engine.enqueue_param_change(index, "nudge", value, generation=track.generation)
        elif pressed:
            if action == "sync":
                observed = bool(track.deck.get("sync", False))
                self._set(index, track, "sync", not self._current(index, track, "sync", observed), observed)
            elif action in ("previous", "next"):
                count = len(track.deck.get("tracks", ()))
                if count:
                    observed = int(track.plugin.values.get("track_select", 1))
                    current = self._current(index, track, "track_select", observed)
                    selected = max(1, min(count, current + (1 if action == "next" else -1)))
                    self._set(index, track, "track_select", selected, observed)
            else:
                self._engine.enqueue_deck_command(index, action, generation=track.generation)
        if not held:
            self._holds.pop(key, None)

    @staticmethod
    def _delta(mode, value):
        if mode == "offset":
            return value - 64
        if mode == "sign_magnitude":
            return -(value - 64) if value >= 64 else value
        return value - 128 if value >= 64 else value

    def _rotary(self, rotary, value):
        index = rotary // 4 if rotary < 8 else (rotary - 8) // 4
        track = self._deck(index)
        if track is None:
            return
        mode = self.profile.rotary_modes[rotary]
        delta = self._delta(mode, value) if mode != "absolute" else None
        if rotary >= 8:
            self._effect(index, track, (rotary - 8) % 4, value, delta)
            return
        control = rotary % 4
        if control == 0:
            observed = track.gain
            current = self._current(index, track, "gain", observed)
            gain = value / 127.0 if delta is None else max(0.0, min(1.0, current + delta / 127.0))
            self._pending[(index, track.generation, "gain")] = (observed, gain)
            self._engine.enqueue_track_change(index, gain=gain, generation=track.generation)
        elif control == 1:
            observed = float(track.deck.get("pitch", 0))
            current = self._current(index, track, "pitch", observed)
            pitch = -16 + 32 * value / 127.0 if delta is None else max(-16, min(16, current + delta * 32 / 127.0))
            self._set(index, track, "pitch", pitch, observed)
        else:
            observed = float(track.deck.get("position", 0))
            current = self._current(index, track, "seek", observed)
            duration = float(track.deck.get("duration", 0))
            if control == 2 and delta is None:
                target = duration * value / 127.0
            else:
                if delta is None:
                    key = (index, track.generation, rotary)
                    previous = self._last_rotary.get(key)
                    self._last_rotary[key] = value
                    if previous is None:
                        return
                    delta = value - previous
                step = self.profile.fine_seek_seconds if control == 3 else self.profile.coarse_seek_seconds
                target = max(0.0, min(duration, current + delta * step))
            self._set(index, track, "seek", target, observed)

    def _effect(self, index, track, ordinal, value, delta):
        binding = _effect_binding(track, ordinal)
        if binding is None:
            return
        fx_index, effect, name, parameter = binding
        if not supported_parameter(parameter):
            return
        if parameter.kind == "bool":
            normalized = float(bool(effect.values.get(name, False)))
        elif parameter.kind == "enum":
            observed = effect.values.get(name, parameter.valid_values[0])
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
                                         fx_token=effect.token, generation=track.generation)
