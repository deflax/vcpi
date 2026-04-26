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
    def __init__(self, name: str = "Dexed", source_type: str = "vst") -> None:
        self.name: str = name
        self.path: str = "/plugins/Dexed.vst3"
        self.plugin: SimpleNamespace = SimpleNamespace(name=name)
        self.display_label: str = name
        self.source_type: str = source_type
        self.vcv_patch_path: str | None = None
        self.gain: float = 0.5
        self.muted: bool = False
        self.solo: bool = True
        self.enabled: bool = True
        self.midi_channels: set[int] = set()
        self.effects: list[SimpleNamespace] = [SimpleNamespace(name="Room")]


class FakeEngine:
    def __init__(self) -> None:
        self.running: bool = False
        self.slots: list[FakeSlot | None] = [FakeSlot(), None, None, None, None, None, None, None]
        self.master_gain: float = 0.8
        self.master_effects: list[object] = []
        self.routes: dict[int, int] = {1: 0, 10: 0}
        self.output_device: object | None = None

    def start(self, output_device: object | None = None) -> None:
        self.running = True
        self.output_device = output_device

    def stop(self) -> None:
        self.running = False

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
        self.start_link_calls: list[float | None] = []
        self.stop_link_calls: int = 0

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

    def start_link(self, bpm: float | None = None) -> None:
        self.start_link_calls.append(bpm)
        if bpm is not None:
            self.link.bpm = bpm
        self.link.enabled = True

    def stop_link(self) -> None:
        self.stop_link_calls += 1
        self.link.enabled = False


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
        bad_slot_match = web.SLOT_ACTION_RE.fullmatch("/api/slots/9/clear")

        self.assertIsNotNone(clear_match)
        self.assertEqual(clear_match.group(1), "1")
        self.assertEqual(clear_match.group(2), "clear")
        self.assertIsNotNone(unload_match)
        self.assertEqual(unload_match.group(1), "8")
        self.assertEqual(unload_match.group(2), "unload")
        with self.assertRaises(ValueError):
            web.VcpiWebHandler._validate_slot_number(bad_slot_match.group(1))

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
