"""Hardware-free BeatStep bytes through fixed routing and typed control APIs."""

from dataclasses import replace
import unittest
from types import SimpleNamespace

from vcpi.audio_state import ParameterSnapshot, PluginSnapshot
from vcpi.controllers.beatstep_pro import BeatStepProController, BeatStepProProfile
from vcpi.deps import HAS_MIDO
from vcpi.models import InstrumentTrack
from vcpi.link import LinkSync
from vcpi.timeline import MusicalTimeline


def deck(generation=7):
    plugin = PluginSnapshot(1, values={"track_select": 1})
    track = InstrumentTrack("Deck", "", plugin, source_type="deck", generation=generation)
    track.deck = {"sync": False, "pitch": 0.0, "position": 12.0,
                  "duration": 127.0, "tracks": [{"id": str(n)} for n in range(4)]}
    return track


class RecordingEngine:
    def __init__(self):
        self.tracks = [deck(), deck(8)] + [
            InstrumentTrack("Synth", "", PluginSnapshot(n), generation=n)
            for n in (9, 10, 11)] + [None] * 3
        # Deliberately misleading generic map: BeatStep must never consult it.
        self.channel_map = {0: 0, 1: 7, 9: 1, 15: 4}
        self.commands = []
        self.midi = []

    def enqueue_midi(self, index, message, *, generation):
        self.midi.append((index, message, generation))

    def enqueue_deck_command(self, index, action, value=0, *, generation):
        self.commands.append((index, action, value, None, generation))

    def enqueue_param_change(self, index, name, value, *, generation, fx_index=None,
                             fx_token=None):
        self.commands.append((index, name, value, fx_index, generation))

    def enqueue_track_change(self, index, *, generation, **fields):
        self.commands.append((index, "mixer", fields, None, generation))


@unittest.skipUnless(HAS_MIDO, "mido required for channel MIDI parsing")
class BeatStepProTests(unittest.TestCase):
    def setUp(self):
        self.engine = RecordingEngine()
        self.now = 10.0
        self.timeline = MusicalTimeline(LinkSync(), clock=lambda: self.now)
        self.controller = BeatStepProController(self.engine, self.timeline)
        self.addCleanup(self.controller.close)

    def send(self, *raw):
        self.controller.on_midi((list(raw), 0.012))

    def button(self, ordinal, value=127):
        self.send(0xBF, self.controller.profile.button_numbers[ordinal], value)

    def press(self, ordinal):
        self.button(ordinal)
        self.button(ordinal, 0)

    def rotary(self, ordinal, value):
        self.send(0xBF, self.controller.profile.rotary_ccs[ordinal], value)

    def test_mmc_stop_and_resume_preserve_unknown_external_position(self):
        for _ in range(110):
            self.now += 1 / 48
            self.send(0xF8)
        self.assertEqual(self.timeline.active_source, "beatstep")
        self.assertFalse(self.timeline.position_confirmed)
        self.send(0xF0, 0x7F, 0x7F, 0x06, 0x01, 0xF7)
        stopped = self.timeline.snapshot()
        self.now += 1
        self.assertFalse(self.timeline.running)
        self.assertAlmostEqual(self.timeline.snapshot().beat, stopped.beat)
        self.send(0xF0, 0x7F, 0x7F, 0x06, 0x02, 0xF7)
        self.assertTrue(self.timeline.running)
        self.assertFalse(self.timeline.position_confirmed)
        self.assertAlmostEqual(self.timeline.snapshot().beat, stopped.beat)

    def test_repeated_realtime_start_restarts_even_without_intervening_stop(self):
        self.send(0xFA)
        self.now += .25
        self.assertGreater(self.timeline.snapshot().beat, .4)
        self.send(0xFA)
        self.assertEqual(self.timeline.snapshot().beat, 0)
        self.assertTrue(self.timeline.position_confirmed)

    def test_sequences_route_to_tracks_three_four_five_independently(self):
        for channel in (0, 1, 9):
            self.send(0x90 | channel, 60, 100)
            self.send(0x90 | channel, 60, 0)
        self.assertEqual([index for index, _, _ in self.engine.midi], [2, 2, 3, 3, 4, 4])
        self.assertEqual([message.type for _, message, _ in self.engine.midi],
                         ["note_on", "note_off"] * 3)
        self.assertEqual(self.engine.commands, [])
        self.send(0x92, 60, 100)
        self.assertEqual(len(self.engine.midi), 6)

    def test_generation_safe_note_releases_and_close_sustain_cleanup(self):
        self.send(0x90, 60, 100)
        self.send(0xB0, 64, 127)
        self.engine.tracks[2] = replace(self.engine.tracks[2], generation=99)
        self.send(0x80, 60, 0)
        self.assertEqual(self.engine.midi[-1][2], 9)
        self.controller.close()
        self.assertEqual(self.engine.midi[-1][1].dict(), {
            "type": "control_change", "time": 0, "channel": 0, "control": 64, "value": 0})
        self.assertEqual(self.engine.midi[-1][2], 9)

    def test_lifecycle_hook_releases_only_selected_track_notes(self):
        self.send(0x90, 60, 100)
        self.send(0x91, 61, 100)
        self.controller.clear_deck_holds(track_index=2)
        self.assertEqual(self.engine.midi[-1][0], 2)
        self.assertEqual(self.engine.midi[-1][1].type, "note_off")
        self.send(0x81, 61, 0)
        self.assertEqual(self.engine.midi[-1][0], 3)

    def test_button_edges_both_decks_and_rapid_sync_selection(self):
        self.button(0)
        self.button(0)
        self.button(0, 0)
        self.press(9)
        self.press(10)
        self.press(3)
        self.press(3)
        self.press(5)
        self.press(5)
        self.press(4)
        self.assertEqual(self.engine.commands, [
            (0, "play_toggle", 0, None, 7), (1, "cue", 0, None, 8),
            (1, "cue_set", 0, None, 8), (0, "sync", True, None, 7),
            (0, "sync", False, None, 7), (0, "track_select", 2, None, 7),
            (0, "track_select", 3, None, 7), (0, "track_select", 2, None, 7)])
        self.assertFalse(any(command[1] == "play" for command in self.engine.commands))

    def test_opposed_nudges_release_and_replacement_generation(self):
        self.button(6)
        self.button(7)
        self.button(6, 0)
        self.controller.clear_deck_holds(channel=15)
        self.assertEqual([command[2] for command in self.engine.commands], [-1, 0, 1, 0])
        self.button(6)
        self.engine.tracks[0] = deck(77)
        self.button(6, 0)  # Old release must not nudge the replacement.
        self.assertEqual(self.engine.commands[-1][-1], 7)
        self.button(6)
        self.assertEqual(self.engine.commands[-1], (0, "nudge", -1, None, 77))

    def test_rotaries_cover_gain_pitch_coarse_and_fine_seek(self):
        self.rotary(0, 127)
        self.rotary(1, 0)
        self.rotary(2, 64)
        self.rotary(3, 64)  # First absolute sample seeds fine movement.
        self.rotary(3, 65)
        self.rotary(4, 0)
        self.rotary(5, 127)
        self.assertEqual(self.engine.commands, [
            (0, "mixer", {"gain": 1.0}, None, 7), (0, "pitch", -16, None, 7),
            (0, "seek", 64, None, 7), (0, "seek", 64.1, None, 7),
            (1, "mixer", {"gain": 0.0}, None, 8), (1, "pitch", 16, None, 8)])

    def test_relative_wire_encoding_uses_deltas_not_absolute_values(self):
        self.controller = BeatStepProController(self.engine, self.timeline, profile=replace(
            BeatStepProProfile(), rotary_modes=("twos_complement",) * 16))
        self.rotary(2, 1)
        self.rotary(2, 127)
        self.rotary(3, 1)
        self.assertEqual([command[2] for command in self.engine.commands], [17, 12, 12.1])

    def test_effects_flatten_exposed_insert_parameters_with_typed_ranges(self):
        self.engine.tracks[0].effects = [
            PluginSnapshot(20, parameters={
                "midi": ParameterSnapshot(label="MIDI CC 0|1"),
                "amount": ParameterSnapshot(range=(-2, 2)),
                "switch": ParameterSnapshot(kind="bool")}),
            PluginSnapshot(21, parameters={
                "mode": ParameterSnapshot(kind="enum", valid_values=("A", "B", "C")),
                "count": ParameterSnapshot(kind="int", range=(2, 10))})]
        bindings = self.controller.control_bindings()[24:28]
        self.assertEqual([binding.target for binding in bindings], [
            "Track 1 deck: FX 1: amount", "Track 1 deck: FX 1: switch",
            "Track 1 deck: FX 2: mode", "Track 1 deck: FX 2: count",
        ])
        self.assertEqual([binding.range for binding in bindings], [
            "-2 .. 2", "false / true", "indices 0-2: A, B, C", "2 .. 10",
        ])
        for ordinal in range(8, 12):
            self.rotary(ordinal, 127)
        self.assertEqual(self.engine.commands, [
            (0, "amount", 2, 0, 7), (0, "switch", True, 0, 7),
            (0, "mode", "C", 1, 7), (0, "count", 10, 1, 7)])
        self.engine.tracks[0].effects = [PluginSnapshot(22, parameters={
            "new": ParameterSnapshot(range=(10, 20))})]
        self.rotary(8, 0)
        self.assertEqual(self.engine.commands[-1], (0, "new", 10, 0, 7))
        self.assertEqual(self.engine.tracks[0].plugin.values, {"track_select": 1})
        binding = self.controller.control_bindings()[24]
        self.assertEqual(binding.target, "Track 1 deck: FX 1: new")
        self.assertEqual(binding.range, "10 .. 20")
        self.assertEqual(self.controller.control_bindings()[25].availability, "unbound")

    def test_deck_controls_do_nothing_on_non_decks_and_sequences_never_drive_decks(self):
        self.engine.tracks[0].source_type = "vst"
        for ordinal in range(8):
            self.press(ordinal)
            self.rotary(ordinal if ordinal < 4 else ordinal + 4, 127)
        self.engine.tracks[2] = deck(90)
        self.send(0x90, 60, 100)
        self.assertEqual(self.engine.commands, [])
        self.assertEqual(self.engine.midi, [])

    def test_existing_note_template_and_channel_separation(self):
        self.controller = BeatStepProController(self.engine, self.timeline, profile=replace(
            BeatStepProProfile(), button_type="note", button_numbers=tuple(range(60, 76))))
        self.send(0x9F, 60, 127)
        self.send(0x9F, 60, 0)
        self.send(0x9F, 60, 127)
        self.assertEqual(len(self.engine.commands), 2)
        self.send(0x90, 60, 127)
        self.assertEqual(self.engine.midi[-1][0], 2)
        with self.assertRaises(ValueError):
            BeatStepProProfile(control_channel=0)



class BeatStepBindingTests(unittest.TestCase):
    def test_cached_bindings_separate_profile_mode_values_and_availability(self):
        engine = RecordingEngine()
        controller = BeatStepProController(engine, MusicalTimeline(LinkSync()))
        self.addCleanup(controller.close)
        engine.tracks[0].muted = True
        engine.tracks[0].enabled = False
        engine.tracks[0].effects = [PluginSnapshot(20, parameters={
            "amount": ParameterSnapshot(range=(-2, 2), units="dB"),
            "unsupported": ParameterSnapshot(range=()),
        }, values={"amount": 0.5})]
        bindings = controller.control_bindings()
        self.assertEqual(len(bindings), 32)
        self.assertEqual(bindings[16].target, "Track 1 deck: gain")
        self.assertEqual(bindings[16].range, "0 .. 1")
        self.assertEqual(bindings[16].availability, "disconnected")
        self.assertEqual(bindings[24].value, "0.5 dB")
        self.assertEqual(bindings[24].range, "-2 .. 2")
        self.assertEqual(bindings[24].mode, "absolute")
        self.assertEqual(bindings[24].track, 1)
        self.assertEqual(bindings[25].availability, "unsupported")
        self.assertEqual(bindings[26].availability, "unbound")
        controller._port = SimpleNamespace(name="Connected", close=lambda: None)
        self.assertEqual(controller.control_bindings()[24].availability, "mapped")
        engine.tracks[0].source_type = "vst"
        self.assertEqual(controller.control_bindings()[16].availability, "unsupported")
        engine.tracks[0] = None
        self.assertEqual(controller.control_bindings()[16].availability, "unavailable")
        self.assertEqual(engine.commands, [])
        self.assertEqual(engine.midi, [])
