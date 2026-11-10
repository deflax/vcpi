"""Hardware-free BeatStep bytes through focused routing and typed control APIs."""

from dataclasses import replace
import io
from types import SimpleNamespace
import unittest

from vcpi.audio_state import ParameterSnapshot, PluginSnapshot
from vcpi.commands.cli import HostCLI
from vcpi.controllers.beatstep_pro import BeatStepProController, BeatStepProProfile
from vcpi.deps import HAS_MIDO
from vcpi.models import NUM_TRACKS, InstrumentTrack, TrackFocus
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
            InstrumentTrack("Synth", "", PluginSnapshot(n), source_type="vcv", generation=n)
            for n in (9, 10, 11)] + [None] * 11
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


class TrackFocusTests(unittest.TestCase):
    def test_selection_revisions_change_only_with_the_selected_track(self):
        focus = TrackFocus()
        self.assertEqual(focus.snapshot(), (0, 0))
        for index in range(NUM_TRACKS):
            focus.select(index)
            self.assertEqual(focus.snapshot(), (index, index))
            focus.select(index)
            self.assertEqual(focus.snapshot(), (index, index))
        focus.select(0)
        self.assertEqual(focus.snapshot(), (0, NUM_TRACKS))

    def test_invalid_selection_preserves_index_and_revision(self):
        focus = TrackFocus()
        focus.select(7)
        before = focus.snapshot()
        for index in (-1, NUM_TRACKS, True, False, 1.0, "1", None):
            with self.subTest(index=index):
                with self.assertRaises(ValueError):
                    focus.select(index)
                self.assertEqual(focus.snapshot(), before)


@unittest.skipUnless(HAS_MIDO, "mido required for channel MIDI parsing")
class BeatStepProTests(unittest.TestCase):
    def setUp(self):
        self.engine = RecordingEngine()
        self.now = 10.0
        self.timeline = MusicalTimeline(LinkSync(), clock=lambda: self.now)
        self.track_focus = TrackFocus()
        self.controller = BeatStepProController(
            self.engine, self.timeline, self.track_focus)
        self.addCleanup(self.controller.close)
        self.output = io.StringIO()
        self.cli = HostCLI(SimpleNamespace(track_focus=self.track_focus),
                           stdout=self.output, owns_host=False)

    def execute(self, command):
        self.output.seek(0)
        self.output.truncate()
        self.cli.onecmd(command)
        return self.output.getvalue()

    def send(self, *raw):
        self.controller.on_midi((list(raw), 0.012))

    def button(self, ordinal, value=127):
        self.send(0xBF, self.controller.profile.button_numbers[ordinal], value)

    def press(self, ordinal):
        self.button(ordinal)
        self.button(ordinal, 0)

    def rotary(self, ordinal, value):
        self.send(0xBF, self.controller.profile.rotary_ccs[ordinal], value)

    def pad(self, ordinal, value=127, *, note_off=False):
        profile = self.controller.profile
        if profile.pad_type == "cc":
            self.send(0xB0 | profile.control_channel, profile.pad_numbers[ordinal], value)
        else:
            status = 0x80 if note_off else 0x90
            self.send(status | profile.control_channel, profile.pad_numbers[ordinal], value)

    def tap_pad(self, ordinal):
        self.pad(ordinal)
        self.pad(ordinal, 0)

    def configure(self, **fields):
        self.controller.close()
        self.controller = BeatStepProController(
            self.engine, self.timeline, self.track_focus,
            profile=replace(BeatStepProProfile(), **fields))
        self.addCleanup(self.controller.close)

    def synth(self, count, *, generation=30):
        return InstrumentTrack("Synth", "", PluginSnapshot(
            generation, parameters={
                f"p{n}": ParameterSnapshot(range=(0, 127)) for n in range(count)}),
            source_type="vcv", generation=generation)

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

    def test_focus_starts_on_track_one_and_all_sixteen_steps_only_select(self):
        self.assertEqual(self.track_focus.snapshot(), (0, 0))
        self.engine.tracks[15] = deck(80)
        for index in range(16):
            self.press(index)
            self.assertEqual(self.track_focus.snapshot()[0], index)
        self.assertEqual(self.engine.commands, [])
        self.rotary(0, 127)
        self.tap_pad(0)
        self.assertEqual(self.engine.commands, [
            (15, "mixer", {"gain": 1.0}, None, 80),
            (15, "play_toggle", 0, None, 80)])

    def test_empty_track_remains_selected_without_commands(self):
        self.press(14)
        for ordinal in range(16):
            self.rotary(ordinal, 127)
        for ordinal in range(7):
            self.tap_pad(ordinal)
        self.assertEqual(self.track_focus.snapshot()[0], 14)
        self.assertEqual(self.engine.commands, [])
        self.assertEqual(self.engine.midi, [])

    def test_external_focus_routes_rotaries_and_pads_without_step_buttons(self):
        self.track_focus.select(1)
        self.rotary(0, 127)
        self.tap_pad(0)
        self.track_focus.select(0)
        self.rotary(1, 0)
        self.tap_pad(1)
        self.assertEqual(self.engine.commands, [
            (1, "mixer", {"gain": 1.0}, None, 8), (1, "play_toggle", 0, None, 8),
            (0, "pitch", -16, None, 7), (0, "cue", 0, None, 7)])

    def test_midi_focus_is_visible_to_cli_query_without_mutation(self):
        for index in (0, 1, 15):
            with self.subTest(index=index):
                self.press(index)
                before = self.track_focus.snapshot()
                self.assertEqual(before[0], index)
                self.assertRegex(self.execute("focus"), rf"\b{index + 1}\b")
                self.assertEqual(self.track_focus.snapshot(), before)
        self.assertEqual(self.engine.commands, [])
        self.assertEqual(self.engine.midi, [])

    def test_cli_focus_routes_midi_controls_to_both_track_boundaries(self):
        self.engine.tracks[15] = deck(80)
        self.execute("focus 16")
        self.assertEqual(self.track_focus.snapshot()[0], 15)
        self.rotary(0, 127)
        self.tap_pad(0)
        self.execute("focus 1")
        self.assertEqual(self.track_focus.snapshot()[0], 0)
        self.rotary(0, 0)
        self.tap_pad(1)
        self.assertEqual(self.engine.commands, [
            (15, "mixer", {"gain": 1.0}, None, 80),
            (15, "play_toggle", 0, None, 80),
            (0, "mixer", {"gain": 0.0}, None, 7), (0, "cue", 0, None, 7)])

    def test_cli_can_focus_empty_track_without_changing_transport_or_routing(self):
        self.send(0xFA)
        before = self.timeline.snapshot()
        channel_map = dict(self.engine.channel_map)
        self.execute("focus 16")
        self.assertEqual(self.track_focus.snapshot(), (15, 1))
        self.rotary(0, 127)
        self.tap_pad(0)
        self.assertEqual(self.engine.commands, [])
        self.assertTrue(self.timeline.running)
        self.assertEqual(self.timeline.snapshot().beat, before.beat)
        self.assertEqual(self.engine.channel_map, channel_map)
        for channel in (0, 1, 9):
            self.send(0x90 | channel, 60, 100)
            self.send(0x80 | channel, 60, 0)
        self.assertEqual([index for index, _, _ in self.engine.midi],
                         [2, 2, 3, 3, 4, 4])

    def test_invalid_cli_focus_preserves_shared_state_and_control_destinations(self):
        self.execute("focus 2")
        before = self.track_focus.snapshot()
        self.assertEqual(before, (1, 1))
        for argument in ("0", "17", "-1", "1.5", "nan", "true", "track",
                         "1 2", "1 extra", '"1'):
            with self.subTest(argument=argument):
                self.execute("focus " + argument)
                self.assertEqual(self.track_focus.snapshot(), before)
                self.assertEqual(self.engine.commands, [])
                self.assertEqual(self.engine.midi, [])
        self.rotary(0, 127)
        self.tap_pad(0)
        self.assertEqual(self.engine.commands, [
            (1, "mixer", {"gain": 1.0}, None, 8), (1, "play_toggle", 0, None, 8)])

    def test_pad_edges_actions_and_rapid_sync_toggle(self):
        self.pad(0)
        self.pad(0)
        self.pad(0, 0, note_off=True)
        for ordinal in (1, 2, 3, 3, 6):
            self.tap_pad(ordinal)
        self.assertEqual(self.engine.commands, [
            (0, "play_toggle", 0, None, 7), (0, "cue", 0, None, 7),
            (0, "cue_set", 0, None, 7), (0, "sync", True, None, 7),
            (0, "sync", False, None, 7), (0, "phase_reset", 0, None, 7)])

    def test_opposed_nudges_release_original_track_after_focus_change(self):
        self.pad(4)
        self.pad(5)
        self.press(1)
        self.pad(4, 0)
        self.pad(5, 0, note_off=True)
        self.assertEqual(self.engine.commands, [
            (0, "nudge", -1, None, 7), (0, "nudge", 0, None, 7),
            (0, "nudge", 1, None, 7), (0, "nudge", 0, None, 7)])
        self.pad(4)
        self.controller.clear_deck_holds(channel=15)
        self.assertEqual(self.engine.commands[-2:], [
            (1, "nudge", -1, None, 8), (1, "nudge", 0, None, 8)])

    def test_nudge_release_preserves_replaced_source_generation(self):
        self.pad(4)
        self.engine.tracks[0] = deck(77)
        self.rotary(0, 127)  # Observe replacement before the old physical release.
        self.pad(4, 0)
        self.assertEqual(self.engine.commands[-1], (0, "nudge", 0, None, 7))
        self.pad(4)
        self.assertEqual(self.engine.commands[-1], (0, "nudge", -1, None, 77))
        self.controller.close()
        self.assertEqual(self.engine.commands[-1], (0, "nudge", 0, None, 77))

    def test_nudge_release_after_cli_focus_uses_original_track_and_generation(self):
        self.pad(4)
        self.execute("focus 2")
        self.engine.tracks[0] = deck(77)
        self.assertEqual(self.engine.commands, [(0, "nudge", -1, None, 7)])
        self.pad(4, 0, note_off=True)
        self.tap_pad(4)
        self.assertEqual(self.engine.commands, [
            (0, "nudge", -1, None, 7), (0, "nudge", 0, None, 7),
            (1, "nudge", -1, None, 8), (1, "nudge", 0, None, 8)])

    def test_rotaries_cover_gain_pitch_coarse_fine_and_phase(self):
        self.press(1)
        for ordinal, value in ((0, 127), (1, 0), (2, 64), (3, 64),
                               (3, 65), (4, 0), (4, 127)):
            self.rotary(ordinal, value)
        for ordinal in (5, 6, 7):
            self.rotary(ordinal, 127)
        self.assertEqual(self.engine.commands, [
            (1, "mixer", {"gain": 1.0}, None, 8), (1, "pitch", -16, None, 8),
            (1, "seek", 64, None, 8), (1, "seek", 64.1, None, 8),
            (1, "phase_offset", -.5, None, 8), (1, "phase_offset", .5, None, 8)])

    def test_all_relative_encodings_use_signed_seek_deltas(self):
        for mode, positive, negative in (
                ("twos_complement", 1, 127), ("offset", 65, 63),
                ("sign_magnitude", 1, 65)):
            with self.subTest(mode=mode):
                self.configure(rotary_modes=(mode,) * 16)
                self.engine.commands.clear()
                self.rotary(2, positive)
                self.rotary(2, negative)
                self.rotary(3, positive)
                self.assertEqual([command[2] for command in self.engine.commands],
                                 [17, 12, 12.1])

    def test_song_steps_are_immediate_accumulate_and_clamp(self):
        for expected in (2, 3, 4, 4):
            self.rotary(8, 1)
            self.assertEqual(self.engine.commands[-1], (0, "track_select", expected, None, 7))
        for expected in (3, 2, 1, 1):
            self.rotary(8, 127)
            self.assertEqual(self.engine.commands[-1], (0, "track_select", expected, None, 7))
        self.assertEqual(self.engine.tracks[0].plugin.values["track_select"], 1)
        self.engine.tracks[0] = deck(77)
        self.rotary(8, 1)
        self.assertEqual(self.engine.commands[-1], (0, "track_select", 2, None, 77))

    def test_absolute_delta_controls_seed_again_after_focus_change(self):
        self.configure(rotary_modes=("absolute",) * 16)
        self.rotary(8, 64)
        self.rotary(3, 64)
        self.assertEqual(self.engine.commands, [])
        self.rotary(8, 65)
        self.rotary(3, 65)
        self.assertEqual(self.engine.commands, [
            (0, "track_select", 2, None, 7), (0, "seek", 12.1, None, 7)])
        self.press(1)
        self.rotary(8, 10)
        self.rotary(3, 10)
        self.assertEqual(len(self.engine.commands), 2)
        self.rotary(8, 11)
        self.rotary(3, 11)
        self.assertEqual(self.engine.commands[-2:], [
            (1, "track_select", 2, None, 8), (1, "seek", 12.1, None, 8)])
        self.press(0)
        self.rotary(8, 100)
        self.rotary(3, 100)
        self.assertEqual(len(self.engine.commands), 4)

    def test_external_focus_away_and_back_reseeds_absolute_delta_controls(self):
        self.configure(rotary_modes=("absolute",) * 16)
        self.rotary(8, 64)
        self.rotary(3, 64)
        self.track_focus.select(1)
        self.track_focus.select(0)
        self.rotary(8, 10)
        self.rotary(3, 10)
        self.assertEqual(self.engine.commands, [])
        self.rotary(8, 11)
        self.rotary(3, 11)
        self.assertEqual(self.engine.commands, [
            (0, "track_select", 2, None, 7), (0, "seek", 12.1, None, 7)])

    def test_reselecting_same_focus_preserves_absolute_delta_baselines(self):
        self.configure(rotary_modes=("absolute",) * 16)
        self.rotary(8, 64)
        self.rotary(3, 64)
        before = self.track_focus.snapshot()
        self.track_focus.select(0)
        self.execute("focus 1")
        self.press(0)
        self.assertEqual(self.track_focus.snapshot(), before)
        self.rotary(8, 65)
        self.rotary(3, 65)
        self.assertEqual(self.engine.commands, [
            (0, "track_select", 2, None, 7), (0, "seek", 12.1, None, 7)])

    def test_deck_inserts_flatten_seven_exposed_typed_parameters(self):
        self.engine.tracks[0].effects = [
            PluginSnapshot(20, name="plugdata-fx", manufacturer_name="plugdata", parameters={
                "volume": ParameterSnapshot(range=(0, 2)),
                "midi": ParameterSnapshot(label="MIDI CC 0|1"),
                "amount": ParameterSnapshot(range=(-2, 2)),
                "switch": ParameterSnapshot(kind="bool")}),
            PluginSnapshot(21, parameters={
                "mode": ParameterSnapshot(kind="enum", valid_values=("A", "B", "C")),
                "count": ParameterSnapshot(kind="int", range=(2, 10)),
                **{f"extra{n}": ParameterSnapshot(range=(0, 1)) for n in range(4)}})]
        for ordinal in range(9, 16):
            self.rotary(ordinal, 127)
        self.assertEqual(self.engine.commands, [
            (0, "amount", 2, 0, 7), (0, "switch", True, 0, 7),
            (0, "mode", "C", 1, 7), (0, "count", 10, 1, 7),
            *[(0, f"extra{n}", 1, 1, 7) for n in range(3)]])
        self.engine.tracks[0].effects = [PluginSnapshot(22, parameters={
            "new": ParameterSnapshot(range=(10, 20))})]
        self.rotary(9, 0)
        self.assertEqual(self.engine.commands[-1], (0, "new", 10, 0, 7))

    def test_non_deck_banks_have_fourteen_controls_and_clamp(self):
        self.engine.tracks[2] = self.synth(29)
        self.press(2)
        slots = (*range(1, 8), *range(9, 16))
        for ordinal in slots:
            self.rotary(ordinal, 127)
        self.assertEqual([command[1] for command in self.engine.commands],
                         [f"p{n}" for n in range(14)])
        self.rotary(8, 1)
        for ordinal in slots:
            self.rotary(ordinal, 0)
        self.assertEqual([command[1] for command in self.engine.commands[-14:]],
                         [f"p{n}" for n in range(14, 28)])
        self.rotary(8, 63)
        self.rotary(1, 127)
        self.assertEqual(self.engine.commands[-1], (2, "p28", 127, None, 30))
        count = len(self.engine.commands)
        self.rotary(2, 127)
        self.assertEqual(len(self.engine.commands), count)
        self.rotary(8, 64)
        self.rotary(1, 0)
        self.assertEqual(self.engine.commands[-1], (2, "p0", 0, None, 30))

    def test_banks_persist_per_track_clamp_on_metadata_and_reset_on_generation(self):
        self.engine.tracks[2] = self.synth(29)
        self.engine.tracks[3] = self.synth(29, generation=31)
        self.press(2)
        self.rotary(8, 2)
        self.press(3)
        self.rotary(1, 127)
        self.assertEqual(self.engine.commands[-1], (3, "p0", 127, None, 31))
        self.press(2)
        self.rotary(1, 127)
        self.assertEqual(self.engine.commands[-1], (2, "p28", 127, None, 30))
        self.engine.tracks[2].plugin = self.synth(15).plugin
        self.rotary(1, 127)
        self.assertEqual(self.engine.commands[-1], (2, "p14", 127, None, 30))
        self.engine.tracks[2] = self.synth(29, generation=99)
        self.rotary(1, 127)
        self.assertEqual(self.engine.commands[-1], (2, "p0", 127, None, 99))

    def test_absolute_bank_delta_tracks_each_step_and_reseeds_on_external_focus(self):
        self.configure(rotary_modes=("absolute",) * 16)
        self.engine.tracks[2] = self.synth(43)
        self.press(2)
        self.rotary(8, 64)
        self.rotary(8, 65)
        self.rotary(1, 127)
        self.assertEqual(self.engine.commands[-1], (2, "p14", 127, None, 30))
        self.rotary(8, 66)
        self.rotary(1, 0)
        self.assertEqual(self.engine.commands[-1], (2, "p28", 0, None, 30))
        self.rotary(8, 67)
        self.rotary(1, 127)
        self.assertEqual(self.engine.commands[-1], (2, "p42", 127, None, 30))
        self.track_focus.select(0)
        self.track_focus.select(2)
        self.rotary(8, 10)
        self.rotary(1, 0)
        self.assertEqual(self.engine.commands[-1], (2, "p42", 0, None, 30))

    def test_relative_plugin_parameters_preserve_types_and_skip_global_volume(self):
        self.configure(rotary_modes=("twos_complement",) * 16)
        self.engine.tracks[2].plugin = PluginSnapshot(
            40, name="plugdata", manufacturer_name="plugdata", parameters={
                "volume": ParameterSnapshot(range=(0, 2)),
                "amount": ParameterSnapshot(range=(-2, 2)),
                "switch": ParameterSnapshot(kind="bool"),
                "mode": ParameterSnapshot(kind="enum", valid_values=("A", "B", "C")),
                "count": ParameterSnapshot(kind="int", range=(2, 10))})
        self.press(2)
        for ordinal in range(1, 5):
            for delta in (63, 63, 1):
                self.rotary(ordinal, delta)
        self.assertEqual(self.engine.commands[2::3], [
            (2, "amount", 2, None, 9), (2, "switch", True, None, 9),
            (2, "mode", "C", None, 9), (2, "count", 10, None, 9)])
        self.assertIs(type(self.engine.commands[5][2]), bool)
        self.assertIs(type(self.engine.commands[11][2]), int)
        for ordinal in range(1, 5):
            for delta in (64, 65):
                self.rotary(ordinal, delta)
        self.assertEqual(self.engine.commands[13::2], [
            (2, "amount", -2, None, 9), (2, "switch", False, None, 9),
            (2, "mode", "A", None, 9), (2, "count", 2, None, 9)])

    def test_non_deck_gain_works_but_pads_and_sequence_to_decks_do_not(self):
        self.press(2)
        self.rotary(0, 127)
        self.assertEqual(self.engine.commands, [(2, "mixer", {"gain": 1.0}, None, 9)])
        self.engine.commands.clear()
        for ordinal in range(7):
            self.tap_pad(ordinal)
        self.engine.tracks[2] = deck(90)
        self.send(0x90, 60, 100)
        self.assertEqual(self.engine.commands, [])
        self.assertEqual(self.engine.midi, [])

    def test_configurable_note_steps_cc_pads_and_channel_separation(self):
        self.configure(button_type="note", button_numbers=tuple(range(80, 96)),
                       pad_type="cc", pad_numbers=tuple(range(60, 67)))
        self.send(0x9F, 81, 127)
        self.send(0x9F, 81, 0)
        self.assertEqual(self.track_focus.snapshot()[0], 1)
        self.pad(0)
        self.pad(0)
        self.pad(0, 0)
        self.tap_pad(0)
        self.assertEqual(self.engine.commands, [(1, "play_toggle", 0, None, 8)] * 2)
        self.send(0x90, 60, 127)
        self.assertEqual(self.engine.midi[-1][0], 2)
        with self.assertRaises(ValueError):
            BeatStepProProfile(control_channel=0)
