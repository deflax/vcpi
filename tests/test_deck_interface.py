"""Cached CLI/completion and actual routed MIDI bytes for native deck controls."""

import base64
from dataclasses import replace
import io
from threading import RLock
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from controllers.akai_midimix import KNOB_CCS, MUTE_NOTES, MidiMixController
from controllers.midi_input import MidiInputController
from core.audio_state import ParameterSnapshot, PluginSnapshot
from core.cli import HostCLI
from core.completion import complete_line
from core.deps import HAS_MIDO
from core.models import InstrumentTrack
from core.session import snapshot


def deck_track(generation=7):
    plugin = PluginSnapshot(1, name="Deck", parameters={
        "track_select": ParameterSnapshot(kind="int", range=(1, 257)),
        "play": ParameterSnapshot(kind="bool", is_boolean=True),
        "pitch": ParameterSnapshot(range=(-16, 16), units="%"),
        "nudge": ParameterSnapshot(range=(-1, 1)),
    }, values={"track_select": 1, "play": False, "pitch": 0.0, "nudge": 0.0})
    track = InstrumentTrack("Deck A", "/bank.json", plugin,
                            source_type="deck", generation=generation)
    track.deck = {
        "bank_path": "/bank.json", "bank_id": "set", "track_id": "first",
        "track_title": "Opening title", "requested_track_id": "second",
        "request_id": 4, "state": "loading", "play": False,
        "position": 12.5, "duration": 100, "cue": 2.0, "pitch": -1.25,
        "nudge": 0, "error": "",
        "tracks": [{"id": str(i), "title": f"Title {i}"} for i in range(257)],
    }
    return track


class DeckCLITests(unittest.TestCase):
    def setUp(self):
        self.track = deck_track()
        self.engine = SimpleNamespace(
            tracks=[self.track] + [None] * 7, master_effects=[],
            deck_command=Mock(), set_parameter=Mock(return_value=2),
            request=Mock(side_effect=AssertionError("read must be cache-only")))
        self.host = SimpleNamespace(engine=self.engine, load_deck=Mock(return_value=self.track))
        self.output = io.StringIO()
        self.cli = HostCLI(self.host, stdout=self.output, owns_host=False)

    def test_creation_quoted_path_and_explicit_transport_grammar(self):
        self.cli.onecmd('track 2 deck "/music/live set.json" Deck B')
        self.host.load_deck.assert_called_once_with(1, "/music/live set.json", "Deck B")
        for command, action, value in (("seek 90", "seek", 90.0),
                                       ("cue set", "cue_set", None),
                                       ("cue", "cue", None)):
            self.cli.onecmd("track 1 deck " + command)
            args = (0, action) if value is None else (0, action, value)
            self.engine.deck_command.assert_called_with(*args)
        self.engine.deck_command.reset_mock()
        for invalid in ("cue extra", "status extra", "seek", "seek xx", "seek 1 2"):
            self.cli.onecmd("track 1 deck " + invalid)
        self.engine.deck_command.assert_not_called()
        self.assertIn("Error:", self.output.getvalue())

    def test_get_status_and_completion_never_request_live_state(self):
        self.cli.onecmd("track 1 deck status")
        self.cli.onecmd("get 1")
        text = self.output.getvalue()
        self.assertIn("track_id = first", text)
        self.assertIn("requested_track_id = second", text)
        self.assertIn("Opening title", text)
        self.assertIn("257: 256", text)
        self.assertIn("deck", complete_line(self.cli, "track 1 ")["matches"])
        self.assertEqual(complete_line(self.cli, "track 1 deck ")["matches"],
                         ["cue", "seek", "status"])
        self.assertEqual(complete_line(self.cli, "track 1 deck cue ")["matches"], ["set"])
        self.assertEqual(complete_line(self.cli, "set 1 play ")["matches"], ["false", "true"])
        self.assertIn("257", complete_line(self.cli, "set 1 track_select ")["matches"])
        self.engine.request.assert_not_called()
        self.engine.deck_command.assert_not_called()
        self.host.load_deck.assert_not_called()

    def test_selection_acknowledges_request_not_applied_track(self):
        self.cli.onecmd("set 1 track_select 2")
        self.engine.set_parameter.assert_called_once_with(0, "track_select", 2.0, fx_index=None)
        self.assertIn("request accepted", self.output.getvalue())
        self.assertEqual(self.track.deck["track_id"], "first")

    def test_deck_payload_omits_source_chunk_but_keeps_insert_chunks(self):
        self.track.effects = [PluginSnapshot(
            2, path_to_plugin_file="/delay.vst3", native_state=b"effect-state")]
        self.engine.state_lock = RLock()
        self.engine.master_gain = 1.0
        self.host.channel_map = {0: 0}
        self.host.audio_output_name = None
        self.host.midi_input_names = []
        self.host.mixer_midi_name = None
        self.host.mixer_midi_out_name = None
        self.host.sample_rate = 44100
        self.host.buffer_size = 256
        self.host.link = SimpleNamespace(bpm=120, enabled=False)
        self.host.sequencer = SimpleNamespace(snapshot=lambda: [])
        saved = snapshot(self.host)["tracks"][0]
        self.assertNotIn("native_state", saved)
        self.assertEqual(saved["deck"], {
            "bank_path": "/bank.json", "bank_id": "set", "track_id": "first",
            "cue": 2.0, "pitch": -1.25})
        self.assertEqual(base64.b64decode(saved["effects"][0]["native_state"]), b"effect-state")


class RecordingDeckEngine:
    def __init__(self):
        self.tracks = [deck_track(), deck_track(8)] + [None] * 6
        self.channel_map = {0: 0, 1: 1}
        self.commands = []
        self.midi = []

    def enqueue_param_change(self, index, name, value, *, generation):
        if self.tracks[index].generation == generation:
            self.commands.append((index, name, value, generation))

    def enqueue_deck_command(self, index, action, value=0, *, generation):
        self.commands.append((index, action, value, generation))

    def enqueue_midi(self, index, message):
        self.midi.append((index, message))

    def enqueue_track_change(self, index, **fields):
        self.commands.append((index, "track", fields))


@unittest.skipUnless(HAS_MIDO, "mido required for raw MIDI dispatch")
class DeckMidiTests(unittest.TestCase):
    def setUp(self):
        self.engine = RecordingDeckEngine()
        self.controller = MidiInputController(self.engine)
        self.addCleanup(self.controller.close)

    def send(self, status, data1, data2):
        self.controller.on_midi(([status, data1, data2], 0.0))

    def test_channel_filter_release_and_velocity_zero_isolate_decks(self):
        self.send(0x92, 64, 127)  # unrouted channel
        self.assertEqual(self.engine.commands, [])
        self.send(0x90, 64, 127)
        self.send(0x91, 63, 127)
        self.send(0x80, 64, 64)
        self.send(0x91, 63, 0)
        self.assertEqual(self.engine.commands, [
            (0, "nudge", 1.0, 7), (1, "nudge", -1.0, 8),
            (0, "nudge", 0.0, 7), (1, "nudge", 0.0, 8)])
        self.assertEqual(self.engine.midi, [])

    def test_opposite_holds_and_edge_triggered_transport(self):
        for raw in ((0x90, 63, 1), (0x90, 64, 1), (0x80, 63, 0), (0x80, 64, 0)):
            self.send(*raw)
        self.assertEqual([command[2] for command in self.engine.commands], [-1, 0, 1, 0])
        self.engine.commands.clear()
        for note, action in ((60, "play_toggle"), (61, "cue"), (62, "cue_set")):
            self.send(0x90, note, 127)
            self.send(0x90, note, 127)  # repeated press is not another toggle
            self.send(0x90, note, 0)
        self.assertEqual(self.engine.commands, [(0, action, 0, 7)
                                               for action in ("play_toggle", "cue", "cue_set")])

    def test_pitch_selection_and_non_deck_forwarding(self):
        for cc in (20, 21):
            self.send(0xB0, cc, 0)
            self.send(0xB0, cc, 127)
        self.assertEqual(self.engine.commands, [
            (0, "pitch", -16, 7), (0, "pitch", 16, 7),
            (0, "track_select", 1, 7), (0, "track_select", 257, 7)])
        self.engine.tracks[1] = replace(self.engine.tracks[1], source_type="wav")
        self.send(0x91, 64, 127)
        self.send(0x91, 64, 0)
        self.assertEqual([msg.type for _, msg in self.engine.midi], ["note_on", "note_off"])
        self.assertTrue(all(index == 1 for index, _ in self.engine.midi))

    def test_disconnect_route_removal_and_replacement_clear_holds(self):
        self.send(0x90, 64, 127)
        self.controller.close()
        self.assertEqual(self.engine.commands[-1], (0, "nudge", 0, 7))
        self.send(0x90, 63, 127)
        self.controller.clear_deck_holds(channel=0)
        self.engine.channel_map.pop(0)
        self.assertEqual(self.engine.commands[-1], (0, "nudge", 0, 7))
        self.engine.channel_map[0] = 0
        self.send(0x90, 64, 127)
        self.controller.clear_deck_holds(track_index=0)
        self.engine.tracks[0] = deck_track(9)
        self.send(0x90, 63, 127)
        self.assertEqual(self.engine.commands[-1], (0, "nudge", -1, 9))

    def test_midimix_integer_selection_and_mute_are_not_nudge(self):
        controller = MidiMixController(self.engine)
        self.addCleanup(controller.close)
        controller.on_midi(([0xB0, KNOB_CCS[0][0], 64], 0.0))
        self.assertIsInstance(self.engine.commands[-1][2], int)
        self.assertEqual(self.engine.commands[-1][1], "track_select")
        controller.on_midi(([0x90, MUTE_NOTES[0], 127], 0.0))
        self.assertEqual(self.engine.commands[-1][1], "track")
        count = len(self.engine.commands)
        controller.on_midi(([0x80, MUTE_NOTES[0], 0], 0.0))
        self.assertEqual(len(self.engine.commands), count)
        self.assertFalse(any(command[1] == "nudge" for command in self.engine.commands))
