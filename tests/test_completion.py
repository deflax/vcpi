"""Shared completion choices, insertion spans, dynamic snapshots, and safety."""

import copy
import io
from pathlib import Path
import shlex
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from core.audio_state import ParameterSnapshot, PluginSnapshot
from core.cli import HostCLI
from core.completion import complete_line
from core.models import NUM_SLOTS
from core.sequencer import NUM_SEQ_BANKS


class CompletionTests(unittest.TestCase):
    def setUp(self):
        self.instrument = PluginSnapshot(1, parameters={
            "cutoff": ParameterSnapshot(kind="float"),
            "bypass": ParameterSnapshot(kind="bool"),
            "mode": ParameterSnapshot(kind="enum", valid_values=(
                "Quiet Mode", "Loud Mode", "Player's Choice", 'Say "hello"')),
            "display mode": ParameterSnapshot(kind="bool"),
            "midi_cc_0_1": ParameterSnapshot(label="MIDI CC 0|1"),
        }, values={"cutoff": 1000.0, "bypass": False, "mode": "Quiet Mode"})
        self.effect = PluginSnapshot(2, parameters={"mix": ParameterSnapshot()})
        self.master = PluginSnapshot(3, parameters={"enabled": ParameterSnapshot(kind="bool")})
        self.slot = SimpleNamespace(plugin=self.instrument, effects=[self.effect])
        self.forbidden = Mock(side_effect=AssertionError("completion requested live state or mutation"))
        self.engine = SimpleNamespace(
            slots=[self.slot] + [None] * (NUM_SLOTS - 1),
            master_effects=[self.master, self.effect],
            audio_devices=[
                {"index": 0, "name": "Input", "max_output_channels": 0},
                {"index": 3, "name": "Output With Spaces", "max_output_channels": 2},
            ], request=self.forbidden, set_parameter=self.forbidden,
            start=self.forbidden, stop=self.forbidden,
        )
        self.host = SimpleNamespace(
            engine=self.engine, midi_inputs=[object(), object()],
            start_audio=self.forbidden, stop_audio=self.forbidden,
            load_instrument=self.forbidden, load_effect=self.forbidden,
            open_midi_input=self.forbidden, close_midi_input=self.forbidden,
        )
        self.cli = HostCLI(self.host, stdout=io.StringIO(), owns_host=False)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        for name in ("vst", "samples", "patches", "sessions"):
            (root / name).mkdir()
        for name in ("Surge XT", "Basic", "Player's Synth"):
            (root / "vst" / f"{name}.vst3").mkdir()
        for pack in ("Drums", "Bad Pack"):
            (root / "samples" / pack).mkdir()
        for name in ("Kick", "Closed Hat"):
            (root / "samples" / "Drums" / f"{name}.wav").touch()
        for name in ("Bass", "Bad Patch"):
            (root / "patches" / f"{name}.vcv").touch()
        for name in ("Live Set", "Studio"):
            (root / "sessions" / f"{name}.json").write_text("{}")
        self.root = root
        for method, value in (
                ("_vst_search_dirs", [root / "vst"]),
                ("_samples_root", root / "samples"),
                ("_patches_root", root / "patches"),
                ("_sessions_root", root / "sessions")):
            patcher = patch.object(self.cli, method, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def matches(self, before):
        result = complete_line(self.cli, before)
        self.assertEqual(set(result), {"start_position", "matches"})
        self.assertIsInstance(result["start_position"], int)
        self.assertLessEqual(result["start_position"], 0)
        self.assertGreaterEqual(result["start_position"], -len(before))
        for value in result["matches"]:
            self.assertFalse(value.endswith(" "))
            self.assertFalse(any(ord(char) < 32 or ord(char) == 127 for char in value))
        return result["matches"]

    def inserted(self, before, match):
        result = complete_line(self.cli, before)
        self.assertIn(match, result["matches"])
        return before[:len(before) + result["start_position"]] + match

    def test_root_help_and_whitespace_boundaries(self):
        self.assertIn("status", self.matches(""))
        self.assertEqual(self.matches("sta"), ["status"])
        self.assertEqual(self.inserted("  sta", "status"), "  status")
        self.assertIn("slot", self.matches("help "))
        self.assertEqual(self.matches("help aud"), ["audio"])
        self.assertIn("master", self.matches("  gain\t"))
        for before in ("unknown ", "status ", "help audio ", "flow ", "about ",
                       "audio stop ", "audio devices ", "audio diagnose ",
                       "debug on ", "mute 1 ", "solo 1 ", "note 1 ",
                       "gain master ", "tempo ", "ableton cut ", "ableton link "):
            with self.subTest(before=before):
                self.assertEqual(self.matches(before), [])

    def test_finite_command_branches(self):
        slots = {str(i) for i in range(1, NUM_SLOTS + 1)}
        channels = {str(i) for i in range(1, 17)}
        banks = {str(i) for i in range(1, NUM_SEQ_BANKS + 1)}
        for before in ("mute ", "solo ", "note ", "midi link 16 ",
                       "seq link 16 ", "seq cut "):
            self.assertEqual(set(self.matches(before)), slots, before)
        for before in ("gain ", "slot ", "info ", "get ", "set "):
            self.assertEqual(set(self.matches(before)), slots | {"master"}, before)
        for before in ("midi link ", "midi cut "):
            self.assertEqual(set(self.matches(before)), channels)
        for before in ("seq link ", "seq clear "):
            self.assertEqual(set(self.matches(before)), banks)
        self.assertEqual(set(self.matches("seq ")), banks | {"link", "cut", "clear"})
        for before, expected in (
                ("audio ", {"start", "stop", "devices", "diagnose"}),
                ("midi ", {"ports", "input", "link", "cut"}),
                ("midi ports ", {"input", "output"}),
                ("midimix ", {"input", "output"}),
                ("ableton ", {"link", "cut"}),
                ("debug ", {"on", "off"}),
                ("slot master ", {"fx"}),
                ("slot 8 ", {"vst", "wav", "vcv", "fx", "clear"})):
            self.assertEqual(set(self.matches(before)), expected)
        for before in ("midi link 17 ", "midi link 0 ", "seq link 0 ",
                       "seq link 17 ", "slot 9 ", "slot master clear ",
                       "slot 1 clear ", "get 0 ", "set 9 ", "info 1 nonsense "):
            self.assertEqual(self.matches(before), [], before)

    def test_audio_output_indexes_follow_current_snapshot(self):
        self.assertEqual(self.matches("audio start "), ["3"])
        self.engine.audio_devices[:] = [
            {"index": 7, "name": "New output", "max_output_channels": 4}]
        self.assertEqual(self.matches("audio start "), ["7"])
        self.assertEqual(self.matches("audio start 3"), [])
        self.assertEqual(self.matches("audio start 7 "), [])

    def test_hardware_ports_and_open_input_indexes_are_distinct_and_current(self):
        with patch("core.midi.list_midi_input_ports", return_value=["Input A", "Input B"]) as inputs, \
                patch("core.midi.list_midi_output_ports", return_value=["Output"]) as outputs:
            self.assertEqual(set(self.matches("midi input ")), {"0", "1", "close"})
            self.assertEqual(self.matches("midimix input "), ["0", "1"])
            self.assertEqual(self.matches("midimix output "), ["0"])
            inputs.return_value = ["Replacement"]
            self.assertEqual(self.matches("midimix input "), ["0"])
            self.assertEqual(self.matches("midi input close "), ["1", "2"])
            self.host.midi_inputs.pop()
            self.assertEqual(self.matches("midi input close "), ["1"])
            inputs.reset_mock()
            outputs.reset_mock()
            for before in ("midi input c", "midi input close ", "midi input bogus",
                           "midi input 0 ", "midimix bad ", "midimix output abc",
                           "midi ports ", "midi ports input "):
                self.matches(before)
            inputs.assert_not_called()
            outputs.assert_not_called()

    def test_unavailable_hardware_keeps_non_hardware_midi_choices(self):
        with patch("core.midi.list_midi_input_ports", side_effect=RuntimeError("no sequencer")), \
                patch("core.midi.list_midi_output_ports", side_effect=RuntimeError("no sequencer")):
            self.assertEqual(self.matches("midi input "), ["close"])
            self.assertEqual(self.matches("midi input c"), ["close"])
            self.assertEqual(self.matches("midi input 0"), [])
            self.assertEqual(self.matches("midimix input "), [])
            self.assertEqual(self.matches("midimix output "), [])
            self.assertEqual(self.matches("midi input close "), ["1", "2"])
            self.assertEqual(self.matches("midi ports "), ["input", "output"])

    def test_effect_indexes_follow_loaded_chains_only(self):
        for before in ("info master ", "get master 2", "set master ", "slot master fx clear "):
            expected = ["2"] if before.endswith("2") else ["1", "2"]
            self.assertEqual(self.matches(before), expected)
        for before in ("info 1 fx ", "get 1 fx ", "set 1 fx ", "slot 1 fx clear "):
            self.assertEqual(self.matches(before), ["1"])
        self.slot.effects.clear()
        self.engine.master_effects.pop()
        self.assertEqual(self.matches("info 1 "), [])
        self.assertEqual(self.matches("info 1 fx "), [])
        self.assertEqual(self.matches("slot 1 fx clear "), [])
        self.assertEqual(self.matches("set master "), ["1"])
        for before in ("info 1 fx 1 ", "set 1 fx 1 ", "get master 2 "):
            self.assertEqual(self.matches(before), [])

    def test_parameters_and_finite_values_use_cached_metadata(self):
        self.assertIn("fx", self.matches("get 1 "))
        self.assertIn("mode", self.matches("set 1 "))
        self.assertNotIn("midi_cc_0_1", self.matches("get 1 "))
        self.assertEqual(self.matches("get 1 fx 1 "), ["mix"])
        self.assertIn("enabled", self.matches("get master "))
        self.assertEqual(self.matches("get master 2 "), ["mix"])
        self.assertEqual(self.matches("set 1 bypass "), ["false", "true"])
        self.assertEqual(self.matches("set master 1 enabled t"), ["true"])
        for before in ("set 1 cutoff ", "set 1 absent ", "set 1 midi_cc_0_1 ",
                       "get 1 mode ", "set 1 bypass true ", "set master enabled "):
            self.assertEqual(self.matches(before), [])
        self.instrument.parameters["mode"].valid_values = ("New Choice",)
        self.assertEqual(self.matches("set 1 mode "), ["'New Choice'"])
        del self.instrument.parameters["mode"]
        self.assertNotIn("mode", self.matches("set 1 "))

    def test_reserved_parameter_names_only_appear_at_unambiguous_positions(self):
        for name in ("0", "99", "+2", "--1"):
            self.master.parameters[name] = ParameterSnapshot(kind="bool")
            self.assertNotIn(name, self.matches("get master "))
            inserted = self.inserted("get master 1 ", name)
            _, _, plugin, remaining = self.cli._parameter_target(shlex.split(inserted)[1:])
            self.assertIs(plugin, self.master)
            self.assertEqual(remaining, [name])
            self.assertIn(name, self.matches("set master 1 "))
        self.instrument.parameters["fx"] = ParameterSnapshot(kind="bool")
        self.assertIn("fx", self.matches("get 1 "))
        self.assertEqual(self.matches("get 1 fx "), ["1"])
        self.slot.effects.clear()
        self.assertNotIn("fx", self.matches("get 1 "))
        self.assertNotIn("fx", self.matches("set 1 "))
        self.effect.parameters["fx"] = ParameterSnapshot(kind="bool")
        self.slot.effects.append(self.effect)
        inserted = self.inserted("get 1 fx 1 ", "fx")
        _, _, plugin, remaining = self.cli._parameter_target(shlex.split(inserted)[1:])
        self.assertIs(plugin, self.effect)
        self.assertEqual(remaining, ["fx"])

    def test_quoted_names_and_enum_insertions_parse_as_one_argument(self):
        for before, value in (("set 1 mode Qu", "Quiet Mode"),
                              ('set 1 mode "Qu', "Quiet Mode"),
                              ("set 1 mode 'Qu", "Quiet Mode"),
                              ("set 1 mode Player", "Player's Choice"),
                              ('set 1 mode "Say ', 'Say "hello"')):
            match = shlex.quote(value)
            inserted = self.inserted(before, match)
            self.assertEqual(shlex.split(inserted), ["set", "1", "mode", value])
        match = shlex.quote("display mode")
        inserted = self.inserted('set 1 "display', match)
        self.assertEqual(shlex.split(inserted), ["set", "1", "display mode"])
        self.assertEqual(self.matches(inserted + " "), ["false", "true"])
        self.assertEqual(self.matches("set 1 mode \\"), [])
        self.assertEqual(self.matches('set 1 "unterminated name'), [])

    def test_multiword_plugin_replaces_whole_fragment_and_supports_name_option(self):
        for before in ("slot 1 vst Surge X", 'slot 1 vst "Surge X',
                       "slot master fx Surge X", "slot 1 fx Surge X"):
            inserted = self.inserted(before, "'Surge XT'")
            plugin_arg = inserted.split(maxsplit=3)[3]
            self.assertEqual(self.cli._parse_vst_args(plugin_arg), ("Surge XT", None))
        self.assertEqual(self.matches("slot 1 vst Basic "), ["--name"])
        self.assertEqual(self.matches("slot 1 vst Basic --n"), ["--name"])
        self.assertEqual(self.matches("slot 1 vst Basic --name "), [])
        self.assertEqual(self.matches("slot 1 fx clear "), ["1"])
        (self.root / "vst" / "Fresh.vst3").mkdir()
        self.assertEqual(self.matches("slot 1 vst Fre"), ["Fresh"])

    def test_readline_bridge_replaces_multiword_plugin_fragment(self):
        before = "slot 1 vst Surge X"
        with patch("readline.get_line_buffer", return_value=before), \
                patch("readline.get_endidx", return_value=len(before)), \
                patch("readline.get_begidx", return_value=0):
            self.assertEqual(self.cli.complete(before, 0), "slot 1 vst 'Surge XT' ")
            self.assertIsNone(self.cli.complete(before, 1))

    def test_raw_sessions_and_split_assets_preserve_execution_syntax(self):
        for command in ("save", "load"):
            inserted = self.inserted(command + " Live S", "Live Set")
            self.assertEqual(inserted, command + " Live Set")
            self.assertEqual(self.cli._resolve_session_path(inserted.partition(" ")[2]),
                             self.root / "sessions" / "Live Set.json")
        self.assertEqual(self.matches("slot 1 wav "), ["Drums"])
        self.assertEqual(self.matches("slot 1 wav Drums "), ["Kick"])
        self.assertEqual(self.matches("slot 1 vcv "), ["Bass"])
        self.assertEqual(self.matches("slot 1 wav Drums Kick "), [])
        self.assertEqual(self.matches("slot 1 vcv Bass "), [])

    def test_invalid_structural_prefixes_do_not_discover_assets(self):
        with patch.object(self.cli, "_vst_names") as vst, \
                patch.object(self.cli, "_sample_pack_names") as packs, \
                patch.object(self.cli, "_sample_names") as samples, \
                patch.object(self.cli, "_vcv_patch_names") as patches, \
                patch.object(self.cli, "_session_names") as sessions:
            for before in ("unknown ", "slot 99 vst ", "slot master vst ",
                           "slot 1 unknown ", "slot 1 clear ", "slot 1 fx clear ",
                           "slot 1 wav Drums Kick Label ", "loadx "):
                self.matches(before)
            for discover in (vst, packs, samples, patches, sessions):
                discover.assert_not_called()

    def test_control_characters_from_assets_and_metadata_are_never_inserted(self):
        unsafe = ["bad\nstatus", "bad\rstatus", "bad\x00value", "bad\x01value", "bad\x1b[31m", "bad\x7f"]
        with patch.object(self.cli, "_vst_names", return_value=["Good", *unsafe]), \
                patch.object(self.cli, "_session_names", return_value=["Good Set", *unsafe]):
            self.assertEqual(self.matches("slot 1 vst "), ["Good"])
            self.assertEqual(self.matches("load "), ["Good Set"])
        self.instrument.parameters["mode"].valid_values = ("Good Choice", *unsafe)
        self.assertEqual(self.matches("set 1 mode "), ["'Good Choice'"])
        for name in unsafe:
            self.instrument.parameters[name] = ParameterSnapshot()
        self.assertFalse(any("bad" in value for value in self.matches("get 1 ")))

    def test_completion_never_mutates_or_requests_live_audio_state(self):
        parameters = copy.deepcopy(self.instrument.parameters)
        values = copy.deepcopy(self.instrument.values)
        effects = list(self.slot.effects)
        with patch("core.midi.list_midi_input_ports", return_value=[]), \
                patch("core.midi.list_midi_output_ports", return_value=[]):
            for before in ("", "audio start ", "audio diagnose ", "midi input ",
                           "midi input close ", "midimix output ", "slot 1 vst ",
                           "slot 1 fx clear ", "get 1 ", "set 1 mode ",
                           "set master 1 enabled ", "seq link ", "debug "):
                self.matches(before)
        self.forbidden.assert_not_called()
        self.assertEqual(self.instrument.parameters, parameters)
        self.assertEqual(self.instrument.values, values)
        self.assertEqual(self.slot.effects, effects)


if __name__ == "__main__":
    unittest.main()
