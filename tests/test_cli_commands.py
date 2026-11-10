"""Interactive parsing, rejection before mutation, and state transitions."""

import io
from pathlib import Path
import shlex
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from vcpi.commands.cli import HostCLI
from vcpi.commands.context import parse_named_source, source_args
from vcpi.link import LinkSync
from vcpi.models import NUM_TRACKS
from vcpi.timeline import MusicalTimeline


class MixerState:
    def __init__(self):
        self.tracks = [SimpleNamespace(name="Bass", gain=0.5, muted=False, solo=False)] + [None] * (NUM_TRACKS - 1)
        self.master_gain = 0.75

    def set_track_state(self, index, **fields):
        track = self.tracks[index]
        for name, value in fields.items():
            setattr(track, name, value)
        return track

    def set_master_gain(self, value):
        self.master_gain = value


class CLICommandTests(unittest.TestCase):
    def setUp(self):
        self.engine = MixerState()
        self.notes = []
        self.host = SimpleNamespace(
            engine=self.engine, link=LinkSync(), midi_inputs=[],
            refresh_mixer_leds=lambda indexes: None,
            send_note=lambda *event: self.notes.append(event))
        self.host.timeline = MusicalTimeline(self.host.link)
        self.output = io.StringIO()
        self.cli = HostCLI(self.host, stdout=self.output, owns_host=False)

    def execute(self, command):
        self.output.seek(0)
        self.output.truncate()
        result = self.cli.onecmd(command)
        return result, self.output.getvalue()

    def test_gain_reads_and_sets_both_target_kinds(self):
        for command, expected in (("gain 1", 0.5), ("gain 1 0.2", 0.2)):
            self.execute(command)
            self.assertEqual(self.engine.tracks[0].gain, expected)
        for command, expected in (("gain master", 0.75), ("gain master 0.4", 0.4)):
            self.execute(command)
            self.assertEqual(self.engine.master_gain, expected)
        for value in ("-0.1", "1.1", "nan", "inf"):
            self.execute("gain 1 " + value)
            self.execute("gain master " + value)
            self.assertEqual(self.engine.tracks[0].gain, 0.2)
            self.assertEqual(self.engine.master_gain, 0.4)

    def test_explicit_switches_are_idempotent_and_bare_switch_toggles(self):
        for command, field in (("mute", "muted"), ("solo", "solo")):
            for suffix, expected in ((" on", True), (" on", True), (" off", False),
                                     (" off", False), (" toggle", True), ("", False)):
                self.execute(command + " 1" + suffix)
                self.assertEqual(getattr(self.engine.tracks[0], field), expected)
            self.execute(command + " 1 yes")
            self.assertFalse(getattr(self.engine.tracks[0], field))

    def test_last_track_mixer_commands_do_not_change_first_track(self):
        self.engine.tracks[15] = SimpleNamespace(name="Last", gain=0.5, muted=False, solo=False)
        self.execute("gain 16 0.25")
        self.execute("mute 16 on")
        self.execute("solo 16 on")
        self.assertEqual(vars(self.engine.tracks[15]),
                         dict(name="Last", gain=0.25, muted=True, solo=True))
        self.assertEqual(vars(self.engine.tracks[0]),
                         dict(name="Bass", gain=0.5, muted=False, solo=False))
        for command in ("gain 17 0.5", "mute 17 on", "solo 17 on"):
            with self.subTest(command=command):
                _, text = self.execute(command)
                self.assertIn("Error:", text)
        self.assertEqual(self.engine.tracks[15].gain, 0.25)

    def test_note_options_defaults_units_and_rejection(self):
        self.execute("note 1 60")
        self.execute("note 16 127 --duration-ms 125.5 --velocity 0")
        self.assertEqual(self.notes, [(0, 60, 100, 0.3), (15, 127, 0, 0.1255)])
        self.notes.clear()
        for args in ("1 60 100 300", "1 -1", "1 128", "0 60", "17 60", "1 60.5",
                     "1 60 --velocity -1", "1 60 --velocity 128", "1 60 --velocity 1.5",
                     "1 60 --duration-ms 0", "1 60 --duration-ms -1",
                     "1 60 --duration-ms nan", "1 60 --duration-ms inf",
                     "1 60 --duration-ms -inf", "1 60 --duration-ms 1e999",
                     "1 60 --velocity", "1 60 --duration 20",
                     "1 60 --velocity 1 --velocity 2", "1 60 --duration-ms 1 extra"):
            with self.subTest(args=args):
                _, text = self.execute("note " + args)
                self.assertIn("Error:", text)
                self.assertEqual(self.notes, [])

    def test_tempo_rejects_nonfinite_and_nonpositive_without_changing_state(self):
        self.execute("tempo 240.5")
        self.assertEqual(self.host.timeline.bpm, 240.5)
        for value in ("0", "-1", "nan", "inf", "-inf", "1e999", "not-a-number"):
            for command in ("tempo ", "ableton link "):
                with self.subTest(command=command, value=value):
                    _, text = self.execute(command + value)
                    self.assertIn("Error:", text)
                    self.assertEqual(self.host.timeline.bpm, 240.5)
        self.execute("tempo 100000")
        self.assertEqual(self.host.timeline.bpm, 100000)

    def test_fixed_arity_commands_reject_trailing_words_before_host_access(self):
        commands = (
            "about extra", "status extra", "deps extra", "quit extra", "exit extra",
            "shutdown extra", "audio stop extra", "audio devices extra", "audio diagnose extra",
            "audio start 1 extra", "midi ports input extra", "midi ports output extra",
            "midi input 0 extra", "midi input close 1 extra", "midi link 1 1 extra",
            "midi cut 1 extra", "midimix input 0 extra", "midimix output 0 extra",
            "seq link 1 1 extra", "seq cut 1 extra", "seq clear 1 extra",
            "track 1 clear extra", "track 1 fx clear 1 extra", "track master fx clear 1 extra",
            "ableton cut extra", "ableton link 120 extra", "tempo 120 extra",
            "gain 1 .5 extra", "gain master .5 extra", "mute 1 on extra", "solo 1 off extra",
            "debug on extra", "save Live Set", "load Live Set", "ps all", "ps --cpu --mem",
            "ps --all --all", "ps --unknown")
        for command in commands:
            with self.subTest(command=command):
                result, text = self.execute(command)
                self.assertIsNone(result)
                self.assertIn("Usage:", text)
                self.assertFalse(self.cli._shutdown_requested)

    def test_vst_source_commands_are_rejected_without_host_calls(self):
        host = Mock()
        self.cli.host = host
        for target in ("1", "16", "master"):
            for source in ("vst", "vst Basic", 'vst "Surge XT" --name Bass',
                           "VST /plugins/Instrument.vst3"):
                with self.subTest(target=target, source=source):
                    result, text = self.execute(f"track {target} {source}")
                    self.assertIsNone(result)
                    self.assertIn("Error:", text)
                    self.assertEqual(host.mock_calls, [])

    def test_removed_vst_help_topic_is_unavailable(self):
        _, text = self.execute("help track vst")
        self.assertIn("No help for", text)
        self.assertNotIn("Usage:", text)

    def test_track_and_master_effects_resolve_multiword_plugin_names(self):
        self.host.load_effect = Mock()
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory) / "Room Reverb.vst3"
            bundle.mkdir()
            with patch("vcpi.commands.assets.vst_search_dirs", return_value=[Path(directory)]):
                for target, index in (("1", 0), ("16", 15), ("master", None)):
                    for source in ("Room Reverb", '"Room Reverb"', shlex.quote(str(bundle))):
                        for suffix, name in (("", None), (' --name "Live Room"', "Live Room")):
                            with self.subTest(target=target, source=source, name=name):
                                _, text = self.execute(f"track {target} fx {source}{suffix}")
                                self.assertNotIn("Error:", text)
                                self.host.load_effect.assert_called_once_with(str(bundle), index, name)
                                self.host.load_effect.reset_mock()

    def test_malformed_quotes_are_errors_not_exceptions(self):
        for command in ("track", "gain", "mute", "solo", "audio", "midi", "midimix",
                        "note", "seq", "ableton", "tempo", "save", "load", "ps", "help"):
            _, text = self.execute(command + ' "unterminated')
            self.assertIn("Error:", text)

    def test_source_labels_are_explicit_single_shell_arguments(self):
        for source, count in (("'/music/song.flac'", 1), ("'VCV patch'", 1)):
            tokens, label = source_args(shlex.split(source + ' --name "Live Voice"'), count)
            self.assertEqual(tokens, shlex.split(source))
            self.assertEqual(label, "Live Voice")
            for suffix in (' "Legacy Label"', " --name", " --name a b", " --other x",
                           " --name a --name b"):
                with self.assertRaises(ValueError):
                    source_args(shlex.split(source + suffix), count)
        for text in ("Surge XT", '"Surge XT"'):
            self.assertEqual(parse_named_source(text + ' --name "Live Voice"'),
                             ("Surge XT", "Live Voice"))

    def test_quoted_session_name_is_saved_without_quote_characters(self):
        with tempfile.TemporaryDirectory() as directory, patch(
                "vcpi.commands.assets.sessions_root", side_effect=lambda: Path(directory)):
            self.host.loaded_session_path = None
            self.host.save_session = lambda path: Path(path).write_text("session payload")
            self.execute('save "Live Set"')
            self.assertEqual((Path(directory) / "Live Set.json").read_text(), "session payload")
            self.execute('save "Live Set" extra')
            self.assertEqual([p.name for p in Path(directory).iterdir()], ["Live Set.json"])



if __name__ == "__main__":
    unittest.main()
