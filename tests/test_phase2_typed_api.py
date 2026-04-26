"""Lightweight Phase 2 typed API contract tests.

These tests intentionally use stdlib unittest and fake state only. They avoid
native audio, MIDI, and plugin imports so they can run on development machines
and CI with just Python.
"""

from __future__ import annotations

import importlib
import json
import sys
import tempfile
import threading
import unittest
from unittest import mock
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

web = importlib.import_module("core.web")
try:
    server = importlib.import_module("core.server")
except ModuleNotFoundError:
    server = None


class FakeSlot:
    def __init__(
        self,
        name: str = "Dexed",
        source_type: str = "vst",
        path: str = "/plugins/Dexed.vst3",
        effects: list[SimpleNamespace] | None = None,
    ) -> None:
        self.name: str = name
        self.path: str = path
        self.plugin: SimpleNamespace = SimpleNamespace(
            name=name,
            cutoff=0.42,
            resonance=0.2,
            parameters={
                "cutoff": SimpleNamespace(value=0.42, range=(0.0, 1.0), unit="Hz", label="Cutoff"),
                "resonance": {"value": 0.2, "min": 0.0, "max": 1.0, "unit": "Q", "label": "Resonance"},
                "shape": SimpleNamespace(range=("sine", "saw")),
            },
        )
        self.display_label: str = name
        self.source_type: str = source_type
        self.vcv_patch_path: str | None = None
        self.gain: float = 0.5
        self.muted: bool = False
        self.solo: bool = True
        self.enabled: bool = True
        self.midi_channels: set[int] = set()
        self.effects: list[SimpleNamespace] = effects if effects is not None else [
            SimpleNamespace(
                name="Room",
                mix=0.25,
                parameters={
                    "mix": SimpleNamespace(value=0.25, range=(0.0, 1.0), unit="%", label="Mix"),
                    "mode": SimpleNamespace(range=("small", "large")),
                },
            )
        ]


class FakeEngine:
    def __init__(self) -> None:
        self.running: bool = False
        self.slots: list[FakeSlot | None] = [FakeSlot(), None, None, None, None, None, None, None]
        self.master_gain: float = 0.8
        self.master_effects: list[SimpleNamespace] = [
            SimpleNamespace(
                name="MasterVerb",
                mix=0.25,
                parameters={
                    "mix": SimpleNamespace(value=0.25, range=(0.0, 1.0), unit="%", label="Mix"),
                    "mode": SimpleNamespace(range=("dark", "bright")),
                },
            )
        ]
        self.routes: dict[int, int] = {1: 0, 10: 0}
        self.output_device: object | None = None
        self.param_changes: list[tuple[int, str, float]] = []

    def start(self, output_device: object | None = None) -> None:
        self.running = True
        self.output_device = output_device

    def stop(self) -> None:
        self.running = False

    def enqueue_param_change(self, slot_index: int, param_name: str, value: float) -> None:
        self.param_changes.append((slot_index, param_name, value))

    def any_solo(self) -> bool:
        return any(slot.solo for slot in self.slots if slot is not None)


class FakeHost:
    def __init__(self) -> None:
        self.sample_rate: int = 44100
        self.buffer_size: int = 512
        self.engine: FakeEngine = FakeEngine()
        self.link: SimpleNamespace = SimpleNamespace(enabled=False, bpm=120.0)
        self.session_path: Path = ROOT / "sessions"
        self.loaded_session_name: str | None = "demo"
        self.loaded_session_path: Path | None = ROOT / "sessions" / "demo.json"
        self.channel_map: dict[int, int] = {0: 0, 9: 0}
        self.midi_input_names: list[str] = ["BeatStep"]
        self.mixer_midi_name: str | None = "MIDI Mix"
        self.mixer_midi_out_name: str | None = "MIDI Mix Out"
        self.audio_output_name: str = "Built-in Output"
        self.started_with: object | None = None
        self.refreshed_slots: list[list[int] | None] = []
        self.save_calls: list[str | None] = []
        self.restore_calls: list[str | None] = []
        self.removed_slots: list[int] = []
        self.removed_effects: list[tuple[int | None, int]] = []
        self.start_link_calls: list[float | None] = []
        self.stop_link_calls: int = 0
        self.sent_notes: list[tuple[int, int, int, float]] = []
        self.loaded_wavs: list[tuple[int, str, str | None]] = []

    def start_audio(self, output_device: object | None = None) -> None:
        self.started_with = output_device
        self.engine.start(output_device)

    def stop_audio(self) -> None:
        self.engine.stop()

    def refresh_mixer_leds(self, slots: list[int] | None = None) -> None:
        self.refreshed_slots.append(slots)

    def save_session(self, path: str | None = None) -> None:
        self.save_calls.append(path)

    def restore_session(self, path: str | None = None) -> None:
        self.restore_calls.append(path)

    def remove_instrument(self, idx: int) -> FakeSlot:
        self.removed_slots.append(idx)
        slot = self.engine.slots[idx]
        if slot is None:
            raise ValueError(f"Slot {idx + 1} is already empty")
        self.engine.slots[idx] = None
        return slot

    def remove_effect(self, slot_index: int | None, effect_index: int) -> SimpleNamespace:
        self.removed_effects.append((slot_index, effect_index))
        if slot_index is None:
            return self.engine.master_effects.pop(effect_index)
        slot = self.engine.slots[slot_index]
        if slot is None:
            raise ValueError(f"Slot {slot_index + 1} is empty")
        return slot.effects.pop(effect_index)

    def load_wav(self, slot_index: int, wav_path: str, name: str | None = None) -> FakeSlot:
        self.loaded_wavs.append((slot_index, wav_path, name))
        slot = FakeSlot(
            name=name or Path(wav_path).stem,
            source_type="wav",
            path=wav_path,
            effects=[],
        )
        self.engine.slots[slot_index] = slot
        return slot

    def start_link(self, bpm: float | None = None) -> None:
        self.start_link_calls.append(bpm)
        if bpm is not None:
            self.link.bpm = bpm
        self.link.enabled = True

    def stop_link(self) -> None:
        self.stop_link_calls += 1
        self.link.enabled = False

    def send_note(self, slot_index: int, note: int, velocity: int = 100, duration: float = 0.3) -> None:
        self.sent_notes.append((slot_index, note, velocity, duration))

    def route(self, midi_channel: int, slot_index: int) -> None:
        previous = self.channel_map.get(midi_channel)
        if previous is not None and previous != slot_index:
            previous_slot = self.engine.slots[previous]
            if previous_slot is not None:
                previous_slot.midi_channels.discard(midi_channel)
        self.channel_map[midi_channel] = slot_index
        self.engine.routes[midi_channel + 1] = slot_index
        slot = self.engine.slots[slot_index]
        if slot is not None:
            slot.midi_channels.add(midi_channel)

    def unroute(self, midi_channel: int) -> None:
        previous = self.channel_map.pop(midi_channel, None)
        self.engine.routes.pop(midi_channel + 1, None)
        if previous is not None:
            previous_slot = self.engine.slots[previous]
            if previous_slot is not None:
                previous_slot.midi_channels.discard(midi_channel)


class TypedDaemonApiContractTests(unittest.TestCase):
    def test_daemon_preserves_main_thread_command_queue(self) -> None:
        if server is None:
            self.skipTest("core.server import needs optional native dependencies in this checkout")

        host = FakeHost()
        daemon = server.VcpiServer(host)
        calls: list[tuple[str, str]] = []

        def run_command(line: str, client_id: str) -> tuple[str, bool]:
            calls.append((line, client_id))
            return "ok\n", False

        daemon._run_command = run_command
        result_box: list[tuple[str | None, bool]] = []

        def submit_command() -> None:
            result_box.append(daemon._submit_command("status", "client-test"))

        worker = threading.Thread(target=submit_command)
        worker.start()
        daemon._drain_commands()
        worker.join(timeout=1.0)

        self.assertFalse(worker.is_alive())
        self.assertEqual(result_box, [("ok\n", False)])
        self.assertEqual(calls, [("status", "client-test")])

    def test_json_status_and_slots_use_safe_fake_state(self) -> None:
        if server is None:
            self.skipTest("core.server import needs optional native dependencies in this checkout")

        daemon = server.VcpiServer(FakeHost())
        status = daemon._handle_json_operation("status", {})
        slots = daemon._handle_json_operation("slots", {})

        self.assertTrue(status["ok"])
        self.assertEqual(status["status"]["sample_rate"], 44100)
        self.assertEqual(status["status"]["audio"]["output"], "Built-in Output")
        self.assertEqual(status["status"]["audio"]["master_effects"], 1)
        self.assertTrue(slots["ok"])
        self.assertEqual(slots["slots"][0]["slot"], 1)
        self.assertEqual(slots["slots"][0]["midi_channels"], [1, 10])
        self.assertFalse(slots["slots"][1]["loaded"])

    def test_json_audio_devices_lists_output_devices_only(self) -> None:
        if server is None:
            self.skipTest("core.server import needs optional native dependencies in this checkout")

        daemon = server.VcpiServer(FakeHost())
        fake_sd = SimpleNamespace(
            default=SimpleNamespace(device=(None, 2)),
            query_devices=lambda: [
                {"name": "Built-in Mic", "max_input_channels": 2, "max_output_channels": 0, "default_samplerate": 44100.0},
                {"name": "Built-in Output", "max_input_channels": 0, "max_output_channels": 2, "default_samplerate": 44100.0},
                {"name": "USB Interface", "max_input_channels": 2, "max_output_channels": 4, "default_samplerate": 48000.0},
            ]
        )

        with mock.patch.object(server.deps, "HAS_SOUNDDEVICE", True), mock.patch.object(server.deps, "sd", fake_sd):
            result = daemon._handle_json_operation("audio.devices", {})

        self.assertTrue(result["ok"])
        self.assertTrue(result["available"])
        self.assertEqual(result["current"], "Built-in Output")
        self.assertEqual(result["default_device"], 2)
        self.assertEqual(
            result["devices"],
            [
                {
                    "id": 1,
                    "name": "Built-in Output",
                    "output_channels": 2,
                    "default": False,
                    "selected": True,
                },
                {
                    "id": 2,
                    "name": "USB Interface",
                    "output_channels": 4,
                    "default": True,
                    "selected": False,
                },
            ],
        )

    def test_json_audio_devices_reports_unavailable_without_sounddevice(self) -> None:
        if server is None:
            self.skipTest("core.server import needs optional native dependencies in this checkout")

        daemon = server.VcpiServer(FakeHost())

        with mock.patch.object(server.deps, "HAS_SOUNDDEVICE", False), mock.patch.object(server.deps, "sd", None):
            result = daemon._handle_json_operation("audio.devices", {})

        self.assertTrue(result["ok"])
        self.assertFalse(result["available"])
        self.assertEqual(result["current"], "Built-in Output")
        self.assertIsNone(result["default_device"])
        self.assertEqual(result["devices"], [])

    def test_json_samples_lists_safe_builtin_wav_catalog(self) -> None:
        if server is None:
            self.skipTest("core.server import needs optional native dependencies in this checkout")

        daemon = server.VcpiServer(FakeHost())
        with tempfile.TemporaryDirectory() as tmp:
            samples_root = Path(tmp) / "sampler" / "samples"
            pack_root = samples_root / "808"
            pack_root.mkdir(parents=True)
            _ = (pack_root / "kick.wav").write_bytes(b"")
            _ = (pack_root / "snare.txt").write_text("not a wav")
            hidden_root = samples_root / ".hidden"
            hidden_root.mkdir()
            _ = (hidden_root / "secret.wav").write_bytes(b"")
            daemon._samples_root = lambda: samples_root

            result = daemon._handle_json_operation("samples", {})

        self.assertTrue(result["ok"])
        self.assertEqual(
            result["packs"],
            [{"name": "808", "samples": [{"name": "kick", "filename": "kick.wav"}]}],
        )
        self.assertEqual(result["samples"], {"808": ["kick"]})

    def test_json_slot_wav_load_loads_catalog_sample_and_returns_updated_state(self) -> None:
        if server is None:
            self.skipTest("core.server import needs optional native dependencies in this checkout")

        host = FakeHost()
        daemon = server.VcpiServer(host)
        with tempfile.TemporaryDirectory() as tmp:
            samples_root = Path(tmp) / "sampler" / "samples"
            pack_root = samples_root / "808"
            pack_root.mkdir(parents=True)
            sample_path = pack_root / "kick.wav"
            _ = sample_path.write_bytes(b"")
            daemon._samples_root = lambda: samples_root

            result = daemon._handle_json_operation(
                "slot.wav.load",
                {"slot": 2, "pack": "808", "sample": "kick", "name": "Kick"},
            )

        self.assertTrue(result["ok"])
        self.assertEqual(host.loaded_wavs, [(1, str(sample_path), "Kick")])
        self.assertEqual(result["slot"]["slot"], 2)
        self.assertEqual(result["slot"]["name"], "Kick")
        self.assertEqual(result["slot"]["source_type"], "wav")
        self.assertEqual(result["slot"]["path"], str(sample_path))
        self.assertEqual(result["slots"][1]["source_type"], "wav")
        self.assertEqual(result["status"]["slots_loaded"], 2)

    def test_json_slot_wav_load_rejects_invalid_inputs_before_mutation(self) -> None:
        if server is None:
            self.skipTest("core.server import needs optional native dependencies in this checkout")

        cases = [
            ({"slot": 2, "pack": "808", "sample": "kick", "extra": True}, 400, "only slot"),
            ({"slot": 0, "pack": "808", "sample": "kick"}, 400, "slot must be 1-8"),
            ({"slot": 9, "pack": "808", "sample": "kick"}, 400, "slot must be 1-8"),
            ({"slot": "2", "pack": "808", "sample": "kick"}, 400, "slot must be an integer"),
            ({"slot": True, "pack": "808", "sample": "kick"}, 400, "slot must be an integer"),
            ({"slot": 2, "pack": "", "sample": "kick"}, 400, "pack must not be empty"),
            ({"slot": 2, "pack": 808, "sample": "kick"}, 400, "pack must be a string"),
            ({"slot": 2, "pack": "../808", "sample": "kick"}, 400, "pack must not contain '..'"),
            ({"slot": 2, "pack": "/tmp", "sample": "kick"}, 400, "pack must not contain path separators"),
            ({"slot": 2, "pack": "80/8", "sample": "kick"}, 400, "pack must not contain path separators"),
            ({"slot": 2, "pack": "808", "sample": ""}, 400, "sample must not be empty"),
            ({"slot": 2, "pack": "808", "sample": 123}, 400, "sample must be a string"),
            ({"slot": 2, "pack": "808", "sample": "../kick"}, 400, "sample must not contain '..'"),
            ({"slot": 2, "pack": "808", "sample": "drums/kick"}, 400, "sample must not contain path separators"),
            ({"slot": 2, "pack": "808", "sample": "kick", "name": ""}, 400, "name must not be empty"),
            ({"slot": 2, "pack": "808", "sample": "kick", "name": 123}, 400, "name must be a string"),
            ({"slot": 2, "pack": "808", "sample": "kick", "name": "Bad/Name"}, 400, "name must not contain path separators"),
            ({"slot": 2, "pack": "909", "sample": "kick"}, 404, "sample pack not found"),
            ({"slot": 2, "pack": "808", "sample": "snare"}, 404, "sample not found"),
        ]

        with tempfile.TemporaryDirectory() as tmp:
            samples_root = Path(tmp) / "sampler" / "samples"
            pack_root = samples_root / "808"
            pack_root.mkdir(parents=True)
            _ = (pack_root / "kick.wav").write_bytes(b"")

            for payload, status, message in cases:
                with self.subTest(payload=payload):
                    host = FakeHost()
                    daemon = server.VcpiServer(host)
                    daemon._samples_root = lambda: samples_root
                    response = json.loads(
                        daemon._run_json_request(
                            json.dumps({"op": "slot.wav.load", "payload": payload}),
                            "test",
                        )
                    )
                    self.assertFalse(response["ok"])
                    self.assertEqual(response["status"], status)
                    self.assertIn(message, response["error"])
                    self.assertEqual(host.loaded_wavs, [])
                    self.assertIsNone(host.engine.slots[1])

    def test_json_flow_returns_current_ascii_signal_flow(self) -> None:
        if server is None:
            self.skipTest("core.server import needs optional native dependencies in this checkout")

        daemon = server.VcpiServer(FakeHost())

        result = daemon._handle_json_operation("flow", {})

        self.assertTrue(result["ok"])
        self.assertIsInstance(result["flow"], str)
        self.assertIn("vcpi Signal Flow", result["flow"])
        self.assertIn("Dexed -> Room", result["flow"])
        self.assertIn("Master", result["flow"])

    def test_json_slot_info_returns_loaded_slot_plugin_metadata(self) -> None:
        if server is None:
            self.skipTest("core.server import needs optional native dependencies in this checkout")

        host = FakeHost()
        slot = host.engine.slots[0]
        if slot is None:
            self.fail("FakeHost slot 1 should be loaded")
        plugin = slot.plugin
        plugin.manufacturer_name = "Digital Suburban"
        plugin.category = "Instrument|Synth"
        plugin.version = "0.9.6"
        plugin.identifier = "com.example.dexed"
        plugin.info_type = "Instrument"
        plugin.reported_latency_samples = 0
        plugin.parameters = {"cutoff": object(), "resonance": object()}
        daemon = server.VcpiServer(host)

        result = daemon._handle_json_operation("slot.info", {"slot": 1})

        self.assertTrue(result["ok"])
        self.assertTrue(result["slot"]["loaded"])
        self.assertEqual(result["slot"]["slot"], 1)
        self.assertEqual(result["slot"]["midi_channels"], [1, 10])
        self.assertIn("Slot 1: Dexed", result["rendered"])
        self.assertIn("Vendor", result["rendered"])
        self.assertEqual(result["instrument"]["name"], "Dexed")
        self.assertEqual(result["instrument"]["vendor"], "Digital Suburban")
        self.assertEqual(result["instrument"]["parameters"], {"count": 2})
        self.assertEqual(result["effects"][0]["name"], "Room")

    def test_json_slot_info_empty_slot_returns_read_only_empty_state(self) -> None:
        if server is None:
            self.skipTest("core.server import needs optional native dependencies in this checkout")

        daemon = server.VcpiServer(FakeHost())

        result = daemon._handle_json_operation("slot.info", {"slot": 2})

        self.assertTrue(result["ok"])
        self.assertFalse(result["slot"]["loaded"])
        self.assertEqual(result["slot"]["slot"], 2)
        self.assertEqual(result["message"], "Slot 2 is empty")
        self.assertIsNone(result["instrument"])
        self.assertEqual(result["effects"], [])
        self.assertEqual(result["rendered"], "")

    def test_json_slot_info_rejects_invalid_slot_payloads(self) -> None:
        if server is None:
            self.skipTest("core.server import needs optional native dependencies in this checkout")

        daemon = server.VcpiServer(FakeHost())

        for payload in ({"slot": 9}, {"slot": "1"}, {"slot": True}, {}):
            with self.subTest(payload=payload):
                response = json.loads(
                    daemon._run_json_request(
                        json.dumps({"op": "slot.info", "payload": payload}),
                        "test",
                    )
                )
                self.assertFalse(response["ok"])
                self.assertEqual(response["status"], 400)

    def test_json_slot_params_returns_loaded_slot_parameter_metadata(self) -> None:
        if server is None:
            self.skipTest("core.server import needs optional native dependencies in this checkout")

        daemon = server.VcpiServer(FakeHost())

        result = daemon._handle_json_operation("slot.params", {"slot": 1})

        self.assertTrue(result["ok"])
        self.assertEqual(result["slot"]["slot"], 1)
        self.assertTrue(result["slot"]["loaded"])
        self.assertEqual(result["count"], 3)
        self.assertEqual(
            result["parameters"][:2],
            [
                {
                    "index": 0,
                    "name": "cutoff",
                    "label": "Cutoff",
                    "value": 0.42,
                    "minimum": 0.0,
                    "maximum": 1.0,
                    "unit": "Hz",
                },
                {
                    "index": 1,
                    "name": "resonance",
                    "label": "Resonance",
                    "value": 0.2,
                    "minimum": 0.0,
                    "maximum": 1.0,
                    "unit": "Q",
                },
            ],
        )
        self.assertEqual(result["parameters"][2]["name"], "shape")
        self.assertEqual(result["parameters"][2]["minimum"], "sine")
        self.assertEqual(result["parameters"][2]["maximum"], "saw")
        self.assertEqual(result["instrument"]["kind"], "instrument")
        self.assertIsNone(result["instrument"]["index"])
        self.assertEqual(result["instrument"]["parameters"], result["parameters"])
        self.assertEqual(result["instrument"]["count"], result["count"])
        self.assertEqual(result["instrument"]["rendered"], result["rendered"])
        self.assertEqual(len(result["effects"]), 1)
        self.assertEqual(result["effects"][0]["kind"], "effect")
        self.assertEqual(result["effects"][0]["index"], 1)
        self.assertEqual(result["effects"][0]["name"], "Room")
        self.assertEqual(result["effects"][0]["count"], 2)
        self.assertEqual(result["effects"][0]["parameters"][0]["name"], "mix")
        self.assertEqual(result["effects"][0]["parameters"][0]["value"], 0.25)

    def test_json_slot_params_rejects_empty_slot(self) -> None:
        if server is None:
            self.skipTest("core.server import needs optional native dependencies in this checkout")

        daemon = server.VcpiServer(FakeHost())

        response = json.loads(daemon._run_json_request('{"op":"slot.params","payload":{"slot":2}}', "test"))

        self.assertFalse(response["ok"])
        self.assertEqual(response["status"], 400)
        self.assertIn("slot 2 is empty", response["error"])

    def test_json_slot_params_rejects_invalid_slot_payloads(self) -> None:
        if server is None:
            self.skipTest("core.server import needs optional native dependencies in this checkout")

        daemon = server.VcpiServer(FakeHost())

        for payload in ({"slot": 0}, {"slot": 9}, {"slot": "1"}, {"slot": True}, {}):
            with self.subTest(payload=payload):
                response = json.loads(
                    daemon._run_json_request(
                        json.dumps({"op": "slot.params", "payload": payload}),
                        "test",
                    )
                )
                self.assertFalse(response["ok"])
                self.assertEqual(response["status"], 400)

    def test_json_slot_param_set_updates_loaded_slot_numeric_parameter(self) -> None:
        if server is None:
            self.skipTest("core.server import needs optional native dependencies in this checkout")

        host = FakeHost()
        slot = host.engine.slots[0]
        if slot is None:
            self.fail("FakeHost slot 1 should be loaded")
        daemon = server.VcpiServer(host)

        result = daemon._handle_json_operation(
            "slot.param.set",
            {"slot": 1, "name": "cutoff", "value": 0.75},
        )

        self.assertTrue(result["ok"])
        self.assertEqual(host.engine.param_changes, [(0, "cutoff", 0.75)])
        self.assertEqual(slot.plugin.cutoff, 0.75)
        self.assertEqual(result["slot"]["slot"], 1)
        self.assertTrue(result["slot"]["loaded"])
        self.assertEqual(result["parameter"]["name"], "cutoff")
        self.assertEqual(result["parameter"]["value"], 0.75)
        self.assertEqual(result["count"], 3)
        self.assertIn("cutoff = 0.75", result["rendered"])
        self.assertEqual(result["parameters"][0]["value"], 0.75)

    def test_json_slot_param_set_updates_slot_effect_numeric_parameter(self) -> None:
        if server is None:
            self.skipTest("core.server import needs optional native dependencies in this checkout")

        host = FakeHost()
        slot = host.engine.slots[0]
        if slot is None:
            self.fail("FakeHost slot 1 should be loaded")
        daemon = server.VcpiServer(host)

        result = daemon._handle_json_operation(
            "slot.param.set",
            {"slot": 1, "target": "effect", "effect": 1, "name": "mix", "value": 0.5},
        )

        self.assertTrue(result["ok"])
        self.assertEqual(host.engine.param_changes, [])
        self.assertEqual(slot.plugin.cutoff, 0.42)
        self.assertEqual(slot.effects[0].mix, 0.5)
        self.assertEqual(result["slot"]["slot"], 1)
        self.assertTrue(result["slot"]["loaded"])
        self.assertEqual(result["effect"]["kind"], "effect")
        self.assertEqual(result["effect"]["index"], 1)
        self.assertEqual(result["parameter"]["name"], "mix")
        self.assertEqual(result["parameter"]["value"], 0.5)
        self.assertEqual(result["effects"][0]["parameters"][0]["value"], 0.5)
        self.assertEqual(result["parameters"][0]["value"], 0.42)

    def test_json_slot_param_set_rejects_empty_slot(self) -> None:
        if server is None:
            self.skipTest("core.server import needs optional native dependencies in this checkout")

        host = FakeHost()
        daemon = server.VcpiServer(host)

        response = json.loads(
            daemon._run_json_request(
                '{"op":"slot.param.set","payload":{"slot":2,"name":"cutoff","value":0.5}}',
                "test",
            )
        )

        self.assertFalse(response["ok"])
        self.assertEqual(response["status"], 400)
        self.assertIn("slot 2 is empty", response["error"])
        self.assertEqual(host.engine.param_changes, [])

    def test_json_slot_param_set_rejects_invalid_name_payloads(self) -> None:
        if server is None:
            self.skipTest("core.server import needs optional native dependencies in this checkout")

        host = FakeHost()
        daemon = server.VcpiServer(host)
        cases = [
            {"slot": 1, "value": 0.5},
            {"slot": 1, "name": "", "value": 0.5},
            {"slot": 1, "name": "   ", "value": 0.5},
            {"slot": 1, "name": 123, "value": 0.5},
        ]

        for payload in cases:
            with self.subTest(payload=payload):
                response = json.loads(
                    daemon._run_json_request(
                        json.dumps({"op": "slot.param.set", "payload": payload}),
                        "test",
                    )
                )
                self.assertFalse(response["ok"])
                self.assertEqual(response["status"], 400)
                self.assertEqual(host.engine.param_changes, [])

    def test_json_slot_param_set_rejects_invalid_value_payloads(self) -> None:
        if server is None:
            self.skipTest("core.server import needs optional native dependencies in this checkout")

        host = FakeHost()
        daemon = server.VcpiServer(host)
        cases = [
            {"slot": 1, "name": "cutoff"},
            {"slot": 1, "name": "cutoff", "value": "0.5"},
            {"slot": 1, "name": "cutoff", "value": True},
            {"slot": 1, "name": "cutoff", "value": None},
            {"slot": 1, "name": "cutoff", "value": [0.5]},
            {"slot": 1, "name": "cutoff", "value": {"amount": 0.5}},
            {"slot": 1, "name": "cutoff", "value": float("nan")},
            {"slot": 1, "name": "cutoff", "value": float("inf")},
        ]

        for payload in cases:
            with self.subTest(payload=payload):
                response = json.loads(
                    daemon._run_json_request(
                        json.dumps({"op": "slot.param.set", "payload": payload}),
                        "test",
                    )
                )
                self.assertFalse(response["ok"])
                self.assertEqual(response["status"], 400)
                self.assertEqual(host.engine.param_changes, [])

    def test_json_slot_param_set_rejects_unknown_nonnumeric_and_out_of_range_params(self) -> None:
        if server is None:
            self.skipTest("core.server import needs optional native dependencies in this checkout")

        host = FakeHost()
        slot = host.engine.slots[0]
        if slot is None:
            self.fail("FakeHost slot 1 should be loaded")
        daemon = server.VcpiServer(host)
        cases = [
            ({"slot": 1, "name": "missing", "value": 0.5}, "unknown parameter"),
            ({"slot": 1, "name": "shape", "value": 0.5}, "non-numeric range"),
            ({"slot": 1, "name": "cutoff", "value": -0.01}, "value must be >= 0"),
            ({"slot": 1, "name": "cutoff", "value": 1.01}, "value must be <= 1"),
        ]

        for payload, message in cases:
            with self.subTest(payload=payload):
                response = json.loads(
                    daemon._run_json_request(
                        json.dumps({"op": "slot.param.set", "payload": payload}),
                        "test",
                    )
                )
                self.assertFalse(response["ok"])
                self.assertEqual(response["status"], 400)
                self.assertIn(message, response["error"])
                self.assertEqual(slot.plugin.cutoff, 0.42)
                self.assertEqual(host.engine.param_changes, [])

    def test_json_slot_param_set_rejects_invalid_effect_targets_and_payloads(self) -> None:
        if server is None:
            self.skipTest("core.server import needs optional native dependencies in this checkout")

        host = FakeHost()
        slot = host.engine.slots[0]
        if slot is None:
            self.fail("FakeHost slot 1 should be loaded")
        daemon = server.VcpiServer(host)
        cases = [
            ({"slot": 1, "target": "master", "name": "mix", "value": 0.5}, "target"),
            ({"slot": 1, "target": "effect", "name": "mix", "value": 0.5}, "effect must be an integer"),
            ({"slot": 1, "target": "effect", "effect": True, "name": "mix", "value": 0.5}, "effect must be an integer"),
            ({"slot": 1, "target": "effect", "effect": "1", "name": "mix", "value": 0.5}, "effect must be an integer"),
            ({"slot": 1, "target": "effect", "effect": 0, "name": "mix", "value": 0.5}, "effect index is out of range"),
            ({"slot": 1, "target": "effect", "effect": 2, "name": "mix", "value": 0.5}, "effect index is out of range"),
            ({"slot": 1, "target": "effect", "effect": 1, "name": "missing", "value": 0.5}, "unknown parameter"),
            ({"slot": 1, "target": "effect", "effect": 1, "name": "mode", "value": 0.5}, "non-numeric range"),
            ({"slot": 1, "target": "effect", "effect": 1, "name": "mix", "value": -0.01}, "value must be >= 0"),
            ({"slot": 1, "target": "effect", "effect": 1, "name": "mix", "value": 1.01}, "value must be <= 1"),
            ({"slot": 1, "target": "effect", "effect": 1, "name": "mix"}, "value must be a finite number"),
            ({"slot": 1, "target": "effect", "effect": 1, "name": "mix", "value": True}, "value must be a finite number"),
            ({"slot": 1, "target": "effect", "effect": 1, "name": "mix", "value": float("nan")}, "value must be a finite number"),
            ({"slot": 1, "target": "effect", "effect": 1, "name": "mix", "value": float("inf")}, "value must be a finite number"),
        ]

        for payload, message in cases:
            with self.subTest(payload=payload):
                response = json.loads(
                    daemon._run_json_request(
                        json.dumps({"op": "slot.param.set", "payload": payload}),
                        "test",
                    )
                )
                self.assertFalse(response["ok"])
                self.assertEqual(response["status"], 400)
                self.assertIn(message, response["error"])
                self.assertEqual(slot.effects[0].mix, 0.25)
                self.assertEqual(slot.plugin.cutoff, 0.42)
                self.assertEqual(host.engine.param_changes, [])

    def test_json_slot_fx_clear_removes_loaded_slot_effect_and_returns_updated_state(self) -> None:
        if server is None:
            self.skipTest("core.server import needs optional native dependencies in this checkout")

        host = FakeHost()
        daemon = server.VcpiServer(host)

        result = daemon._handle_json_operation("slot.fx.clear", {"slot": 1, "effect": 1})

        self.assertTrue(result["ok"])
        self.assertEqual(host.removed_effects, [(0, 0)])
        self.assertEqual(result["removed"], {"kind": "effect", "slot": 1, "effect": 1, "name": "Room"})
        self.assertEqual(result["slot"]["slot"], 1)
        self.assertEqual(result["slot"]["effects"], 0)
        self.assertEqual(result["slots"][0]["effects"], 0)
        self.assertEqual(result["status"]["slots_loaded"], 1)

    def test_json_slot_fx_clear_rejects_invalid_payloads_before_mutation(self) -> None:
        if server is None:
            self.skipTest("core.server import needs optional native dependencies in this checkout")

        host = FakeHost()
        daemon = server.VcpiServer(host)
        cases = [
            ({"effect": 1}, "slot must be an integer"),
            ({"slot": 0, "effect": 1}, "slot must be 1-8"),
            ({"slot": 9, "effect": 1}, "slot must be 1-8"),
            ({"slot": "1", "effect": 1}, "slot must be an integer"),
            ({"slot": True, "effect": 1}, "slot must be an integer"),
            ({"slot": 1}, "effect must be an integer"),
            ({"slot": 1, "effect": "1"}, "effect must be an integer"),
            ({"slot": 1, "effect": True}, "effect must be an integer"),
            ({"slot": 1, "effect": 0}, "effect index is out of range"),
            ({"slot": 1, "effect": 2}, "effect index is out of range"),
            ({"slot": 2, "effect": 1}, "slot 2 is empty"),
        ]

        for payload, message in cases:
            with self.subTest(payload=payload):
                response = json.loads(
                    daemon._run_json_request(
                        json.dumps({"op": "slot.fx.clear", "payload": payload}),
                        "test",
                    )
                )
                self.assertFalse(response["ok"])
                self.assertEqual(response["status"], 400)
                self.assertIn(message, response["error"])
                self.assertEqual(host.removed_effects, [])
                slot = host.engine.slots[0]
                if slot is None:
                    self.fail("FakeHost slot 1 should remain loaded")
                self.assertEqual(len(slot.effects), 1)

    def test_json_master_fx_params_returns_master_effect_parameter_metadata(self) -> None:
        if server is None:
            self.skipTest("core.server import needs optional native dependencies in this checkout")

        daemon = server.VcpiServer(FakeHost())

        result = daemon._handle_json_operation("master.fx.params", {"effect": 1})

        self.assertTrue(result["ok"])
        self.assertEqual(result["effect"]["kind"], "master_effect")
        self.assertEqual(result["effect"]["index"], 1)
        self.assertEqual(result["effect"]["name"], "MasterVerb")
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["parameters"], result["effect"]["parameters"])
        self.assertEqual(result["parameters"][0]["name"], "mix")
        self.assertEqual(result["parameters"][0]["value"], 0.25)
        self.assertEqual(result["parameters"][0]["minimum"], 0.0)
        self.assertEqual(result["parameters"][0]["maximum"], 1.0)
        self.assertEqual(result["parameters"][1]["name"], "mode")
        self.assertEqual(result["parameters"][1]["minimum"], "dark")
        self.assertEqual(result["parameters"][1]["maximum"], "bright")
        self.assertIn("Master FX 1", result["rendered"])

    def test_json_master_fx_params_rejects_invalid_effect_payloads(self) -> None:
        if server is None:
            self.skipTest("core.server import needs optional native dependencies in this checkout")

        daemon = server.VcpiServer(FakeHost())

        for payload in ({}, {"effect": "1"}, {"effect": True}, {"effect": 0}, {"effect": 2}):
            with self.subTest(payload=payload):
                response = json.loads(
                    daemon._run_json_request(
                        json.dumps({"op": "master.fx.params", "payload": payload}),
                        "test",
                    )
                )
                self.assertFalse(response["ok"])
                self.assertEqual(response["status"], 400)

    def test_json_master_fx_param_set_updates_numeric_parameter(self) -> None:
        if server is None:
            self.skipTest("core.server import needs optional native dependencies in this checkout")

        host = FakeHost()
        effect = host.engine.master_effects[0]
        daemon = server.VcpiServer(host)

        result = daemon._handle_json_operation(
            "master.fx.param.set",
            {"effect": 1, "name": "mix", "value": 0.5},
        )

        self.assertTrue(result["ok"])
        self.assertEqual(host.engine.param_changes, [])
        self.assertEqual(effect.mix, 0.5)
        self.assertEqual(result["effect"]["kind"], "master_effect")
        self.assertEqual(result["effect"]["index"], 1)
        self.assertEqual(result["parameter"]["name"], "mix")
        self.assertEqual(result["parameter"]["value"], 0.5)
        self.assertEqual(result["parameters"][0]["value"], 0.5)
        self.assertIn("mix = 0.5", result["rendered"])

    def test_json_master_fx_param_set_rejects_invalid_payloads_before_mutation(self) -> None:
        if server is None:
            self.skipTest("core.server import needs optional native dependencies in this checkout")

        host = FakeHost()
        effect = host.engine.master_effects[0]
        daemon = server.VcpiServer(host)
        cases = [
            ({"name": "mix", "value": 0.5}, "effect must be an integer"),
            ({"effect": "1", "name": "mix", "value": 0.5}, "effect must be an integer"),
            ({"effect": True, "name": "mix", "value": 0.5}, "effect must be an integer"),
            ({"effect": 0, "name": "mix", "value": 0.5}, "effect index is out of range"),
            ({"effect": 2, "name": "mix", "value": 0.5}, "effect index is out of range"),
            ({"effect": 1, "value": 0.5}, "name must be a string"),
            ({"effect": 1, "name": "", "value": 0.5}, "name must not be empty"),
            ({"effect": 1, "name": "   ", "value": 0.5}, "name must not be empty"),
            ({"effect": 1, "name": 123, "value": 0.5}, "name must be a string"),
            ({"effect": 1, "name": "missing", "value": 0.5}, "unknown parameter"),
            ({"effect": 1, "name": "mode", "value": 0.5}, "non-numeric range"),
            ({"effect": 1, "name": "mix"}, "value must be a finite number"),
            ({"effect": 1, "name": "mix", "value": "0.5"}, "value must be a finite number"),
            ({"effect": 1, "name": "mix", "value": True}, "value must be a finite number"),
            ({"effect": 1, "name": "mix", "value": float("nan")}, "value must be a finite number"),
            ({"effect": 1, "name": "mix", "value": float("inf")}, "value must be a finite number"),
            ({"effect": 1, "name": "mix", "value": -0.01}, "value must be >= 0"),
            ({"effect": 1, "name": "mix", "value": 1.01}, "value must be <= 1"),
        ]

        for payload, message in cases:
            with self.subTest(payload=payload):
                response = json.loads(
                    daemon._run_json_request(
                        json.dumps({"op": "master.fx.param.set", "payload": payload}),
                        "test",
                    )
                )
                self.assertFalse(response["ok"])
                self.assertEqual(response["status"], 400)
                self.assertIn(message, response["error"])
                self.assertEqual(effect.mix, 0.25)
                self.assertEqual(host.engine.param_changes, [])

    def test_json_master_fx_clear_removes_loaded_master_effect_and_returns_updated_status(self) -> None:
        if server is None:
            self.skipTest("core.server import needs optional native dependencies in this checkout")

        host = FakeHost()
        daemon = server.VcpiServer(host)

        result = daemon._handle_json_operation("master.fx.clear", {"effect": 1})

        self.assertTrue(result["ok"])
        self.assertEqual(host.removed_effects, [(None, 0)])
        self.assertEqual(result["removed"], {"kind": "master_effect", "effect": 1, "name": "MasterVerb"})
        self.assertEqual(result["status"]["audio"]["master_effects"], 0)
        self.assertEqual(len(host.engine.master_effects), 0)

    def test_json_master_fx_clear_rejects_invalid_payloads_before_mutation(self) -> None:
        if server is None:
            self.skipTest("core.server import needs optional native dependencies in this checkout")

        host = FakeHost()
        daemon = server.VcpiServer(host)
        cases = [
            ({}, "effect must be an integer"),
            ({"effect": "1"}, "effect must be an integer"),
            ({"effect": True}, "effect must be an integer"),
            ({"effect": 0}, "effect index is out of range"),
            ({"effect": 2}, "effect index is out of range"),
        ]

        for payload, message in cases:
            with self.subTest(payload=payload):
                response = json.loads(
                    daemon._run_json_request(
                        json.dumps({"op": "master.fx.clear", "payload": payload}),
                        "test",
                    )
                )
                self.assertFalse(response["ok"])
                self.assertEqual(response["status"], 400)
                self.assertIn(message, response["error"])
                self.assertEqual(host.removed_effects, [])
                self.assertEqual(len(host.engine.master_effects), 1)

    def test_json_slot_mutations_validate_gain_and_toggle(self) -> None:
        if server is None:
            self.skipTest("core.server import needs optional native dependencies in this checkout")

        host = FakeHost()
        daemon = server.VcpiServer(host)
        gain = daemon._handle_json_operation("slot.gain", {"slot": 1, "gain": 0.25})
        muted = daemon._handle_json_operation("slot.mute", {"slot": 1, "toggle": True})
        no_op = daemon._handle_json_operation("slot.mute", {"slot": 1, "toggle": False})

        self.assertEqual(gain["slot"]["gain"], 0.25)
        self.assertTrue(muted["slot"]["muted"])
        self.assertTrue(no_op["slot"]["muted"])
        self.assertEqual(host.refreshed_slots, [[0], [0]])
        with self.assertRaises(server._JsonOperationError):
            daemon._handle_json_operation("slot.gain", {"slot": 1, "gain": 1.25})
        response = json.loads(daemon._run_json_request('{"op":"slot.mute","payload":{"slot":1,"toggle":"yes"}}', "test"))
        self.assertFalse(response["ok"])
        self.assertEqual(response["status"], 400)

    def test_json_slot_clear_unloads_loaded_slot_and_returns_updated_state(self) -> None:
        if server is None:
            self.skipTest("core.server import needs optional native dependencies in this checkout")

        host = FakeHost()
        daemon = server.VcpiServer(host)

        result = daemon._handle_json_operation("slot.clear", {"slot": 1})

        self.assertTrue(result["ok"])
        self.assertEqual(host.removed_slots, [0])
        self.assertEqual(host.refreshed_slots, [[0]])
        self.assertFalse(result["slot"]["loaded"])
        self.assertIsNone(result["slot"]["name"])
        self.assertEqual(result["slot"]["slot"], 1)
        self.assertEqual(result["slot"]["midi_channels"], [1, 10])
        self.assertEqual(result["status"]["slots_loaded"], 0)
        self.assertFalse(result["slots"][0]["loaded"])

    def test_json_slot_unload_alias_uses_same_clear_contract(self) -> None:
        if server is None:
            self.skipTest("core.server import needs optional native dependencies in this checkout")

        host = FakeHost()
        daemon = server.VcpiServer(host)

        result = daemon._handle_json_operation("slot.unload", {"slot": 1})

        self.assertTrue(result["ok"])
        self.assertEqual(host.removed_slots, [0])
        self.assertFalse(result["slot"]["loaded"])

    def test_json_slot_clear_empty_slot_returns_typed_error(self) -> None:
        if server is None:
            self.skipTest("core.server import needs optional native dependencies in this checkout")

        host = FakeHost()
        daemon = server.VcpiServer(host)

        response = json.loads(daemon._run_json_request('{"op":"slot.clear","payload":{"slot":2}}', "test"))

        self.assertFalse(response["ok"])
        self.assertEqual(response["status"], 400)
        self.assertIn("slot 2 is empty", response["error"])
        self.assertEqual(host.removed_slots, [])
        self.assertEqual(host.refreshed_slots, [])

    def test_json_slot_clear_rejects_invalid_slot_before_core_call(self) -> None:
        if server is None:
            self.skipTest("core.server import needs optional native dependencies in this checkout")

        host = FakeHost()
        daemon = server.VcpiServer(host)

        response = json.loads(daemon._run_json_request('{"op":"slot.clear","payload":{"slot":9}}', "test"))

        self.assertFalse(response["ok"])
        self.assertEqual(response["status"], 400)
        self.assertEqual(host.removed_slots, [])

    def test_json_master_gain_mutation_validates_range(self) -> None:
        if server is None:
            self.skipTest("core.server import needs optional native dependencies in this checkout")

        host = FakeHost()
        daemon = server.VcpiServer(host)
        gain = daemon._handle_json_operation("master.gain", {"gain": 0.35})

        self.assertTrue(gain["ok"])
        self.assertEqual(host.engine.master_gain, 0.35)
        self.assertEqual(gain["status"]["audio"]["master_gain"], 0.35)
        with self.assertRaises(server._JsonOperationError):
            daemon._handle_json_operation("master.gain", {"gain": 1.25})
        response = json.loads(daemon._run_json_request('{"op":"master.gain","payload":{"gain":"loud"}}', "test"))
        self.assertFalse(response["ok"])
        self.assertEqual(response["status"], 400)

    def test_json_tempo_set_updates_bpm_and_rejects_invalid_values(self) -> None:
        if server is None:
            self.skipTest("core.server import needs optional native dependencies in this checkout")

        host = FakeHost()
        daemon = server.VcpiServer(host)

        result = daemon._handle_json_operation("tempo.set", {"bpm": 128})

        self.assertTrue(result["ok"])
        self.assertEqual(host.link.bpm, 128.0)
        self.assertEqual(result["status"]["link"]["bpm"], 128.0)
        invalid_payloads = [
            {"bpm": "fast"},
            {"bpm": True},
            {"bpm": 19.99},
            {"bpm": 300.01},
        ]
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(server._JsonOperationError):
                    daemon._handle_json_operation("tempo.set", payload)

    def test_json_link_start_accepts_optional_bpm_and_stop_disables_link(self) -> None:
        if server is None:
            self.skipTest("core.server import needs optional native dependencies in this checkout")

        host = FakeHost()
        daemon = server.VcpiServer(host)

        start_default = daemon._handle_json_operation("link.start", {})
        start_with_bpm = daemon._handle_json_operation("link.start", {"bpm": 132.5})
        stop = daemon._handle_json_operation("link.stop", {})

        self.assertTrue(start_default["ok"])
        self.assertTrue(start_with_bpm["ok"])
        self.assertTrue(stop["ok"])
        self.assertEqual(host.start_link_calls, [None, 132.5])
        self.assertEqual(host.stop_link_calls, 1)
        self.assertFalse(host.link.enabled)
        self.assertEqual(start_with_bpm["status"]["link"]["bpm"], 132.5)
        self.assertFalse(stop["status"]["link"]["enabled"])

    def test_json_link_start_rejects_invalid_bpm_values(self) -> None:
        if server is None:
            self.skipTest("core.server import needs optional native dependencies in this checkout")

        daemon = server.VcpiServer(FakeHost())
        invalid_payloads = [
            {"bpm": "fast"},
            {"bpm": False},
            {"bpm": 19.99},
            {"bpm": 300.01},
        ]

        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                response = json.loads(
                    daemon._run_json_request(
                        json.dumps({"op": "link.start", "payload": payload}),
                        "test",
                    )
                )
                self.assertFalse(response["ok"])
                self.assertEqual(response["status"], 400)

    def test_json_midi_link_routes_channel_to_loaded_slot(self) -> None:
        if server is None:
            self.skipTest("core.server import needs optional native dependencies in this checkout")

        host = FakeHost()
        daemon = server.VcpiServer(host)

        result = daemon._handle_json_operation("midi.link", {"channel": 2, "slot": 1})

        self.assertTrue(result["ok"])
        self.assertEqual(host.channel_map[1], 0)
        self.assertEqual(result["route"], {"channel": 2, "slot": 1})
        self.assertEqual(result["status"]["midi"]["routing"], {"1": 1, "2": 1, "10": 1})
        self.assertEqual(result["slots"][0]["midi_channels"], [1, 2, 10])

    def test_json_midi_link_allows_route_to_empty_slot(self) -> None:
        if server is None:
            self.skipTest("core.server import needs optional native dependencies in this checkout")

        host = FakeHost()
        daemon = server.VcpiServer(host)

        result = daemon._handle_json_operation("midi.link", {"channel": 3, "slot": 2})

        self.assertTrue(result["ok"])
        self.assertEqual(host.channel_map[2], 1)
        self.assertFalse(result["slots"][1]["loaded"])
        self.assertEqual(result["slots"][1]["midi_channels"], [3])

    def test_json_midi_link_reroutes_previous_assignment(self) -> None:
        if server is None:
            self.skipTest("core.server import needs optional native dependencies in this checkout")

        host = FakeHost()
        host.engine.slots[1] = FakeSlot("Surge")
        daemon = server.VcpiServer(host)

        result = daemon._handle_json_operation("midi.link", {"channel": 1, "slot": 2})

        slot_one = host.engine.slots[0]
        slot_two = host.engine.slots[1]
        if slot_one is None or slot_two is None:
            self.fail("FakeHost slots 1 and 2 should be loaded")
        self.assertTrue(result["ok"])
        self.assertEqual(host.channel_map[0], 1)
        self.assertNotIn(0, slot_one.midi_channels)
        self.assertIn(0, slot_two.midi_channels)
        self.assertEqual(result["status"]["midi"]["routing"]["1"], 2)

    def test_json_midi_cut_removes_routed_channel(self) -> None:
        if server is None:
            self.skipTest("core.server import needs optional native dependencies in this checkout")

        host = FakeHost()
        daemon = server.VcpiServer(host)

        result = daemon._handle_json_operation("midi.cut", {"channel": 1})

        self.assertTrue(result["ok"])
        self.assertEqual(result["route"], {"channel": 1, "slot": None})
        self.assertNotIn(0, host.channel_map)
        self.assertEqual(result["status"]["midi"]["routing"], {"10": 1})
        self.assertEqual(result["slots"][0]["midi_channels"], [10])

    def test_json_midi_cut_unrouted_channel_is_noop(self) -> None:
        if server is None:
            self.skipTest("core.server import needs optional native dependencies in this checkout")

        daemon = server.VcpiServer(FakeHost())

        result = daemon._handle_json_operation("midi.cut", {"channel": 16})

        self.assertTrue(result["ok"])
        self.assertEqual(result["route"], {"channel": 16, "slot": None})
        self.assertEqual(result["status"]["midi"]["routing"], {"1": 1, "10": 1})

    def test_json_midi_link_and_cut_reject_invalid_channel_and_slot_payloads(self) -> None:
        if server is None:
            self.skipTest("core.server import needs optional native dependencies in this checkout")

        daemon = server.VcpiServer(FakeHost())
        cases = [
            ("midi.link", {"channel": 0, "slot": 1}),
            ("midi.link", {"channel": 17, "slot": 1}),
            ("midi.link", {"channel": "1", "slot": 1}),
            ("midi.link", {"channel": True, "slot": 1}),
            ("midi.link", {"channel": 1, "slot": 0}),
            ("midi.link", {"channel": 1, "slot": 9}),
            ("midi.link", {"channel": 1, "slot": "1"}),
            ("midi.link", {"channel": 1, "slot": False}),
            ("midi.cut", {"channel": 0}),
            ("midi.cut", {"channel": 17}),
            ("midi.cut", {"channel": "1"}),
            ("midi.cut", {"channel": True}),
        ]

        for operation, payload in cases:
            with self.subTest(operation=operation, payload=payload):
                response = json.loads(
                    daemon._run_json_request(
                        json.dumps({"op": operation, "payload": payload}),
                        "test",
                    )
                )
                self.assertFalse(response["ok"])
                self.assertEqual(response["status"], 400)

    def test_json_slot_note_triggers_loaded_slot_with_defaults(self) -> None:
        if server is None:
            self.skipTest("core.server import needs optional native dependencies in this checkout")

        host = FakeHost()
        daemon = server.VcpiServer(host)

        result = daemon._handle_json_operation("slot.note", {"slot": 1, "note": 64})

        self.assertTrue(result["ok"])
        self.assertEqual(host.sent_notes, [(0, 64, 100, 0.3)])
        self.assertEqual(result["slot"]["slot"], 1)
        self.assertEqual(result["note"], {"note": 64, "velocity": 100, "duration_ms": 300})

    def test_json_slot_note_rejects_empty_slot(self) -> None:
        if server is None:
            self.skipTest("core.server import needs optional native dependencies in this checkout")

        host = FakeHost()
        daemon = server.VcpiServer(host)

        response = json.loads(daemon._run_json_request('{"op":"slot.note","payload":{"slot":2,"note":60}}', "test"))

        self.assertFalse(response["ok"])
        self.assertEqual(response["status"], 400)
        self.assertIn("slot 2 is empty", response["error"])
        self.assertEqual(host.sent_notes, [])

    def test_json_slot_note_rejects_invalid_payloads_before_send_note(self) -> None:
        if server is None:
            self.skipTest("core.server import needs optional native dependencies in this checkout")

        host = FakeHost()
        daemon = server.VcpiServer(host)
        cases = [
            {"slot": 0, "note": 60},
            {"slot": 9, "note": 60},
            {"slot": "1", "note": 60},
            {"slot": True, "note": 60},
            {"slot": 1},
            {"slot": 1, "note": -1},
            {"slot": 1, "note": 128},
            {"slot": 1, "note": "60"},
            {"slot": 1, "note": False},
            {"slot": 1, "note": 60, "velocity": -1},
            {"slot": 1, "note": 60, "velocity": 128},
            {"slot": 1, "note": 60, "velocity": "100"},
            {"slot": 1, "note": 60, "velocity": True},
            {"slot": 1, "note": 60, "duration_ms": 0},
            {"slot": 1, "note": 60, "duration_ms": 5001},
            {"slot": 1, "note": 60, "duration_ms": 10.5},
            {"slot": 1, "note": 60, "duration_ms": "300"},
            {"slot": 1, "note": 60, "duration_ms": False},
        ]

        for payload in cases:
            with self.subTest(payload=payload):
                response = json.loads(
                    daemon._run_json_request(
                        json.dumps({"op": "slot.note", "payload": payload}),
                        "test",
                    )
                )
                self.assertFalse(response["ok"])
                self.assertEqual(response["status"], 400)
                self.assertEqual(host.sent_notes, [])

    def test_json_session_save_with_name_updates_loaded_session(self) -> None:
        if server is None:
            self.skipTest("core.server import needs optional native dependencies in this checkout")

        host = FakeHost()
        daemon = server.VcpiServer(host)
        with tempfile.TemporaryDirectory() as tmp:
            sessions_root = Path(tmp) / "sessions"
            sessions_root.mkdir()
            daemon._sessions_root = lambda: sessions_root

            result = daemon._handle_json_operation("session.save", {"name": "demo.json"})

            expected_path = sessions_root / "demo.json"
            self.assertTrue(result["ok"])
            self.assertEqual(host.save_calls, [str(expected_path)])
            self.assertEqual(host.loaded_session_name, "demo")
            self.assertEqual(host.loaded_session_path, expected_path)
            self.assertEqual(result["session"]["loaded_path"], str(expected_path))

    def test_json_session_save_without_name_uses_existing_loaded_path(self) -> None:
        if server is None:
            self.skipTest("core.server import needs optional native dependencies in this checkout")

        host = FakeHost()
        daemon = server.VcpiServer(host)

        result = daemon._handle_json_operation("session.save", {})

        self.assertTrue(result["ok"])
        self.assertEqual(host.save_calls, [str(ROOT / "sessions" / "demo.json")])
        self.assertEqual(host.loaded_session_name, "demo")

    def test_json_session_save_without_name_requires_loaded_path(self) -> None:
        if server is None:
            self.skipTest("core.server import needs optional native dependencies in this checkout")

        host = FakeHost()
        host.loaded_session_name = None
        host.loaded_session_path = None
        daemon = server.VcpiServer(host)

        response = json.loads(daemon._run_json_request('{"op":"session.save","payload":{}}', "test"))

        self.assertFalse(response["ok"])
        self.assertEqual(response["status"], 400)
        self.assertEqual(host.save_calls, [])

    def test_json_session_load_existing_restores_refreshes_and_returns_slots(self) -> None:
        if server is None:
            self.skipTest("core.server import needs optional native dependencies in this checkout")

        host = FakeHost()
        daemon = server.VcpiServer(host)
        with tempfile.TemporaryDirectory() as tmp:
            sessions_root = Path(tmp) / "sessions"
            sessions_root.mkdir()
            session_path = sessions_root / "demo.json"
            session_path.write_text("{}")
            daemon._sessions_root = lambda: sessions_root

            result = daemon._handle_json_operation("session.load", {"name": "demo"})

            self.assertTrue(result["ok"])
            self.assertEqual(host.restore_calls, [str(session_path)])
            self.assertEqual(host.refreshed_slots, [None])
            self.assertEqual(host.save_calls, [None])
            self.assertEqual(host.loaded_session_name, "demo")
            self.assertEqual(host.loaded_session_path, session_path)
            self.assertEqual(result["slots"][0]["slot"], 1)

    def test_json_session_load_missing_file_returns_typed_404(self) -> None:
        if server is None:
            self.skipTest("core.server import needs optional native dependencies in this checkout")

        host = FakeHost()
        daemon = server.VcpiServer(host)
        with tempfile.TemporaryDirectory() as tmp:
            sessions_root = Path(tmp) / "sessions"
            sessions_root.mkdir()
            daemon._sessions_root = lambda: sessions_root

            response = json.loads(daemon._run_json_request('{"op":"session.load","payload":{"name":"missing"}}', "test"))

            self.assertFalse(response["ok"])
            self.assertEqual(response["status"], 404)
            self.assertEqual(host.restore_calls, [])

    def test_json_sessions_lists_only_safe_top_level_json_and_marks_loaded(self) -> None:
        if server is None:
            self.skipTest("core.server import needs optional native dependencies in this checkout")

        host = FakeHost()
        daemon = server.VcpiServer(host)
        with tempfile.TemporaryDirectory() as tmp:
            sessions_root = Path(tmp) / "sessions"
            sessions_root.mkdir()
            _ = (sessions_root / "beta.json").write_text("{}")
            _ = (sessions_root / "alpha.json").write_text("{}")
            _ = (sessions_root / ".hidden.json").write_text("{}")
            _ = (sessions_root / "bad name.json").write_text("{}")
            _ = (sessions_root / "notes.txt").write_text("not json")
            nested = sessions_root / "nested"
            nested.mkdir()
            _ = (nested / "inner.json").write_text("{}")
            host.loaded_session_name = "beta"
            host.loaded_session_path = sessions_root / "beta.json"
            daemon._sessions_root = lambda: sessions_root

            result = daemon._handle_json_operation("sessions", {})

            self.assertTrue(result["ok"])
            self.assertEqual(
                result["sessions"],
                [
                    {"name": "alpha", "filename": "alpha.json", "loaded": False},
                    {"name": "beta", "filename": "beta.json", "loaded": True},
                ],
            )
            self.assertEqual(result["session"]["loaded_name"], "beta")
            self.assertEqual(result["session"]["loaded_path"], str(sessions_root / "beta.json"))


class WebSafetyTests(unittest.TestCase):
    def test_command_validation_blocks_shutdown_without_opt_in(self) -> None:
        with self.assertRaises(PermissionError):
            web._validate_command("shutdown", allow_shutdown=False)
        self.assertEqual(web._validate_command("shutdown", allow_shutdown=True), "shutdown")

    def test_command_validation_requires_single_nonempty_line(self) -> None:
        with self.assertRaises(ValueError):
            web._validate_command("", allow_shutdown=False)
        with self.assertRaises(ValueError):
            web._validate_command("status\nshutdown", allow_shutdown=False)
        self.assertEqual(web._validate_command("  status  ", allow_shutdown=False), "status")

    def test_loopback_guard_defaults_to_local_hosts(self) -> None:
        self.assertTrue(web._is_loopback_host("127.0.0.1"))
        self.assertTrue(web._is_loopback_host("localhost"))
        self.assertTrue(web._is_loopback_host("::1"))
        self.assertFalse(web._is_loopback_host("0.0.0.0"))

    def test_typed_web_validation_rejects_bad_slot_gain_and_toggle(self) -> None:
        self.assertEqual(web.VcpiWebHandler._validate_slot_number("1"), 1)
        with self.assertRaises(ValueError):
            web.VcpiWebHandler._validate_slot_number("9")
        web.VcpiWebHandler._validate_gain_payload({"gain": 0.25})
        web.VcpiWebHandler._validate_gain_payload({"gain": 1.0})
        with self.assertRaises(ValueError):
            web.VcpiWebHandler._validate_gain_payload({"gain": 1.25})
        web.VcpiWebHandler._validate_optional_bool_payload({"toggle": True}, "muted")
        with self.assertRaises(ValueError):
            web.VcpiWebHandler._validate_optional_bool_payload({"toggle": "yes"}, "muted")

    def test_typed_web_slot_route_validation_accepts_clear_and_unload(self) -> None:
        clear_match = web.SLOT_ACTION_RE.fullmatch("/api/slots/1/clear")
        unload_match = web.SLOT_ACTION_RE.fullmatch("/api/slots/8/unload")
        note_match = web.SLOT_ACTION_RE.fullmatch("/api/slots/4/note")
        params_match = web.SLOT_ACTION_RE.fullmatch("/api/slots/3/params")
        wav_match = web.SLOT_ACTION_RE.fullmatch("/api/slots/2/wav")
        fx_clear_match = web.SLOT_FX_CLEAR_RE.fullmatch("/api/slots/1/fx/2/clear")
        bad_slot_match = web.SLOT_ACTION_RE.fullmatch("/api/slots/9/clear")

        self.assertIsNotNone(clear_match)
        self.assertEqual(clear_match.group(1), "1")
        self.assertEqual(clear_match.group(2), "clear")
        self.assertIsNotNone(unload_match)
        self.assertEqual(unload_match.group(1), "8")
        self.assertEqual(unload_match.group(2), "unload")
        self.assertIsNotNone(note_match)
        self.assertEqual(note_match.group(1), "4")
        self.assertEqual(note_match.group(2), "note")
        self.assertIsNotNone(params_match)
        self.assertEqual(params_match.group(1), "3")
        self.assertEqual(params_match.group(2), "params")
        self.assertIsNotNone(wav_match)
        self.assertEqual(wav_match.group(1), "2")
        self.assertEqual(wav_match.group(2), "wav")
        self.assertIsNotNone(fx_clear_match)
        self.assertEqual(fx_clear_match.group(1), "1")
        self.assertEqual(fx_clear_match.group(2), "2")
        with self.assertRaises(ValueError):
            web.VcpiWebHandler._validate_slot_number(bad_slot_match.group(1))

    def test_typed_web_note_validation_applies_defaults_and_ranges(self) -> None:
        payload: dict[str, object] = {"note": 60}

        web.VcpiWebHandler._validate_note_payload(payload)

        self.assertEqual(payload, {"note": 60, "velocity": 100, "duration_ms": 300})
        invalid_payloads = [
            {},
            {"note": -1},
            {"note": 128},
            {"note": "60"},
            {"note": False},
            {"note": 60, "velocity": -1},
            {"note": 60, "velocity": 128},
            {"note": 60, "velocity": "100"},
            {"note": 60, "velocity": True},
            {"note": 60, "duration_ms": 0},
            {"note": 60, "duration_ms": 5001},
            {"note": 60, "duration_ms": 10.5},
            {"note": 60, "duration_ms": "300"},
            {"note": 60, "duration_ms": False},
        ]
        for value in invalid_payloads:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    web.VcpiWebHandler._validate_note_payload(value)

    def test_typed_web_wav_load_validation_accepts_safe_catalog_payload(self) -> None:
        payload: dict[str, object] = {"slot": 2, "pack": " 808 ", "sample": " kick.wav ", "name": " Kick "}

        web.VcpiWebHandler._validate_wav_load_payload(payload)

        self.assertEqual(payload, {"slot": 2, "pack": "808", "sample": "kick", "name": "Kick"})

    def test_typed_web_wav_load_validation_rejects_invalid_payloads(self) -> None:
        invalid_payloads = [
            {"slot": 2, "pack": "808", "sample": "kick", "extra": True},
            {"slot": 2, "pack": "", "sample": "kick"},
            {"slot": 2, "pack": 808, "sample": "kick"},
            {"slot": 2, "pack": "../808", "sample": "kick"},
            {"slot": 2, "pack": "/tmp", "sample": "kick"},
            {"slot": 2, "pack": "80/8", "sample": "kick"},
            {"slot": 2, "pack": "808", "sample": ""},
            {"slot": 2, "pack": "808", "sample": 123},
            {"slot": 2, "pack": "808", "sample": "../kick"},
            {"slot": 2, "pack": "808", "sample": "drums/kick"},
            {"slot": 2, "pack": "808", "sample": "kick", "name": ""},
            {"slot": 2, "pack": "808", "sample": "kick", "name": 123},
            {"slot": 2, "pack": "808", "sample": "kick", "name": "Bad/Name"},
        ]
        for value in invalid_payloads:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    web.VcpiWebHandler._validate_wav_load_payload(value)

    def test_typed_web_slot_param_validation_accepts_numeric_payload(self) -> None:
        payload: dict[str, object] = {"name": " cutoff ", "value": 0.75}

        web.VcpiWebHandler._validate_slot_param_payload(payload)

        self.assertEqual(payload, {"name": "cutoff", "value": 0.75})

    def test_typed_web_slot_param_validation_accepts_effect_numeric_payload(self) -> None:
        payload: dict[str, object] = {"target": "effect", "effect": 1, "name": " mix ", "value": 0.5}

        web.VcpiWebHandler._validate_slot_param_payload(payload)

        self.assertEqual(payload, {"target": "effect", "effect": 1, "name": "mix", "value": 0.5})

    def test_typed_web_slot_param_validation_rejects_invalid_payloads(self) -> None:
        invalid_payloads = [
            {},
            {"name": "", "value": 0.5},
            {"name": "   ", "value": 0.5},
            {"name": 123, "value": 0.5},
            {"name": "x" * (web.MAX_PARAMETER_NAME_LENGTH + 1), "value": 0.5},
            {"name": "cutoff"},
            {"name": "cutoff", "value": "0.5"},
            {"name": "cutoff", "value": True},
            {"name": "cutoff", "value": None},
            {"name": "cutoff", "value": [0.5]},
            {"name": "cutoff", "value": {"amount": 0.5}},
            {"name": "cutoff", "value": float("nan")},
            {"name": "cutoff", "value": float("inf")},
            {"target": "master", "name": "cutoff", "value": 0.5},
            {"target": True, "name": "cutoff", "value": 0.5},
            {"target": "effect", "name": "mix", "value": 0.5},
            {"target": "effect", "effect": 0, "name": "mix", "value": 0.5},
            {"target": "effect", "effect": True, "name": "mix", "value": 0.5},
            {"target": "effect", "effect": "1", "name": "mix", "value": 0.5},
        ]
        for value in invalid_payloads:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    web.VcpiWebHandler._validate_slot_param_payload(value)

    def test_typed_web_bpm_validation_accepts_numeric_range(self) -> None:
        web.VcpiWebHandler._validate_bpm_payload({"bpm": 20.0}, required=True)
        web.VcpiWebHandler._validate_bpm_payload({"bpm": 300}, required=True)
        web.VcpiWebHandler._validate_bpm_payload({}, required=False)
        with self.assertRaises(ValueError):
            web.VcpiWebHandler._validate_bpm_payload({"bpm": "fast"}, required=True)
        with self.assertRaises(ValueError):
            web.VcpiWebHandler._validate_bpm_payload({"bpm": True}, required=True)
        with self.assertRaises(ValueError):
            web.VcpiWebHandler._validate_bpm_payload({"bpm": 19.99}, required=True)
        with self.assertRaises(ValueError):
            web.VcpiWebHandler._validate_bpm_payload({"bpm": 300.01}, required=True)

    def test_typed_web_tempo_route_maps_to_tempo_operation(self) -> None:
        handler = web.VcpiWebHandler.__new__(web.VcpiWebHandler)
        handler.path = "/api/tempo"
        handler.server = SimpleNamespace(sock_path=Path("/tmp/vcpi.sock"), daemon_timeout=1.0)
        payload: dict[str, object] = {"ok": True, "status": {"link": {"bpm": 128.0}}}

        with mock.patch.object(
            web.VcpiWebHandler,
            "_read_secure_optional_json_body",
            return_value={"bpm": 128},
        ), mock.patch.object(
            web.VcpiWebHandler,
            "_validate_post_security",
            return_value=None,
        ), mock.patch.object(
            web,
            "execute_json_operation",
            return_value=SimpleNamespace(payload=payload),
        ) as execute_json_operation, mock.patch.object(web, "_send_json") as send_json:
            handler.do_POST()

        execute_json_operation.assert_called_once_with(
            "tempo.set",
            {"bpm": 128},
            Path("/tmp/vcpi.sock"),
            daemon_timeout=1.0,
        )
        send_json.assert_called_once_with(handler, web.HTTPStatus.OK, payload)

    def test_typed_web_link_routes_map_to_link_operations(self) -> None:
        cases = [
            ("/api/link/start", {"bpm": 126.5}, "link.start", {"bpm": 126.5}),
            ("/api/link/start", {}, "link.start", {}),
            ("/api/link/stop", {}, "link.stop", {}),
        ]

        for path, body, operation, expected_payload in cases:
            with self.subTest(path=path, body=body):
                handler = web.VcpiWebHandler.__new__(web.VcpiWebHandler)
                handler.path = path
                handler.server = SimpleNamespace(sock_path=Path("/tmp/vcpi.sock"), daemon_timeout=1.0)
                payload: dict[str, object] = {"ok": True, "status": {"link": {"enabled": path.endswith("start")}}}

                with mock.patch.object(
                    web.VcpiWebHandler,
                    "_read_secure_optional_json_body",
                    return_value=body,
                ), mock.patch.object(
                    web.VcpiWebHandler,
                    "_validate_post_security",
                    return_value=None,
                ), mock.patch.object(
                    web,
                    "execute_json_operation",
                    return_value=SimpleNamespace(payload=payload),
                ) as execute_json_operation, mock.patch.object(web, "_send_json") as send_json:
                    handler.do_POST()

                execute_json_operation.assert_called_once_with(
                    operation,
                    expected_payload,
                    Path("/tmp/vcpi.sock"),
                    daemon_timeout=1.0,
                )
                send_json.assert_called_once_with(handler, web.HTTPStatus.OK, payload)

    def test_typed_web_tempo_and_link_routes_reject_invalid_bpm(self) -> None:
        cases = [
            ("/api/tempo", {"bpm": "fast"}),
            ("/api/link/start", {"bpm": True}),
            ("/api/link/start", {"bpm": 19.99}),
            ("/api/tempo", {"bpm": 300.01}),
        ]

        for path, body in cases:
            with self.subTest(path=path, body=body):
                handler = web.VcpiWebHandler.__new__(web.VcpiWebHandler)
                handler.path = path
                handler.server = SimpleNamespace(sock_path=Path("/tmp/vcpi.sock"), daemon_timeout=1.0)

                with mock.patch.object(
                    web.VcpiWebHandler,
                    "_read_secure_optional_json_body",
                    return_value=body,
                ), mock.patch.object(web, "execute_json_operation") as execute_json_operation, mock.patch.object(
                    web,
                    "_send_json",
                ) as send_json:
                    handler.do_POST()

                execute_json_operation.assert_not_called()
                self.assertEqual(send_json.call_args.args[0], handler)
                self.assertEqual(send_json.call_args.args[1], web.HTTPStatus.BAD_REQUEST)

    def test_typed_web_midi_routes_map_to_midi_operations(self) -> None:
        cases = [
            ("/api/midi/link", {"channel": 2, "slot": 3}, "midi.link", {"channel": 2, "slot": 3}),
            ("/api/midi/cut", {"channel": 10}, "midi.cut", {"channel": 10}),
        ]

        for path, body, operation, expected_payload in cases:
            with self.subTest(path=path):
                handler = web.VcpiWebHandler.__new__(web.VcpiWebHandler)
                handler.path = path
                handler.server = SimpleNamespace(sock_path=Path("/tmp/vcpi.sock"), daemon_timeout=1.0)
                payload: dict[str, object] = {"ok": True, "route": {"channel": body["channel"]}}

                with mock.patch.object(
                    web.VcpiWebHandler,
                    "_read_secure_optional_json_body",
                    return_value=body,
                ), mock.patch.object(
                    web.VcpiWebHandler,
                    "_validate_post_security",
                    return_value=None,
                ), mock.patch.object(
                    web,
                    "execute_json_operation",
                    return_value=SimpleNamespace(payload=payload),
                ) as execute_json_operation, mock.patch.object(web, "_send_json") as send_json:
                    handler.do_POST()

                execute_json_operation.assert_called_once_with(
                    operation,
                    expected_payload,
                    Path("/tmp/vcpi.sock"),
                    daemon_timeout=1.0,
                )
                send_json.assert_called_once_with(handler, web.HTTPStatus.OK, payload)

    def test_typed_web_midi_routes_reject_invalid_payload_before_daemon(self) -> None:
        cases = [
            ("/api/midi/link", {"channel": 0, "slot": 1}),
            ("/api/midi/link", {"channel": 17, "slot": 1}),
            ("/api/midi/link", {"channel": "1", "slot": 1}),
            ("/api/midi/link", {"channel": True, "slot": 1}),
            ("/api/midi/link", {"channel": 1, "slot": 0}),
            ("/api/midi/link", {"channel": 1, "slot": 9}),
            ("/api/midi/link", {"channel": 1, "slot": "1"}),
            ("/api/midi/link", {"channel": 1, "slot": False}),
            ("/api/midi/cut", {"channel": 0}),
            ("/api/midi/cut", {"channel": 17}),
            ("/api/midi/cut", {"channel": "1"}),
            ("/api/midi/cut", {"channel": True}),
        ]

        for path, body in cases:
            with self.subTest(path=path, body=body):
                handler = web.VcpiWebHandler.__new__(web.VcpiWebHandler)
                handler.path = path
                handler.server = SimpleNamespace(sock_path=Path("/tmp/vcpi.sock"), daemon_timeout=1.0)

                with mock.patch.object(
                    web.VcpiWebHandler,
                    "_read_secure_optional_json_body",
                    return_value=body,
                ), mock.patch.object(web, "execute_json_operation") as execute_json_operation, mock.patch.object(
                    web,
                    "_send_json",
                ) as send_json:
                    handler.do_POST()

                execute_json_operation.assert_not_called()
                self.assertEqual(send_json.call_args.args[0], handler)
                self.assertEqual(send_json.call_args.args[1], web.HTTPStatus.BAD_REQUEST)

    def test_typed_web_slot_note_route_maps_to_note_operation(self) -> None:
        handler = web.VcpiWebHandler.__new__(web.VcpiWebHandler)
        handler.path = "/api/slots/1/note"
        handler.server = SimpleNamespace(sock_path=Path("/tmp/vcpi.sock"), daemon_timeout=1.0)
        payload: dict[str, object] = {"ok": True, "note": {"note": 60}}

        with mock.patch.object(
            web.VcpiWebHandler,
            "_read_secure_optional_json_body",
            return_value={"note": 60},
        ), mock.patch.object(
            web.VcpiWebHandler,
            "_validate_post_security",
            return_value=None,
        ), mock.patch.object(
            web,
            "execute_json_operation",
            return_value=SimpleNamespace(payload=payload),
        ) as execute_json_operation, mock.patch.object(web, "_send_json") as send_json:
            handler.do_POST()

        execute_json_operation.assert_called_once_with(
            "slot.note",
            {"note": 60, "slot": 1, "velocity": 100, "duration_ms": 300},
            Path("/tmp/vcpi.sock"),
            daemon_timeout=1.0,
        )
        send_json.assert_called_once_with(handler, web.HTTPStatus.OK, payload)

    def test_typed_web_slot_wav_route_maps_to_load_operation(self) -> None:
        handler = web.VcpiWebHandler.__new__(web.VcpiWebHandler)
        handler.path = "/api/slots/2/wav"
        handler.server = SimpleNamespace(sock_path=Path("/tmp/vcpi.sock"), daemon_timeout=1.0)
        payload: dict[str, object] = {"ok": True, "slot": {"slot": 2, "source_type": "wav"}}

        with mock.patch.object(
            web.VcpiWebHandler,
            "_read_secure_optional_json_body",
            return_value={"pack": "808", "sample": " kick.wav ", "name": " Kick "},
        ), mock.patch.object(
            web.VcpiWebHandler,
            "_validate_post_security",
            return_value=None,
        ), mock.patch.object(
            web,
            "execute_json_operation",
            return_value=SimpleNamespace(payload=payload),
        ) as execute_json_operation, mock.patch.object(web, "_send_json") as send_json:
            handler.do_POST()

        execute_json_operation.assert_called_once_with(
            "slot.wav.load",
            {"pack": "808", "sample": "kick", "name": "Kick", "slot": 2},
            Path("/tmp/vcpi.sock"),
            daemon_timeout=1.0,
        )
        send_json.assert_called_once_with(handler, web.HTTPStatus.OK, payload)

    def test_typed_web_slot_wav_rejects_invalid_payload_before_daemon(self) -> None:
        cases = [
            ("/api/slots/9/wav", {"pack": "808", "sample": "kick"}),
            ("/api/slots/1/wav", {"slot": 1, "pack": "808", "sample": "kick"}),
            ("/api/slots/1/wav", {"pack": "808", "sample": "kick", "extra": True}),
            ("/api/slots/1/wav", {"pack": "", "sample": "kick"}),
            ("/api/slots/1/wav", {"pack": "80/8", "sample": "kick"}),
            ("/api/slots/1/wav", {"pack": "808", "sample": "../kick"}),
            ("/api/slots/1/wav", {"pack": "808", "sample": "kick", "name": "Bad/Name"}),
        ]

        for path, body in cases:
            with self.subTest(path=path, body=body):
                handler = web.VcpiWebHandler.__new__(web.VcpiWebHandler)
                handler.path = path
                handler.server = SimpleNamespace(sock_path=Path("/tmp/vcpi.sock"), daemon_timeout=1.0)

                with mock.patch.object(
                    web.VcpiWebHandler,
                    "_read_secure_optional_json_body",
                    return_value=body,
                ), mock.patch.object(web, "execute_json_operation") as execute_json_operation, mock.patch.object(
                    web,
                    "_send_json",
                ) as send_json:
                    handler.do_POST()

                execute_json_operation.assert_not_called()
                self.assertEqual(send_json.call_args.args[0], handler)
                self.assertEqual(send_json.call_args.args[1], web.HTTPStatus.BAD_REQUEST)

    def test_typed_web_slot_wav_requires_csrf(self) -> None:
        handler = web.VcpiWebHandler.__new__(web.VcpiWebHandler)
        handler.path = "/api/slots/1/wav"
        handler.server = SimpleNamespace(sock_path=Path("/tmp/vcpi.sock"), daemon_timeout=1.0)

        with mock.patch.object(
            web.VcpiWebHandler,
            "_validate_post_security",
            side_effect=PermissionError("missing or invalid CSRF token"),
        ), mock.patch.object(web, "execute_json_operation") as execute_json_operation, mock.patch.object(
            web,
            "_send_json",
        ) as send_json:
            handler.do_POST()

        execute_json_operation.assert_not_called()
        send_json.assert_called_once_with(
            handler,
            web.HTTPStatus.FORBIDDEN,
            {"ok": False, "error": "missing or invalid CSRF token"},
        )

    def test_typed_web_slot_note_rejects_invalid_payload_before_daemon(self) -> None:
        cases = [
            ("/api/slots/9/note", {"note": 60}),
            ("/api/slots/1/note", {}),
            ("/api/slots/1/note", {"note": -1}),
            ("/api/slots/1/note", {"note": 128}),
            ("/api/slots/1/note", {"note": "60"}),
            ("/api/slots/1/note", {"note": True}),
            ("/api/slots/1/note", {"note": 60, "velocity": -1}),
            ("/api/slots/1/note", {"note": 60, "velocity": 128}),
            ("/api/slots/1/note", {"note": 60, "velocity": "100"}),
            ("/api/slots/1/note", {"note": 60, "velocity": False}),
            ("/api/slots/1/note", {"note": 60, "duration_ms": 0}),
            ("/api/slots/1/note", {"note": 60, "duration_ms": 5001}),
            ("/api/slots/1/note", {"note": 60, "duration_ms": 10.5}),
            ("/api/slots/1/note", {"note": 60, "duration_ms": "300"}),
            ("/api/slots/1/note", {"note": 60, "duration_ms": True}),
        ]

        for path, body in cases:
            with self.subTest(path=path, body=body):
                handler = web.VcpiWebHandler.__new__(web.VcpiWebHandler)
                handler.path = path
                handler.server = SimpleNamespace(sock_path=Path("/tmp/vcpi.sock"), daemon_timeout=1.0)

                with mock.patch.object(
                    web.VcpiWebHandler,
                    "_read_secure_optional_json_body",
                    return_value=body,
                ), mock.patch.object(web, "execute_json_operation") as execute_json_operation, mock.patch.object(
                    web,
                    "_send_json",
                ) as send_json:
                    handler.do_POST()

                execute_json_operation.assert_not_called()
                self.assertEqual(send_json.call_args.args[0], handler)
                self.assertEqual(send_json.call_args.args[1], web.HTTPStatus.BAD_REQUEST)

    def test_typed_web_sessions_route_maps_to_sessions_operation(self) -> None:
        handler = web.VcpiWebHandler.__new__(web.VcpiWebHandler)
        handler.path = "/api/sessions"
        handler.server = SimpleNamespace(sock_path=Path("/tmp/vcpi.sock"), daemon_timeout=1.0)
        payload: dict[str, object] = {"ok": True, "sessions": []}

        with mock.patch.object(
            web,
            "execute_json_operation",
            return_value=SimpleNamespace(payload=payload),
        ) as execute_json_operation, mock.patch.object(web, "_send_json") as send_json:
            handler.do_GET()

        execute_json_operation.assert_called_once_with(
            "sessions",
            {},
            Path("/tmp/vcpi.sock"),
            daemon_timeout=1.0,
        )
        send_json.assert_called_once_with(handler, web.HTTPStatus.OK, payload)

    def test_typed_web_audio_devices_route_maps_to_read_only_operation(self) -> None:
        handler = web.VcpiWebHandler.__new__(web.VcpiWebHandler)
        handler.path = "/api/audio/devices"
        handler.server = SimpleNamespace(sock_path=Path("/tmp/vcpi.sock"), daemon_timeout=1.0)
        payload: dict[str, object] = {"ok": True, "devices": []}

        with mock.patch.object(
            web,
            "execute_json_operation",
            return_value=SimpleNamespace(payload=payload),
        ) as execute_json_operation, mock.patch.object(web, "_send_json") as send_json:
            handler.do_GET()

        execute_json_operation.assert_called_once_with(
            "audio.devices",
            {},
            Path("/tmp/vcpi.sock"),
            daemon_timeout=1.0,
        )
        send_json.assert_called_once_with(handler, web.HTTPStatus.OK, payload)

    def test_typed_web_flow_route_maps_to_read_only_operation(self) -> None:
        handler = web.VcpiWebHandler.__new__(web.VcpiWebHandler)
        handler.path = "/api/flow"
        handler.server = SimpleNamespace(sock_path=Path("/tmp/vcpi.sock"), daemon_timeout=1.0)
        payload: dict[str, object] = {"ok": True, "flow": "vcpi Signal Flow\n"}

        with mock.patch.object(
            web,
            "execute_json_operation",
            return_value=SimpleNamespace(payload=payload),
        ) as execute_json_operation, mock.patch.object(web, "_send_json") as send_json:
            handler.do_GET()

        execute_json_operation.assert_called_once_with(
            "flow",
            {},
            Path("/tmp/vcpi.sock"),
            daemon_timeout=1.0,
        )
        send_json.assert_called_once_with(handler, web.HTTPStatus.OK, payload)

    def test_typed_web_samples_route_maps_to_read_only_operation(self) -> None:
        handler = web.VcpiWebHandler.__new__(web.VcpiWebHandler)
        handler.path = "/api/samples"
        handler.server = SimpleNamespace(sock_path=Path("/tmp/vcpi.sock"), daemon_timeout=1.0)
        payload: dict[str, object] = {"ok": True, "packs": [], "samples": {}}

        with mock.patch.object(
            web,
            "execute_json_operation",
            return_value=SimpleNamespace(payload=payload),
        ) as execute_json_operation, mock.patch.object(web, "_send_json") as send_json:
            handler.do_GET()

        execute_json_operation.assert_called_once_with(
            "samples",
            {},
            Path("/tmp/vcpi.sock"),
            daemon_timeout=1.0,
        )
        send_json.assert_called_once_with(handler, web.HTTPStatus.OK, payload)

    def test_typed_web_slot_info_route_maps_to_read_only_operation(self) -> None:
        handler = web.VcpiWebHandler.__new__(web.VcpiWebHandler)
        handler.path = "/api/slots/1/info"
        handler.server = SimpleNamespace(sock_path=Path("/tmp/vcpi.sock"), daemon_timeout=1.0)
        payload: dict[str, object] = {
            "ok": True,
            "slot": {"slot": 1, "loaded": True},
            "instrument": {"name": "Dexed"},
            "effects": [],
            "rendered": "Slot 1: Dexed",
        }

        with mock.patch.object(
            web,
            "execute_json_operation",
            return_value=SimpleNamespace(payload=payload),
        ) as execute_json_operation, mock.patch.object(web, "_send_json") as send_json:
            handler.do_GET()

        execute_json_operation.assert_called_once_with(
            "slot.info",
            {"slot": 1},
            Path("/tmp/vcpi.sock"),
            daemon_timeout=1.0,
        )
        send_json.assert_called_once_with(handler, web.HTTPStatus.OK, payload)

    def test_typed_web_slot_params_route_maps_to_read_only_operation(self) -> None:
        handler = web.VcpiWebHandler.__new__(web.VcpiWebHandler)
        handler.path = "/api/slots/1/params"
        handler.server = SimpleNamespace(sock_path=Path("/tmp/vcpi.sock"), daemon_timeout=1.0)
        payload: dict[str, object] = {
            "ok": True,
            "slot": {"slot": 1, "loaded": True},
            "parameters": [{"name": "cutoff", "value": 0.42}],
            "count": 1,
        }

        with mock.patch.object(
            web,
            "execute_json_operation",
            return_value=SimpleNamespace(payload=payload),
        ) as execute_json_operation, mock.patch.object(web, "_send_json") as send_json:
            handler.do_GET()

        execute_json_operation.assert_called_once_with(
            "slot.params",
            {"slot": 1},
            Path("/tmp/vcpi.sock"),
            daemon_timeout=1.0,
        )
        send_json.assert_called_once_with(handler, web.HTTPStatus.OK, payload)

    def test_typed_web_slot_param_post_route_maps_to_set_operation(self) -> None:
        handler = web.VcpiWebHandler.__new__(web.VcpiWebHandler)
        handler.path = "/api/slots/1/params"
        handler.server = SimpleNamespace(sock_path=Path("/tmp/vcpi.sock"), daemon_timeout=1.0)
        payload: dict[str, object] = {
            "ok": True,
            "slot": {"slot": 1, "loaded": True},
            "parameter": {"name": "cutoff", "value": 0.75},
            "parameters": [{"name": "cutoff", "value": 0.75}],
            "count": 1,
        }

        with mock.patch.object(
            web.VcpiWebHandler,
            "_read_secure_optional_json_body",
            return_value={"name": " cutoff ", "value": 0.75},
        ), mock.patch.object(
            web.VcpiWebHandler,
            "_validate_post_security",
            return_value=None,
        ), mock.patch.object(
            web,
            "execute_json_operation",
            return_value=SimpleNamespace(payload=payload),
        ) as execute_json_operation, mock.patch.object(web, "_send_json") as send_json:
            handler.do_POST()

        execute_json_operation.assert_called_once_with(
            "slot.param.set",
            {"name": "cutoff", "value": 0.75, "slot": 1},
            Path("/tmp/vcpi.sock"),
            daemon_timeout=1.0,
        )
        send_json.assert_called_once_with(handler, web.HTTPStatus.OK, payload)

    def test_typed_web_slot_param_post_effect_route_maps_to_set_operation(self) -> None:
        handler = web.VcpiWebHandler.__new__(web.VcpiWebHandler)
        handler.path = "/api/slots/1/params"
        handler.server = SimpleNamespace(sock_path=Path("/tmp/vcpi.sock"), daemon_timeout=1.0)
        payload: dict[str, object] = {
            "ok": True,
            "slot": {"slot": 1, "loaded": True},
            "effect": {"kind": "effect", "index": 1},
            "parameter": {"name": "mix", "value": 0.5},
        }

        with mock.patch.object(
            web.VcpiWebHandler,
            "_read_secure_optional_json_body",
            return_value={"target": "effect", "effect": 1, "name": " mix ", "value": 0.5},
        ), mock.patch.object(
            web.VcpiWebHandler,
            "_validate_post_security",
            return_value=None,
        ), mock.patch.object(
            web,
            "execute_json_operation",
            return_value=SimpleNamespace(payload=payload),
        ) as execute_json_operation, mock.patch.object(web, "_send_json") as send_json:
            handler.do_POST()

        execute_json_operation.assert_called_once_with(
            "slot.param.set",
            {"target": "effect", "effect": 1, "name": "mix", "value": 0.5, "slot": 1},
            Path("/tmp/vcpi.sock"),
            daemon_timeout=1.0,
        )
        send_json.assert_called_once_with(handler, web.HTTPStatus.OK, payload)

    def test_typed_web_slot_param_post_rejects_invalid_payload_before_daemon(self) -> None:
        cases = [
            ("/api/slots/9/params", {"name": "cutoff", "value": 0.5}),
            ("/api/slots/1/params", {}),
            ("/api/slots/1/params", {"name": "", "value": 0.5}),
            ("/api/slots/1/params", {"name": "   ", "value": 0.5}),
            ("/api/slots/1/params", {"name": 123, "value": 0.5}),
            ("/api/slots/1/params", {"name": "x" * (web.MAX_PARAMETER_NAME_LENGTH + 1), "value": 0.5}),
            ("/api/slots/1/params", {"name": "cutoff"}),
            ("/api/slots/1/params", {"name": "cutoff", "value": "0.5"}),
            ("/api/slots/1/params", {"name": "cutoff", "value": True}),
            ("/api/slots/1/params", {"name": "cutoff", "value": None}),
            ("/api/slots/1/params", {"name": "cutoff", "value": [0.5]}),
            ("/api/slots/1/params", {"name": "cutoff", "value": {"amount": 0.5}}),
            ("/api/slots/1/params", {"name": "cutoff", "value": float("nan")}),
            ("/api/slots/1/params", {"name": "cutoff", "value": float("inf")}),
            ("/api/slots/1/params", {"target": "master", "name": "mix", "value": 0.5}),
            ("/api/slots/1/params", {"target": True, "name": "mix", "value": 0.5}),
            ("/api/slots/1/params", {"target": "effect", "name": "mix", "value": 0.5}),
            ("/api/slots/1/params", {"target": "effect", "effect": 0, "name": "mix", "value": 0.5}),
            ("/api/slots/1/params", {"target": "effect", "effect": True, "name": "mix", "value": 0.5}),
            ("/api/slots/1/params", {"target": "effect", "effect": "1", "name": "mix", "value": 0.5}),
        ]

        for path, body in cases:
            with self.subTest(path=path, body=body):
                handler = web.VcpiWebHandler.__new__(web.VcpiWebHandler)
                handler.path = path
                handler.server = SimpleNamespace(sock_path=Path("/tmp/vcpi.sock"), daemon_timeout=1.0)

                with mock.patch.object(
                    web.VcpiWebHandler,
                    "_read_secure_optional_json_body",
                    return_value=body,
                ), mock.patch.object(web, "execute_json_operation") as execute_json_operation, mock.patch.object(
                    web,
                    "_send_json",
                ) as send_json:
                    handler.do_POST()

                execute_json_operation.assert_not_called()
                self.assertEqual(send_json.call_args.args[0], handler)
                self.assertEqual(send_json.call_args.args[1], web.HTTPStatus.BAD_REQUEST)

    def test_typed_web_slot_param_post_requires_csrf(self) -> None:
        handler = web.VcpiWebHandler.__new__(web.VcpiWebHandler)
        handler.path = "/api/slots/1/params"
        handler.server = SimpleNamespace(sock_path=Path("/tmp/vcpi.sock"), daemon_timeout=1.0)

        with mock.patch.object(
            web.VcpiWebHandler,
            "_validate_post_security",
            side_effect=PermissionError("missing or invalid CSRF token"),
        ), mock.patch.object(web, "execute_json_operation") as execute_json_operation, mock.patch.object(
            web,
            "_send_json",
        ) as send_json:
            handler.do_POST()

        execute_json_operation.assert_not_called()
        send_json.assert_called_once_with(
            handler,
            web.HTTPStatus.FORBIDDEN,
            {"ok": False, "error": "missing or invalid CSRF token"},
        )

    def test_typed_web_slot_fx_clear_route_maps_to_clear_operation(self) -> None:
        handler = web.VcpiWebHandler.__new__(web.VcpiWebHandler)
        handler.path = "/api/slots/1/fx/1/clear"
        handler.server = SimpleNamespace(sock_path=Path("/tmp/vcpi.sock"), daemon_timeout=1.0)
        payload: dict[str, object] = {"ok": True, "slot": {"slot": 1, "effects": 0}}

        with mock.patch.object(
            web.VcpiWebHandler,
            "_read_secure_optional_json_body",
            return_value={},
        ), mock.patch.object(
            web.VcpiWebHandler,
            "_validate_post_security",
            return_value=None,
        ), mock.patch.object(
            web,
            "execute_json_operation",
            return_value=SimpleNamespace(payload=payload),
        ) as execute_json_operation, mock.patch.object(web, "_send_json") as send_json:
            handler.do_POST()

        execute_json_operation.assert_called_once_with(
            "slot.fx.clear",
            {"slot": 1, "effect": 1},
            Path("/tmp/vcpi.sock"),
            daemon_timeout=1.0,
        )
        send_json.assert_called_once_with(handler, web.HTTPStatus.OK, payload)

    def test_typed_web_slot_fx_clear_rejects_invalid_route_and_body_before_daemon(self) -> None:
        cases = [
            ("/api/slots/0/fx/1/clear", {}),
            ("/api/slots/9/fx/1/clear", {}),
            ("/api/slots/nope/fx/1/clear", {}),
            ("/api/slots/1/fx/0/clear", {}),
            ("/api/slots/1/fx/nope/clear", {}),
            ("/api/slots/1/fx/1/clear", {"unexpected": True}),
        ]

        for path, body in cases:
            with self.subTest(path=path, body=body):
                handler = web.VcpiWebHandler.__new__(web.VcpiWebHandler)
                handler.path = path
                handler.server = SimpleNamespace(sock_path=Path("/tmp/vcpi.sock"), daemon_timeout=1.0)

                with mock.patch.object(
                    web.VcpiWebHandler,
                    "_read_secure_optional_json_body",
                    return_value=body,
                ), mock.patch.object(web, "execute_json_operation") as execute_json_operation, mock.patch.object(
                    web,
                    "_send_json",
                ) as send_json:
                    handler.do_POST()

                execute_json_operation.assert_not_called()
                self.assertEqual(send_json.call_args.args[0], handler)
                self.assertEqual(send_json.call_args.args[1], web.HTTPStatus.BAD_REQUEST)

    def test_typed_web_slot_fx_clear_requires_csrf(self) -> None:
        handler = web.VcpiWebHandler.__new__(web.VcpiWebHandler)
        handler.path = "/api/slots/1/fx/1/clear"
        handler.server = SimpleNamespace(sock_path=Path("/tmp/vcpi.sock"), daemon_timeout=1.0)

        with mock.patch.object(
            web.VcpiWebHandler,
            "_validate_post_security",
            side_effect=PermissionError("missing or invalid CSRF token"),
        ), mock.patch.object(web, "execute_json_operation") as execute_json_operation, mock.patch.object(
            web,
            "_send_json",
        ) as send_json:
            handler.do_POST()

        execute_json_operation.assert_not_called()
        send_json.assert_called_once_with(
            handler,
            web.HTTPStatus.FORBIDDEN,
            {"ok": False, "error": "missing or invalid CSRF token"},
        )

    def test_typed_web_master_fx_params_route_maps_to_read_only_operation(self) -> None:
        handler = web.VcpiWebHandler.__new__(web.VcpiWebHandler)
        handler.path = "/api/master/fx/1/params"
        handler.server = SimpleNamespace(sock_path=Path("/tmp/vcpi.sock"), daemon_timeout=1.0)
        payload: dict[str, object] = {
            "ok": True,
            "effect": {"kind": "master_effect", "index": 1},
            "parameters": [{"name": "mix", "value": 0.25}],
            "count": 1,
        }

        with mock.patch.object(
            web,
            "execute_json_operation",
            return_value=SimpleNamespace(payload=payload),
        ) as execute_json_operation, mock.patch.object(web, "_send_json") as send_json, mock.patch.object(
            web.VcpiWebHandler,
            "_validate_post_security",
        ) as validate_post_security:
            handler.do_GET()

        validate_post_security.assert_not_called()
        execute_json_operation.assert_called_once_with(
            "master.fx.params",
            {"effect": 1},
            Path("/tmp/vcpi.sock"),
            daemon_timeout=1.0,
        )
        send_json.assert_called_once_with(handler, web.HTTPStatus.OK, payload)

    def test_typed_web_master_fx_param_post_route_maps_to_set_operation(self) -> None:
        handler = web.VcpiWebHandler.__new__(web.VcpiWebHandler)
        handler.path = "/api/master/fx/1/params"
        handler.server = SimpleNamespace(sock_path=Path("/tmp/vcpi.sock"), daemon_timeout=1.0)
        payload: dict[str, object] = {
            "ok": True,
            "effect": {"kind": "master_effect", "index": 1},
            "parameter": {"name": "mix", "value": 0.5},
        }

        with mock.patch.object(
            web.VcpiWebHandler,
            "_read_secure_optional_json_body",
            return_value={"name": " mix ", "value": 0.5},
        ), mock.patch.object(
            web.VcpiWebHandler,
            "_validate_post_security",
            return_value=None,
        ), mock.patch.object(
            web,
            "execute_json_operation",
            return_value=SimpleNamespace(payload=payload),
        ) as execute_json_operation, mock.patch.object(web, "_send_json") as send_json:
            handler.do_POST()

        execute_json_operation.assert_called_once_with(
            "master.fx.param.set",
            {"name": "mix", "value": 0.5, "effect": 1},
            Path("/tmp/vcpi.sock"),
            daemon_timeout=1.0,
        )
        send_json.assert_called_once_with(handler, web.HTTPStatus.OK, payload)

    def test_typed_web_master_fx_param_rejects_invalid_routes_and_payloads_before_daemon(self) -> None:
        cases = [
            ("GET", "/api/master/fx/0/params", {}),
            ("GET", "/api/master/fx/nope/params", {}),
            ("POST", "/api/master/fx/0/params", {"name": "mix", "value": 0.5}),
            ("POST", "/api/master/fx/1/params", {}),
            ("POST", "/api/master/fx/1/params", {"name": "", "value": 0.5}),
            ("POST", "/api/master/fx/1/params", {"name": "   ", "value": 0.5}),
            ("POST", "/api/master/fx/1/params", {"name": 123, "value": 0.5}),
            ("POST", "/api/master/fx/1/params", {"name": "x" * (web.MAX_PARAMETER_NAME_LENGTH + 1), "value": 0.5}),
            ("POST", "/api/master/fx/1/params", {"name": "mix"}),
            ("POST", "/api/master/fx/1/params", {"name": "mix", "value": "0.5"}),
            ("POST", "/api/master/fx/1/params", {"name": "mix", "value": True}),
            ("POST", "/api/master/fx/1/params", {"name": "mix", "value": None}),
            ("POST", "/api/master/fx/1/params", {"name": "mix", "value": [0.5]}),
            ("POST", "/api/master/fx/1/params", {"name": "mix", "value": {"amount": 0.5}}),
            ("POST", "/api/master/fx/1/params", {"name": "mix", "value": float("nan")}),
            ("POST", "/api/master/fx/1/params", {"name": "mix", "value": float("inf")}),
            ("POST", "/api/master/fx/1/params", {"target": "effect", "name": "mix", "value": 0.5}),
            ("POST", "/api/master/fx/1/params", {"slot": 1, "name": "mix", "value": 0.5}),
            ("POST", "/api/master/fx/1/params", {"effect": 1, "name": "mix", "value": 0.5}),
        ]

        for method, path, body in cases:
            with self.subTest(method=method, path=path, body=body):
                handler = web.VcpiWebHandler.__new__(web.VcpiWebHandler)
                handler.path = path
                handler.server = SimpleNamespace(sock_path=Path("/tmp/vcpi.sock"), daemon_timeout=1.0)

                with mock.patch.object(
                    web.VcpiWebHandler,
                    "_read_secure_optional_json_body",
                    return_value=body,
                ), mock.patch.object(web, "execute_json_operation") as execute_json_operation, mock.patch.object(
                    web,
                    "_send_json",
                ) as send_json:
                    if method == "GET":
                        handler.do_GET()
                    else:
                        handler.do_POST()

                execute_json_operation.assert_not_called()
                self.assertEqual(send_json.call_args.args[0], handler)
                self.assertEqual(send_json.call_args.args[1], web.HTTPStatus.BAD_REQUEST)

    def test_typed_web_master_fx_param_post_requires_csrf(self) -> None:
        handler = web.VcpiWebHandler.__new__(web.VcpiWebHandler)
        handler.path = "/api/master/fx/1/params"
        handler.server = SimpleNamespace(sock_path=Path("/tmp/vcpi.sock"), daemon_timeout=1.0)

        with mock.patch.object(
            web.VcpiWebHandler,
            "_validate_post_security",
            side_effect=PermissionError("missing or invalid CSRF token"),
        ), mock.patch.object(web, "execute_json_operation") as execute_json_operation, mock.patch.object(
            web,
            "_send_json",
        ) as send_json:
            handler.do_POST()

        execute_json_operation.assert_not_called()
        send_json.assert_called_once_with(
            handler,
            web.HTTPStatus.FORBIDDEN,
            {"ok": False, "error": "missing or invalid CSRF token"},
        )

    def test_typed_web_master_fx_clear_route_maps_to_clear_operation(self) -> None:
        handler = web.VcpiWebHandler.__new__(web.VcpiWebHandler)
        handler.path = "/api/master/fx/1/clear"
        handler.server = SimpleNamespace(sock_path=Path("/tmp/vcpi.sock"), daemon_timeout=1.0)
        payload: dict[str, object] = {"ok": True, "status": {"audio": {"master_effects": 0}}}

        with mock.patch.object(
            web.VcpiWebHandler,
            "_read_secure_optional_json_body",
            return_value={},
        ), mock.patch.object(
            web.VcpiWebHandler,
            "_validate_post_security",
            return_value=None,
        ), mock.patch.object(
            web,
            "execute_json_operation",
            return_value=SimpleNamespace(payload=payload),
        ) as execute_json_operation, mock.patch.object(web, "_send_json") as send_json:
            handler.do_POST()

        execute_json_operation.assert_called_once_with(
            "master.fx.clear",
            {"effect": 1},
            Path("/tmp/vcpi.sock"),
            daemon_timeout=1.0,
        )
        send_json.assert_called_once_with(handler, web.HTTPStatus.OK, payload)

    def test_typed_web_master_fx_clear_rejects_invalid_route_and_body_before_daemon(self) -> None:
        cases = [
            ("/api/master/fx/0/clear", {}),
            ("/api/master/fx/nope/clear", {}),
            ("/api/master/fx/1/clear", {"unexpected": True}),
        ]

        for path, body in cases:
            with self.subTest(path=path, body=body):
                handler = web.VcpiWebHandler.__new__(web.VcpiWebHandler)
                handler.path = path
                handler.server = SimpleNamespace(sock_path=Path("/tmp/vcpi.sock"), daemon_timeout=1.0)

                with mock.patch.object(
                    web.VcpiWebHandler,
                    "_read_secure_optional_json_body",
                    return_value=body,
                ), mock.patch.object(web, "execute_json_operation") as execute_json_operation, mock.patch.object(
                    web,
                    "_send_json",
                ) as send_json:
                    handler.do_POST()

                execute_json_operation.assert_not_called()
                self.assertEqual(send_json.call_args.args[0], handler)
                self.assertEqual(send_json.call_args.args[1], web.HTTPStatus.BAD_REQUEST)

    def test_typed_web_master_fx_clear_requires_csrf(self) -> None:
        handler = web.VcpiWebHandler.__new__(web.VcpiWebHandler)
        handler.path = "/api/master/fx/1/clear"
        handler.server = SimpleNamespace(sock_path=Path("/tmp/vcpi.sock"), daemon_timeout=1.0)

        with mock.patch.object(
            web.VcpiWebHandler,
            "_validate_post_security",
            side_effect=PermissionError("missing or invalid CSRF token"),
        ), mock.patch.object(web, "execute_json_operation") as execute_json_operation, mock.patch.object(
            web,
            "_send_json",
        ) as send_json:
            handler.do_POST()

        execute_json_operation.assert_not_called()
        send_json.assert_called_once_with(
            handler,
            web.HTTPStatus.FORBIDDEN,
            {"ok": False, "error": "missing or invalid CSRF token"},
        )

    def test_typed_web_slot_info_route_rejects_invalid_slot(self) -> None:
        handler = web.VcpiWebHandler.__new__(web.VcpiWebHandler)
        handler.path = "/api/slots/9/info"
        handler.server = SimpleNamespace(sock_path=Path("/tmp/vcpi.sock"), daemon_timeout=1.0)

        with mock.patch.object(web, "execute_json_operation") as execute_json_operation, mock.patch.object(
            web,
            "_send_json",
        ) as send_json:
            handler.do_GET()

        execute_json_operation.assert_not_called()
        self.assertEqual(send_json.call_args.args[0], handler)
        self.assertEqual(send_json.call_args.args[1], web.HTTPStatus.BAD_REQUEST)

    def test_typed_web_slot_params_route_rejects_invalid_slot(self) -> None:
        handler = web.VcpiWebHandler.__new__(web.VcpiWebHandler)
        handler.path = "/api/slots/9/params"
        handler.server = SimpleNamespace(sock_path=Path("/tmp/vcpi.sock"), daemon_timeout=1.0)

        with mock.patch.object(web, "execute_json_operation") as execute_json_operation, mock.patch.object(
            web,
            "_send_json",
        ) as send_json:
            handler.do_GET()

        execute_json_operation.assert_not_called()
        self.assertEqual(send_json.call_args.args[0], handler)
        self.assertEqual(send_json.call_args.args[1], web.HTTPStatus.BAD_REQUEST)

    def test_typed_web_audio_start_accepts_selected_picker_device(self) -> None:
        handler = web.VcpiWebHandler.__new__(web.VcpiWebHandler)
        handler.path = "/api/audio/start"
        handler.server = SimpleNamespace(sock_path=Path("/tmp/vcpi.sock"), daemon_timeout=1.0)
        payload: dict[str, object] = {"ok": True, "status": {"audio": {"running": True}}}

        with mock.patch.object(
            web.VcpiWebHandler,
            "_read_secure_optional_json_body",
            return_value={"device": "USB Interface"},
        ), mock.patch.object(
            web.VcpiWebHandler,
            "_validate_post_security",
            return_value=None,
        ), mock.patch.object(
            web,
            "execute_json_operation",
            return_value=SimpleNamespace(payload=payload),
        ) as execute_json_operation, mock.patch.object(web, "_send_json") as send_json:
            handler.do_POST()

        execute_json_operation.assert_called_once_with(
            "audio.start",
            {"device": "USB Interface"},
            Path("/tmp/vcpi.sock"),
            daemon_timeout=1.0,
        )
        send_json.assert_called_once_with(handler, web.HTTPStatus.OK, payload)

    def test_typed_web_audio_start_rejects_invalid_device_payload(self) -> None:
        handler = web.VcpiWebHandler.__new__(web.VcpiWebHandler)
        handler.path = "/api/audio/start"
        handler.server = SimpleNamespace(sock_path=Path("/tmp/vcpi.sock"), daemon_timeout=1.0)

        with mock.patch.object(
            web.VcpiWebHandler,
            "_read_secure_optional_json_body",
            return_value={"device": True},
        ), mock.patch.object(web, "execute_json_operation") as execute_json_operation, mock.patch.object(
            web,
            "_send_json",
        ) as send_json:
            handler.do_POST()

        execute_json_operation.assert_not_called()
        self.assertEqual(send_json.call_args.args[0], handler)
        self.assertEqual(send_json.call_args.args[1], web.HTTPStatus.BAD_REQUEST)

    def test_session_name_validation_accepts_safe_names(self) -> None:
        self.assertEqual(web.VcpiWebHandler._validate_session_name("demo"), "demo")
        self.assertEqual(web.VcpiWebHandler._validate_session_name("demo.json"), "demo")

    def test_session_name_validation_rejects_unsafe_names(self) -> None:
        unsafe_names = [
            "../x",
            "/tmp/x",
            "a/b",
            ".hidden",
            "",
            "   ",
            "bad name",
            123,
            "a" * 65,
        ]
        for value in unsafe_names:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    web.VcpiWebHandler._validate_session_name(value)


if __name__ == "__main__":
    _ = unittest.main()
