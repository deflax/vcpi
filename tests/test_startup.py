"""Startup/session behavior with real host state and hardware-free device edges."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.engine import AudioEngine
from core.host import VcpiCore
from core.link import LinkSync, validate_bpm
from core.main import _boot_host, _build_parser


class DeviceEngine(AudioEngine):
    """Keep real mixer state; replace only output hardware and process transport."""

    outputs = ("Built-in", "Studio Output")

    def __init__(self, sample_rate=44100, buffer_size=512):
        super().__init__(sample_rate, buffer_size)
        self.state_lock = self._lock
        self.audio_output_name = None
        self.listeners = []
        self.opened_outputs = []
        self.failure = None

    def add_state_listener(self, listener):
        self.listeners.append(listener)

    def remove_state_listener(self, listener):
        self.listeners.remove(listener)

    def sync_state(self):
        pass

    def request(self, operation, **arguments):
        if operation == "stop_audio":
            self._running = False
            return
        if operation != "start_audio":
            raise AssertionError(operation)
        selected = arguments["output_device"]
        if selected is None:
            selected = 0
        name = self.outputs[selected] if isinstance(selected, int) else selected
        if name not in self.outputs:
            raise ValueError("output unavailable")
        self.audio_output_name = name
        self.opened_outputs.append(name)
        self._running = True


class MidiHardware:
    names = ("Saved Keyboard", "Live Keyboard", "MIDI Mix")
    opened = []

    def get_port_count(self):
        return len(self.names)

    def get_port_name(self, index):
        return self.names[index]

    def open_port(self, index):
        self.opened.append(self.names[index])

    def set_callback(self, callback):
        self.callback = callback

    def send_message(self, data):
        pass

    def close_port(self):
        pass

    def delete(self):
        pass


class LinkHardware:
    created_tempos = []

    def __init__(self, bpm):
        self.tempo = bpm
        self.enabled = False
        self.num_peers = 0
        self.created_tempos.append(bpm)


class StartupTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "session.json"
        self.parser = _build_parser()
        self.enterContext(patch("core.host.RemoteAudioEngine", DeviceEngine))
        self.enterContext(patch("core.link.HAS_LINK", True))
        self.enterContext(patch("core.link.aalink", SimpleNamespace(Link=LinkHardware)))
        self.enterContext(patch("core.midi.HAS_RTMIDI", True))
        self.enterContext(patch("core.midi.rtmidi", SimpleNamespace(
            MidiIn=MidiHardware, MidiOut=MidiHardware)))
        LinkHardware.created_tempos = []
        MidiHardware.opened = []

    def session(self, **fields):
        self.path.write_text(json.dumps({"version": 3, **fields}))

    def boot(self, *options):
        args = self.parser.parse_args(["serve", "--session", str(self.path), *options])
        host = _boot_host(args)
        self.addCleanup(host.shutdown, save=False)
        return host

    def test_restore_preserves_setup_but_never_opens_audio(self):
        self.session(bpm=93, link_enabled=True, master_gain=0.45,
                     sample_rate=22050, buffer_size=64,
                     connections={"audio_output": "Studio Output"})
        host = self.boot()
        self.assertEqual(host.link.bpm, 93)
        self.assertTrue(host.link.enabled)
        self.assertEqual(host.engine.master_gain, 0.45)
        self.assertEqual((host.sample_rate, host.buffer_size), (44100, 512))
        self.assertEqual(host.preferred_output_device, "Studio Output")
        self.assertIsNone(host.audio_output_name)
        self.assertFalse(host.engine.running)
        self.assertEqual(host.engine.opened_outputs, [])
        host.start_audio()
        self.assertEqual(host.audio_output_name, "Studio Output")

    def test_explicit_settings_override_without_opening_saved_devices(self):
        self.session(bpm=93, link_enabled=True, connections={
            "audio_output": "Saved unavailable output", "midi_inputs": ["Saved Keyboard"],
            "midi_mix_in": "Saved Keyboard", "midi_mix_out": "Saved Keyboard"})
        host = self.boot("--bpm", "147", "--no-link", "--output", "1",
                         "--midi-input", "Live Keyboard", "--midi-input", "2",
                         "--midimix-input", "MIDI Mix", "--midimix-output", "2",
                         "--sample-rate", "48000", "--buffer-size", "256")
        self.assertEqual(host.link.bpm, 147)
        self.assertFalse(host.link.enabled)
        self.assertEqual(LinkHardware.created_tempos, [])
        self.assertEqual(host.midi_input_names, ["Live Keyboard", "MIDI Mix"])
        self.assertEqual(host.mixer_midi_name, "MIDI Mix")
        self.assertEqual(host.mixer_midi_out_name, "MIDI Mix")
        self.assertNotIn("Saved Keyboard", MidiHardware.opened)
        self.assertEqual(host.engine.opened_outputs, [])
        self.assertEqual((host.sample_rate, host.buffer_size), (48000, 256))
        host.start_audio()
        self.assertEqual(host.audio_output_name, "Studio Output")

    def test_omitted_midi_connections_restore_and_link_uses_explicit_tempo(self):
        self.session(bpm=83, link_enabled=True, connections={
            "midi_inputs": ["Saved Keyboard"], "midi_mix_in": "MIDI Mix",
            "midi_mix_out": "MIDI Mix"})
        host = self.boot("--bpm", "151")
        self.assertEqual(host.link.bpm, 151)
        self.assertEqual(host.midi_input_names, ["Saved Keyboard"])
        self.assertEqual(host.mixer_midi_name, "MIDI Mix")
        self.assertEqual(host.mixer_midi_out_name, "MIDI Mix")
        self.assertEqual(LinkHardware.created_tempos, [151])

    def test_no_restore_uses_defaults_and_explicit_flags(self):
        self.session(bpm=81, link_enabled=True, master_gain=0.1,
                     connections={"audio_output": "Studio Output"})
        host = self.boot("--no-restore")
        self.assertEqual(host.link.bpm, 120)
        self.assertFalse(host.link.enabled)
        self.assertIsNone(host.preferred_output_device)
        self.assertEqual(host.engine.opened_outputs, [])
        explicit = self.boot("--no-restore", "--bpm", "142", "--link")
        self.assertTrue(explicit.link.enabled)
        self.assertEqual(explicit.link.bpm, 142)

    def test_stopped_numeric_preference_round_trips_then_runtime_load_starts_audio(self):
        self.session(connections={"audio_output": 1})
        host = self.boot()
        host.save_session()
        self.assertEqual(json.loads(self.path.read_text())["connections"]["audio_output"], 1)
        restored = self.boot()
        self.assertFalse(restored.engine.running)
        restored.restore_session()
        self.assertTrue(restored.engine.running)
        self.assertEqual(restored.audio_output_name, "Studio Output")
        restored.stop_audio()
        restored.start_audio(0)
        restored.stop_audio()
        restored.start_audio()
        self.assertEqual(restored.audio_output_name, "Built-in")
        self.assertEqual(restored.preferred_output_device, "Built-in")
        with self.assertRaises(ValueError):
            restored.start_audio("unavailable")
        self.assertEqual(restored.preferred_output_device, "Built-in")
        restored.save_session()
        self.assertEqual(json.loads(self.path.read_text())["connections"]["audio_output"],
                         "Built-in")

    def test_runtime_load_disables_saved_link_and_retains_running_default_output(self):
        host = self.boot("--no-restore", "--link")
        host.start_audio(1)
        self.session(bpm=97, link_enabled=False)
        host.restore_session()
        self.assertFalse(host.link.enabled)
        self.assertEqual(host.link.bpm, 97)
        self.assertTrue(host.engine.running)
        self.assertEqual(host.audio_output_name, "Studio Output")

    def test_invalid_saved_tempo_rejected_before_mutating_live_state(self):
        host = self.boot("--no-restore")
        host.start_audio(1)
        for value in (0, -1, float("nan"), float("inf"), "bad", None):
            with self.subTest(value=value):
                self.session(bpm=value, master_gain=0.1)
                with self.assertRaises(ValueError):
                    host.restore_session()
                self.assertEqual(host.link.bpm, 120)
                self.assertTrue(host.engine.running)
        self.session(bpm=-10)
        override = self.boot("--bpm", "1000000")
        self.assertEqual(override.link.bpm, 1000000)

    def test_parser_repeated_ports_names_and_socket(self):
        args = self.parser.parse_args(["serve", "--midi-input", "0", "Live Keyboard",
                                       "--midi-input", "2", "--socket", "/tmp/live socket"])
        self.assertEqual(args.midi_input, ["0", "Live Keyboard", "2"])
        self.assertEqual(args.socket, "/tmp/live socket")
        self.assertIsNone(args.bpm)
        self.assertIsNone(args.link)
        cli = self.parser.parse_args(["cli", "--socket", "/tmp/live socket"])
        self.assertEqual(cli.socket, args.socket)

    def test_parser_rejects_invalid_values_old_spellings_and_abbreviations(self):
        invalid = [
            ["--sample-rate", "0"], ["--sample-rate", "1.5"], ["--buffer-size", "-1"],
            ["--midi-input"], ["--midi-input", ""], ["--midimix-input", " "],
            ["--midimix-output", ""], ["--output", ""], ["--link", "--no-link"],
            ["--sr", "44100"], ["--buf", "512"], ["--sock", "/tmp/sock"],
            ["--midi-in", "0"], ["--mix-midi", "0"], ["--mix-midi-out", "0"],
        ]
        invalid += [["--bpm", value] for value in ("0", "-1", "nan", "inf", "bad")]
        for options in invalid:
            with self.subTest(options=options), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    self.parser.parse_args(["serve", *options])
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.parser.parse_args(["cli", "--sock", "/tmp/sock"])


class TempoValidationTests(unittest.TestCase):
    def test_constructor_setter_and_enable_reject_invalid_tempos(self):
        for value in (0, -1, float("nan"), float("inf"), -float("inf"), "bad", None):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    validate_bpm(value)
                with self.assertRaises(ValueError):
                    LinkSync(value)
                link = LinkSync(90)
                with self.assertRaises(ValueError):
                    link.bpm = value
                self.assertEqual(link.bpm, 90)
                link._bpm = value
                with self.assertRaises(ValueError):
                    link.enable()
                self.assertFalse(link.enabled)
                self.assertIsNone(link._loop)

    def test_positive_finite_tempos_have_no_arbitrary_upper_bound(self):
        link = LinkSync("0.001")
        self.assertEqual(link.bpm, 0.001)
        link.bpm = "1000000"
        self.assertEqual(link.bpm, 1000000.0)
