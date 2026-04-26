"""Lightweight Phase 2 typed API contract tests.

These tests intentionally use stdlib unittest and fake state only. They avoid
native audio, MIDI, and plugin imports so they can run on development machines
and CI with just Python.
"""

from __future__ import annotations

import importlib
import json
import sys
import threading
import unittest
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
        self.refreshed_slots: list[list[int]] = []

    def start_audio(self, output_device: object | None = None) -> None:
        self.started_with = output_device
        self.engine.start(output_device)

    def stop_audio(self) -> None:
        self.engine.stop()

    def refresh_mixer_leds(self, slots: list[int]) -> None:
        self.refreshed_slots.append(slots)


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


if __name__ == "__main__":
    _ = unittest.main()
