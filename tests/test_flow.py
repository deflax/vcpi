"""Routing, metadata, and controller destinations from cached state only."""

import copy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from vcpi.audio_state import ParameterSnapshot, PluginSnapshot
from vcpi.models import InstrumentTrack
from vcpi.sequencer import SequenceBank
from vcpi.presentation.signal_flow import build_flow_snapshot, render_flow_snapshot, show_view


class FlowTests(unittest.TestCase):
    def setUp(self):
        instrument = PluginSnapshot(
            1, name="Synth", descriptive_name="Polyphonic lead synthesizer",
            manufacturer_name="Synth Vendor", category="Instrument",
            version="1.2.3", identifier="synth-unique-id",
            reported_latency_samples=37, info_type="VST3",
            path_to_plugin_file="/plugins/lead.vst3", parameters={
                "cutoff": ParameterSnapshot(range=(20, 20000), is_automatable=True),
                "enabled": ParameterSnapshot(kind="bool", is_automatable=True,
                                             is_boolean=True, is_discrete=True),
                "mode": ParameterSnapshot(kind="enum", valid_values=("Soft", "Hard"),
                                          is_automatable=True, is_discrete=True),
                "unmapped": ParameterSnapshot(range=(0, 1)),
            }, values={"cutoff": 880.0, "enabled": True, "mode": "Hard"})
        effect = PluginSnapshot(
            2, name="Delay", descriptive_name="Stereo tape delay",
            manufacturer_name="Delay Vendor", category="Delay",
            version="2.3.4", identifier="delay-unique-id",
            reported_latency_samples=53, info_type="VST3",
            path_to_plugin_file="/plugins/delay.vst3", parameters={
                "effect_mix": ParameterSnapshot(range=(0, 1), is_automatable=True),
            })
        master = PluginSnapshot(
            3, name="Limiter", descriptive_name="Master peak limiter",
            manufacturer_name="Limiter Vendor", category="Dynamics",
            version="3.4.5", identifier="limiter-unique-id",
            reported_latency_samples=71, info_type="VST3",
            path_to_plugin_file="/plugins/limiter.vst3", parameters={
                "bypass": ParameterSnapshot(kind="bool", is_boolean=True, is_discrete=True),
            })
        self.track = InstrumentTrack("Lead", "lead.vst3", instrument, effects=[effect])
        self.engine = SimpleNamespace(
            tracks=[self.track] + [None] * 7, master_effects=[master], master_gain=1.0,
            channel_map={0: 0, 15: 7}, _failure=None, _closed=False, _health=[1],
            audio_output_name="Output", audio_output_index=0, backend_label="", sample_rate=48000,
            buffer_size=256, output_channels=2,
        )
        self.host = SimpleNamespace(
            engine=self.engine, channel_map=self.engine.channel_map,
            midi_inputs=[SimpleNamespace(port_name="Keyboard")],
            midimix=SimpleNamespace(input_port_name="Mixer", output_port_name=None),
            sequencer=SimpleNamespace(banks=[SequenceBank([60], linked_track=7)] + [None]*15,
                                      _running=True),
            link=SimpleNamespace(enabled=False, _bpm=120),
        )

    def rows(self, section):
        return next(s['rows'] for s in build_flow_snapshot(self.host)['sections']
                    if s['id'] == section)

    def track_state(self):
        return self.rows('track1')[0][1]

    def test_deck_titles_transport_and_midi_mapping_are_cached(self):
        self.track.source_type = "deck"
        self.track.deck = {
            "track_id": "opening", "track_title": "Opening title",
            "requested_track_id": "next", "state": "loading", "play": False,
            "position": 12, "duration": 90, "cue": 4, "pitch": -1.25,
            "nudge": 0, "tracks": [{"id": "opening", "title": "Opening title"}],
        }
        self.engine.request = Mock(side_effect=AssertionError("cache-only"))
        snapshot = build_flow_snapshot(self.host)
        text = render_flow_snapshot(snapshot)
        self.assertIn("Deck: Opening title", text)
        self.assertIn("deck paused", text)
        rows = {row[0]: row[1] for row in self.rows("track1")}
        self.assertEqual(rows["Deck track_id"], "opening")
        self.assertEqual(rows["Deck requested_track_id"], "next")
        self.assertEqual(rows["Deck selection 1"], "Opening title")
        midi = {row[0]: row[1] for row in self.rows("midi")}
        self.assertEqual(midi["Deck note 63"], "nudge_down")
        self.assertEqual(midi["Deck note 64"], "nudge_up")
        self.assertIn("pitch", midi["Deck CC 20"])
        self.engine.request.assert_not_called()

    def test_output_eligibility_tracks_independent_blockers(self):
        self.assertIn('eligible', self.track_state())
        for field, value, reason in (
            ('muted', True, 'muted'), ('enabled', False, 'disabled'),
            ('worker_failure', 'worker crashed', 'worker crashed'), ('gain', 0, 'gain zero'),
        ):
            original = getattr(self.track, field)
            setattr(self.track, field, value)
            self.assertIn(reason, self.track_state())
            self.assertNotIn('eligible', self.track_state())
            setattr(self.track, field, original)
        self.engine.tracks[1] = InstrumentTrack('Solo', '', PluginSnapshot(3), solo=True)
        self.assertIn('solo-excluded', self.track_state())
        self.track.solo = True
        self.assertIn('eligible', self.track_state())
        self.engine._failure = 'owner crashed'
        self.assertIn('audio owner failed', self.track_state())
        self.assertNotIn('eligible', self.track_state())

    def test_knobs_target_instrument_parameters_not_inserts_or_extra_parameters(self):
        knobs = [row for row in self.rows('controls') if row[0].startswith('Track 1 knob')]
        self.assertEqual([row[2].split(': ', 1)[1] for row in knobs],
                         ['cutoff', 'enabled', 'mode'])
        self.assertIn('880', knobs[0][3])
        self.assertIn('20 .. 20000', knobs[0][3])
        self.assertIn('true', knobs[1][3])
        self.assertIn('Soft, Hard', knobs[2][3])
        self.host.midimix.input_port_name = None
        self.assertTrue(all('inactive' in row[4] for row in self.rows('controls')
                            if row[0].startswith('Track 1 knob')))

    def test_empty_destinations_remain_visible_for_midi_and_sequencer(self):
        rows = self.rows('midi')
        channel = next(row for row in rows if row[0] == 'MIDI channel 16')
        bank = next(row for row in rows if row[0] == 'Sequencer bank 1')
        self.assertIn('Track 8', channel[1])
        self.assertIn('empty', channel[2])
        self.assertIn('Track 8', bank[1])
        self.assertIn('empty', bank[2])
        self.assertIn('C4 (60)', bank[2])

    def test_track_preserves_routing_mixer_worker_and_full_instrument_metadata(self):
        rows = self.rows("track1")
        labels = {row[0] for row in rows}
        self.assertTrue({"State", "MIDI channels", "Generic MIDI inputs", "Sequencer banks",
                         "Mixer", "Worker", "Worker buffering", "Audio destination"} <= labels)
        self.assert_plugin_metadata(rows, self.track.plugin, (4, 3, 1, 2))

    def assert_plugin_metadata(self, rows, plugin, counts):
        text = "\n".join(" ".join(row) for row in rows)
        for value in (plugin.name, plugin.descriptive_name, plugin.manufacturer_name,
                      plugin.category, plugin.version, plugin.identifier, plugin.info_type,
                      plugin.path_to_plugin_file, str(plugin.reported_latency_samples)):
            with self.subTest(metadata=value):
                self.assertIn(value, text)
        by_field = {row[0].strip().lower(): row[1] for row in rows}
        for field, count in zip(("reported parameters", "automatable parameters (reported)",
                                 "boolean parameters (reported)", "discrete parameters (reported)"), counts):
            self.assertEqual(by_field[field], str(count))
        self.assertEqual(by_field["sound controls"], str(counts[0]))
        for field in ("plugin settings", "midi parameters", "unused parameters"):
            self.assertEqual(by_field[field], "0")

    def test_effect_sections_preserve_full_metadata_and_render_in_all(self):
        snapshot = build_flow_snapshot(self.host)
        sections = {section["id"]: section for section in snapshot["sections"]}
        rendered = render_flow_snapshot(snapshot)
        for section_id, plugin, counts in (
                ("track1fx1", self.track.effects[0], (1, 1, 0, 0)),
                ("masterfx1", self.engine.master_effects[0], (1, 0, 1, 1))):
            with self.subTest(section=section_id):
                self.assert_plugin_metadata(sections[section_id]["rows"], plugin, counts)
                selected = render_flow_snapshot(snapshot, section_id)
                self.assertIn(plugin.identifier, selected)
                self.assertIn(plugin.identifier, rendered)
        self.assertNotIn(self.engine.master_effects[0].identifier,
                         render_flow_snapshot(snapshot, "track1fx1"))
        self.assertNotIn(self.track.effects[0].identifier,
                         render_flow_snapshot(snapshot, "masterfx1"))

    def test_metadata_snapshot_is_cached_only_and_does_not_mutate_plugins(self):
        forbidden = Mock(side_effect=AssertionError("flow requested live state or mutation"))
        self.engine.request = forbidden
        self.engine.set_parameter = forbidden
        original = copy.deepcopy((self.track, self.engine.master_effects))
        with patch("vcpi.midi.list_midi_input_ports", forbidden), \
                patch("vcpi.midi.list_midi_output_ports", forbidden):
            snapshot = build_flow_snapshot(self.host)
            for section_id in ("track1", "track1fx1", "masterfx1"):
                render_flow_snapshot(snapshot, section_id)
        forbidden.assert_not_called()
        self.assertEqual((self.track, self.engine.master_effects), original)

    def test_effect_sections_follow_current_cached_chains(self):
        self.track.effects.clear()
        self.engine.master_effects.clear()
        snapshot = build_flow_snapshot(self.host)
        section_ids = {section["id"] for section in snapshot["sections"]}
        self.assertNotIn("track1fx1", section_ids)
        self.assertNotIn("masterfx1", section_ids)
        for section_id in ("track1fx1", "masterfx1"):
            with self.subTest(section=section_id), self.assertRaises(ValueError):
                render_flow_snapshot(snapshot, section_id)

    def test_show_view_resolves_named_tracks_and_explicit_effect_indexes(self):
        self.assertEqual(show_view([]), "overview")
        for view in ("all", "midi", "controls", "audio", "master"):
            self.assertEqual(show_view([view]), view)
        for index in range(1, 9):
            self.assertEqual(show_view([str(index)]), f"track{index}")
            self.assertEqual(show_view([str(index), "fx", "2"]), f"track{index}fx2")
        self.assertEqual(show_view(["master", "fx", "1"]), "masterfx1")
        self.assertEqual(show_view(["master", "fx", "12"]), "masterfx12")

    def test_show_view_rejects_missing_nonpositive_and_nonnumeric_effect_indexes(self):
        for target in ("1", "master"):
            for suffix in (["fx"], ["fx", "0"], ["fx", "-1"], ["fx", "1.5"],
                           ["fx", "nope"], ["fx", ""], ["fx", "1", "extra"],
                           ["1"], ["effect", "1"]):
                arguments = [target, *suffix]
                with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                    show_view(arguments)
        for arguments in (["0"], ["9"], ["track1"], ["midi", "fx", "1"],
                          ["explore"], ["0", "fx", "1"], ["9", "fx", "1"]):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                show_view(arguments)


if __name__ == '__main__':
    unittest.main()
