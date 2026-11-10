"""Current-format session state and rejection boundaries."""

import json
import struct
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from vcpi.host import VcpiCore
from vcpi.models import NUM_TRACKS, TrackFocus
from vcpi.plugin_host import PluginHost
from vcpi.sessions.persistence import restore
from vcpi.sessions.restore import _restore_tracks


class TrackSessionTests(unittest.TestCase):
    def test_current_session_roundtrip_preserves_tracks_routing_and_sequences(self):
        with tempfile.TemporaryDirectory(prefix="vcpi-track-session-") as directory:
            root = Path(directory)
            sample = root / "sample.wav"
            with wave.open(str(sample), "wb") as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(44100)
                audio.writeframes(struct.pack("<h", 4096) * 64)
            path = root / "session.json"
            host = VcpiCore(session_path=str(path))
            try:
                host.load_deck(1, str(sample), "Bass")
                host.engine.set_track_state(1, gain=0.35, muted=True, solo=True)
                host.route(3, 1)
                host.sequencer.set_bank(0, ["C4", "E4"], velocity=73)
                host.sequencer.link(0, 1)
                host.track_focus.select(8)
                host.save_session()
            finally:
                host.shutdown(save=False)
            restored = VcpiCore(session_path=str(path))
            try:
                restored.restore_session(start_audio=False)
                restored.sequencer.stop()
                track = restored.engine.tracks[1]
                self.assertEqual(track.name, "Bass")
                self.assertEqual(track.source_type, "deck")
                self.assertAlmostEqual(track.gain, 0.35)
                self.assertTrue(track.muted)
                self.assertTrue(track.solo)
                self.assertEqual(restored.channel_map, {3: 1})
                self.assertEqual(restored.sequencer.banks[0].linked_track, 1)
                self.assertEqual(restored.sequencer.banks[0].velocity, 73)
                self.assertEqual(restored.track_focus.snapshot()[0], 8)
            finally:
                restored.shutdown(save=False)

    def test_unsupported_versions_reject_before_touching_host_or_file(self):
        with tempfile.TemporaryDirectory(prefix="vcpi-session-version-") as directory:
            path = Path(directory) / "session.json"
            for version in (None, 0, 1, 2, 4, True, 3.0, "3"):
                with self.subTest(version=version):
                    data = {"master_gain": 0.1, "bpm": 99, "tracks": []}
                    if version is not None:
                        data["version"] = version
                    original = json.dumps(data)
                    path.write_text(original)
                    host = Mock()
                    with self.assertRaises(ValueError):
                        restore(host, path)
                    self.assertEqual(host.mock_calls, [])
                    self.assertEqual(path.read_text(), original)

    def test_unsupported_sources_never_load_as_plugins(self):
        for kind in ("plugin", "wav", "unknown-source", None):
            for path in ("/unsupported/source", None):
                with self.subTest(kind=kind, path=path):
                    host = Mock()
                    errors = []
                    entry = {"path": path, "name": "Old source"}
                    if kind is not None:
                        entry["kind"] = kind
                    _restore_tracks(host, {"tracks": [entry]}, errors)
                    self.assertEqual(len(errors), 1)
                    self.assertIn("unsupported track source kind", errors[0])
                    host.restore_patch.assert_not_called()
                    host.restore_deck.assert_not_called()
                    host.engine.set_track_state.assert_not_called()

    def test_patch_sources_restore_backend_before_embedded_state(self):
        for kind, patch_field in (("vcv", "vcv_patch_path"), ("pd", "pd_patch_path")):
            with self.subTest(kind=kind):
                host = Mock()
                entry = {
                    "kind": kind,
                    "path": "/saved/backend.vst3",
                    "name": "Saved patch",
                    patch_field: "/deleted/original.patch",
                    "native_state": "saved native chunk",
                }
                errors = []
                with patch("vcpi.sessions.restore._restore_plugin_state") as restore_state:
                    host.attach_mock(restore_state, "restore_state")
                    _restore_tracks(host, {"tracks": [entry]}, errors)
                self.assertEqual(errors, [])
                host.restore_patch.assert_called_once_with(
                    0, entry["path"], kind, entry["name"])
                host.restore_state.assert_called_once_with(host, 0, entry)
                self.assertEqual(
                    [call[0] for call in host.mock_calls],
                    ["restore_patch", "engine.set_track_state", "restore_state"])
                fields = host.engine.set_track_state.call_args.kwargs
                self.assertEqual(fields[patch_field], entry[patch_field])
                self.assertNotIn("source_type", fields)
                host.load_vcv.assert_not_called()
                host.load_pd.assert_not_called()

    def test_restore_patch_selects_dedicated_backend_without_importing_patch_file(self):
        for kind, resolver in (("vcv", "_resolve_cardinal_path"),
                               ("pd", "_resolve_plugdata_path")):
            with self.subTest(kind=kind):
                owner = PluginHost.__new__(PluginHost)
                owner.sample_rate, owner.buffer_size = 44100, 512
                owner.engine = SimpleNamespace(tracks=[None] * NUM_TRACKS)
                owner._resolve_cardinal_path = Mock(return_value=Path("/saved/Cardinal.vst3"))
                owner._resolve_plugdata_path = Mock(return_value=Path("/saved/plugdata.vst3"))
                with patch("vcpi.plugin_host.NativePlugin") as native:
                    track = owner.restore_patch(0, "/saved/backend.vst3", kind, "Saved patch")
                getattr(owner, resolver).assert_called_once_with("/saved/backend.vst3")
                other = "_resolve_plugdata_path" if kind == "vcv" else "_resolve_cardinal_path"
                getattr(owner, other).assert_not_called()
                self.assertIs(owner.engine.tracks[0], track)
                self.assertEqual(track.source_type, kind)
                self.assertEqual(track.name, "Saved patch")
                native.assert_called_once_with(track.path, 44100, 512, "instrument")
                native.return_value.load_preset.assert_not_called()

    def test_patch_loaders_reject_unsupported_kinds_before_loading_native_plugins(self):
        owner = PluginHost.__new__(PluginHost)
        host = VcpiCore.__new__(VcpiCore)
        with patch("vcpi.plugin_host.NativePlugin") as native:
            for kind in ("plugin", "deck", "unknown", None):
                for loader in (owner.restore_patch, owner._load_patch_plugin, host.restore_patch):
                    with self.subTest(kind=kind, loader=loader.__name__):
                        with self.assertRaisesRegex(ValueError, "unsupported patch source kind"):
                            loader(0, "/unused.vst3", kind)
            native.assert_not_called()

    def test_shared_focus_roundtrip_precedes_device_connections(self):
        with tempfile.TemporaryDirectory(prefix="vcpi-focus-session-") as directory:
            path = Path(directory) / "focus.json"
            host = VcpiCore(session_path=str(path))
            try:
                host.track_focus.select(NUM_TRACKS - 1)
                host.save_session()
                self.assertEqual(json.loads(path.read_text())["focus"], NUM_TRACKS)
                host.track_focus.select(2)
                observed = []
                with patch("vcpi.sessions.restore._restore_connections",
                           side_effect=lambda host, *args, **kwargs:
                           observed.append(host.track_focus.snapshot()[0])):
                    host.restore_session(start_audio=False)
                self.assertEqual(observed, [NUM_TRACKS - 1])
                self.assertEqual(host.beatstep._focused_index(), NUM_TRACKS - 1)
            finally:
                host.shutdown(save=False)

    def test_invalid_focus_rejects_before_mutating_running_host(self):
        with tempfile.TemporaryDirectory(prefix="vcpi-focus-validation-") as directory:
            path = Path(directory) / "invalid.json"
            focus = TrackFocus()
            focus.select(3)
            host = SimpleNamespace(
                track_focus=focus, engine=SimpleNamespace(running=True),
                stop_audio=Mock(), timeline=SimpleNamespace(bpm=123))
            for value in (0, NUM_TRACKS + 1, -1, True, False, 1.0, "1", None):
                with self.subTest(value=value):
                    path.write_text(json.dumps({"version": 3, "focus": value, "bpm": 99}))
                    with self.assertRaisesRegex(ValueError, "focus"):
                        restore(host, path)
                    host.stop_audio.assert_not_called()
                    self.assertTrue(host.engine.running)
                    self.assertEqual(host.timeline.bpm, 123)
                    self.assertEqual(focus.snapshot()[0], 3)
