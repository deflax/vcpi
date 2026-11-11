"""Routing, metadata, and controller destinations from cached state only."""

import copy
import io
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, PropertyMock, patch

from vcpi.audio_state import ParameterSnapshot, PluginSnapshot
from vcpi.commands.cli import HostCLI
from vcpi.models import NUM_TRACKS, InstrumentTrack, TrackFocus
from vcpi.sequencer import SequenceBank
from vcpi.presentation.signal_flow import build_flow_snapshot, render_flow_snapshot
from vcpi.presentation import flow_display
from vcpi.link import LinkSync
from vcpi.timeline import MusicalTimeline
from vcpi.controllers.beatstep_pro import BeatStepProController


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
        self.track = InstrumentTrack("Lead", "lead.vst3", instrument, effects=[effect], source_type="vcv")
        self.engine = SimpleNamespace(
            tracks=[self.track] + [None] * (NUM_TRACKS - 1), master_effects=[master], master_gain=1.0,
            channel_map={0: 0, 15: 15}, _failure=None, _closed=False, _health=[1],
            audio_output_name="Output", audio_output_index=0, backend_label="", sample_rate=48000,
            buffer_size=256, output_channels=2,
        )
        self.host = SimpleNamespace(
            engine=self.engine, channel_map=self.engine.channel_map,
            midi_inputs=[SimpleNamespace(port_name="Keyboard")],
            midimix=SimpleNamespace(input_port_name="Mixer", output_port_name=None),
            sequencer=SimpleNamespace(banks=[SequenceBank([60], linked_track=15)] + [None]*15,
                                      _running=True),
            link=LinkSync(),
        )
        self.host.timeline = MusicalTimeline(self.host.link)
        self.host.track_focus = TrackFocus()
        self.host.beatstep = BeatStepProController(
            self.engine, self.host.timeline, self.host.track_focus)

    def section(self, section_id, *, details=False):
        return next(section for section in build_flow_snapshot(self.host, details=details)["sections"]
                    if section["id"] == section_id)

    def rows(self, section_id, *, details=False):
        return self.section(section_id, details=details)["rows"]

    def track_state(self):
        return next(row[1] for row in self.rows("track1") if row[0] == "State")

    def test_overview_keeps_all_sixteen_tracks(self):
        snapshot = build_flow_snapshot(self.host)
        overview = next(section for section in snapshot["sections"] if section["id"] == "overview")
        labels = [row[0] for row in overview["rows"]]
        self.assertEqual([label for label in labels if label.startswith("Track ")],
                         [f"Track {index}" for index in range(1, 17)])
        self.assertTrue({"Master", "Output", "Controls", "Problems"} <= set(labels))
        rendered = render_flow_snapshot(snapshot, "overview")
        self.assertNotIn("/plugins/", rendered)
        self.assertNotIn("worker", rendered.lower())
        self.assertNotIn(self.track.plugin.identifier, rendered)

    def test_numeric_display_targets_include_every_track(self):
        snapshot = build_flow_snapshot(self.host)
        console = Mock(width=80)
        with patch.object(flow_display, "_section_table") as table:
            for number in range(1, 17):
                with self.subTest(track=number):
                    flow_display.display_flow(console, snapshot, str(number))
                    self.assertEqual(table.call_args.args[0]["id"], f"track{number}")

    def test_upper_tracks_have_no_physical_midimix_bindings(self):
        self.engine.tracks[15] = copy.deepcopy(self.track)
        rows = self.rows("track16", details=True)
        self.assertFalse(any(row[0].startswith("MIDI Mix:") for row in rows))
        self.assertIn("16", next(row[1] for row in rows if row[0] == "MIDI channels"))

    def test_overview_keeps_problem_marker_when_source_names_are_long(self):
        self.track.plugin.name = "Long source " * 30
        self.track.worker_failure = "worker crashed " * 20
        overview = self.section("overview")
        self.assertEqual(overview["cell_states"][0][-1], "error")
        text = render_flow_snapshot({"title": "Cached", "sections": [overview]}, "overview")
        self.assertIn("worker failed", text)
        self.assertIn("blocked track", text)

    def test_deck_operational_state_is_compact_and_details_expand_reference(self):
        self.track.source_type = "deck"
        self.track.deck = {
            "track_id": "opening", "track_title": "Opening title",
            "requested_track_id": "next", "state": "loading", "play": False,
            "position": 12, "duration": 90, "cue": 4, "pitch": -1.25,
            "nudge": 0, "bpm": 123.5, "sync": True, "sync_active": False,
            "sync_error": "Waiting for global clock", "error": "Could not load next song",
            "beat_offset": 0.125, "beat_confidence": 0.9,
            "playback_speed": 1.02, "beat_phase_error": -0.01,
            "tracks": [{"id": "opening", "title": "Opening title", "path": "/music/opening.wav"}],
        }
        compact = self.rows("track1")
        self.assertTrue(any("Opening title" in cell for row in compact for cell in row))
        self.assertIn("deck paused", self.track_state())
        by_label = {row[0]: row for row in compact}
        self.assertEqual(by_label["Detected BPM"][1], "123.5 BPM")
        self.assertIn("loading", by_label["Deck transport"][2])
        self.assertEqual(by_label["Deck sync"][1], "requested true; active false")
        self.assertEqual(by_label["Deck sync_error"][1], "Waiting for global clock")
        self.assertEqual(by_label["Deck error"][1], "Could not load next song")
        self.assertNotIn("Deck requested_track_id", {row[0] for row in compact})
        detailed = {row[0]: row[1] for row in self.rows("track1", details=True)}
        self.assertEqual(detailed["Deck track_id"], "opening")
        self.assertEqual(detailed["Deck requested_track_id"], "next")
        self.assertEqual(detailed["Deck selection 1"], "Opening title")
        playlist = next(row for row in self.rows("track1", details=True) if row[0] == "Deck selection 1")
        self.assertIn("/music/opening.wav", playlist[2])
        for field, expected in (("beat_offset", "0.125"), ("beat_confidence", "0.9"),
                                ("playback_speed", "1.02"), ("beat_phase_error", "-0.01")):
            self.assertEqual(detailed[f"Deck {field}"], expected)
        self.assertFalse(any(row[0].startswith("Deck note") for row in self.rows("midi")))
        midi = {row[0]: row[1] for row in self.rows("midi", details=True)}
        self.assertEqual(midi["Deck note 63"], "nudge_down")
        self.assertEqual(midi["Deck note 64"], "nudge_up")
        self.assertIn("pitch", midi["Deck CC 20"])

    def test_deck_pitch_uses_applied_speed_in_compact_and_detailed_views(self):
        self.track.source_type = "deck"
        for sync, speed, expected in ((True, 1.25, "+25.00%"), (False, 0.98, "-2.00%"),
                                      (True, None, "not cached")):
            self.track.deck = {"pitch": -1.25, "sync": sync, "sync_active": sync}
            if speed is not None:
                self.track.deck["playback_speed"] = speed
            for details in (False, True):
                with self.subTest(sync=sync, speed=speed, details=details):
                    rows = {row[0]: row[1] for row in self.rows("track1", details=details)}
                    self.assertEqual(rows["Deck pitch"], "-1.25 %")
                    self.assertEqual(rows["Effective pitch"], expected)

    def test_deck_bpm_distinguishes_pending_and_unavailable_grid(self):
        self.track.source_type = "deck"
        for state, bpm, expected in (("loading", 0, "analyzing"), ("ready", 0, "unavailable"),
                                     ("ready", None, "unavailable"), ("ready", 128, "128 BPM")):
            with self.subTest(state=state, bpm=bpm):
                self.track.deck = {"state": state, "bpm": bpm}
                rows = {row[0]: row[1] for row in self.rows("track1")}
                self.assertEqual(rows["Detected BPM"], expected)

    def test_show_deck_reads_cached_state_and_retains_playlist_paths(self):
        self.track.source_type = "deck"
        self.track.deck = {
            "track_id": "first", "requested_track_id": "second",
            "track_title": "Opening", "bpm": 125, "play": True,
            "tracks": [{"id": "first", "path": "/music/opening.wav"}],
        }
        self.engine.request = Mock(side_effect=AssertionError("read must be cache-only"))
        output = io.StringIO()
        cli = HostCLI(self.host, stdout=output, owns_host=False)
        cli.onecmd("show 1")
        compact = output.getvalue()
        self.assertIn("125 BPM", compact)
        self.assertNotIn("/music/", compact)
        output.seek(0)
        output.truncate()
        cli.onecmd("show 1 --details")
        detailed = output.getvalue()
        self.assertIn("/music/opening.wav", detailed)
        self.assertIn("second", detailed)
        self.engine.request.assert_not_called()

    def test_output_eligibility_tracks_independent_blockers(self):
        self.assertEqual(self.track_state(), "eligible")
        for field, value, reason in (
            ("muted", True, "muted"), ("enabled", False, "disabled"),
            ("worker_failure", "worker crashed", "worker crashed"), ("gain", 0, "gain zero"),
        ):
            original = getattr(self.track, field)
            setattr(self.track, field, value)
            self.assertIn(reason, self.track_state())
            self.assertNotIn("eligible", self.track_state())
            setattr(self.track, field, original)
        self.engine.tracks[1] = InstrumentTrack("Solo", "", PluginSnapshot(3), solo=True, source_type="vcv")
        self.assertIn("solo-excluded", self.track_state())
        self.track.solo = True
        self.assertEqual(self.track_state(), "eligible")
        self.engine._failure = "owner crashed"
        self.assertIn("audio owner failed", self.track_state())
        self.assertEqual(self.section("track1")["cell_states"][0][-1], "error")

    def test_mapping_targets_survive_muting_and_controller_disconnect(self):
        self.track.muted = True
        connected = self.rows("controlsmidimix")
        cutoff = next(row for row in connected if row[0] == "Track 1 knob 1")
        self.assertEqual(cutoff[2], "Track 1 instrument: cutoff")
        self.assertEqual(cutoff[-1], "mapped")
        self.host.midimix.input_port_name = None
        disconnected = self.rows("controlsmidimix")
        cutoff = next(row for row in disconnected if row[0] == "Track 1 knob 1")
        self.assertEqual(cutoff[2], "Track 1 instrument: cutoff")
        self.assertEqual(cutoff[-1], "disconnected")
        self.assertFalse(any("effect_mix" in row[2] for row in disconnected))

    def test_track_contains_relevant_bindings_from_both_controllers(self):
        self.track.source_type = "deck"
        self.track.deck = {"play": True, "duration": 90, "position": 12,
                           "tracks": [{"id": "opening", "title": "Opening"}]}
        with patch.object(BeatStepProController, "input_port_name",
                          new_callable=PropertyMock, return_value="BeatStep"):
            rows = self.rows("track1")
        labels = [row[0] for row in rows]
        self.assertTrue(any(label.startswith("MIDI Mix:") for label in labels))
        self.assertTrue(any(label.startswith("BeatStep Pro:") for label in labels))
        self.assertTrue(any("play_toggle" in row[1] for row in rows))
        self.assertTrue(any("instrument: cutoff" in row[1] or "deck source: cutoff" in row[1]
                            for row in rows))
        self.assertFalse(any("Track 2" in row[0] for row in rows))

    def test_midi_shows_configured_empty_destinations_without_full_reference(self):
        rows = self.rows("midi")
        channel = next(row for row in rows if row[0] == "MIDI channel 16")
        bank = next(row for row in rows if row[0] == "Sequencer bank 1")
        self.assertIn("Track 16", channel[1])
        self.assertIn("empty", channel[2])
        self.assertIn("Track 16", bank[1])
        self.assertIn("empty", bank[2])
        self.assertEqual(sum(row[0].startswith("MIDI channel ") for row in rows), 2)
        self.assertEqual(next(row[1] for row in rows if row[0] == "Unused channels"), "14")
        self.assertNotIn("C4 (60)", bank[2])
        details = self.rows("midi", details=True)
        self.assertEqual(sum(row[0].startswith("MIDI channel ") for row in details), 16)
        self.assertEqual(sum(row[0].startswith("Sequencer bank ") for row in details), 16)
        self.assertIn("C4 (60)", next(row[2] for row in details if row[0] == "Sequencer bank 1"))

    def test_track_details_expand_metadata_and_worker_diagnostics(self):
        compact = self.rows("track1")
        labels = {row[0] for row in compact}
        self.assertTrue({"State", "Chain", "MIDI channels", "Sequencer banks", "Mixer", "Output"} <= labels)
        self.assertFalse({"Worker", "Worker buffering", "Path"} & labels)
        self.assertNotIn(self.track.plugin.identifier, str(compact))
        detailed = self.rows("track1", details=True)
        self.assertTrue({"Worker", "Worker buffering"} <= {row[0] for row in detailed})
        self.assert_plugin_metadata(detailed, self.track.plugin, (4, 3, 1, 2))

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

    def test_effect_views_exist_in_both_modes_and_details_expand_metadata(self):
        compact = {section["id"]: section for section in build_flow_snapshot(self.host)["sections"]}
        snapshot = build_flow_snapshot(self.host, details=True)
        detailed = {section["id"]: section for section in snapshot["sections"]}
        self.assertEqual(set(compact), set(detailed))
        for section_id, plugin, counts in (
                ("track1fx1", self.track.effects[0], (1, 1, 0, 0)),
                ("masterfx1", self.engine.master_effects[0], (1, 0, 1, 1))):
            with self.subTest(section=section_id):
                self.assertNotIn(plugin.identifier, str(compact[section_id]["rows"]))
                self.assert_plugin_metadata(detailed[section_id]["rows"], plugin, counts)
                self.assertIn(plugin.identifier, render_flow_snapshot(snapshot, section_id))
                self.assertIn(plugin.identifier, render_flow_snapshot(snapshot))

    def test_audio_is_output_configuration_not_repeated_track_chains(self):
        rows = self.rows("audio")
        self.assertTrue({"Output", "Backend", "Stream", "Problems"} <= {row[0] for row in rows})
        self.assertFalse(any(row[0].startswith("Track ") for row in rows))
        self.assertNotIn(self.track.plugin.name, str(rows))


    def test_snapshot_is_cached_only_and_does_not_mutate_plugins(self):
        forbidden = Mock(side_effect=AssertionError("flow requested live state or mutation"))
        self.engine.request = forbidden
        self.engine.set_parameter = forbidden
        original = copy.deepcopy((self.track, self.engine.master_effects))
        with patch("vcpi.midi.list_midi_input_ports", forbidden), \
                patch("vcpi.midi.list_midi_output_ports", forbidden):
            for details in (False, True):
                snapshot = build_flow_snapshot(self.host, details=details)
                render_flow_snapshot(snapshot)
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



if __name__ == '__main__':
    unittest.main()
