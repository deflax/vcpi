"""Persistent beat analysis through the real native deck, without an audio device."""
import copy
import gc
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import wave

import numpy as np

from vcpi.audio_deps import require_native


class NativeDeckCacheTests(unittest.TestCase):
    def setUp(self):
        self.backend = require_native()
        self.temporary = tempfile.TemporaryDirectory(prefix="vcpi-deck-cache-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.cache_home = self.root / "cache"
        self.environment = patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.cache_home)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.song = self.tone("song")

    def tone(self, name):
        path = self.root / f"{name}.wav"
        rate = 22050
        audio = np.sin(np.arange(rate, dtype=np.float64) * (2 * np.pi * 1000 / rate)) * 8192
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(rate)
            output.writeframes(audio.astype("<i2").tobytes())
        return path

    def catalog(self, song, bpm_hint=None):
        entry = {"id": "song", "path": str(song), "title": song.stem}
        if bpm_hint is not None:
            entry["bpm_hint"] = bpm_hint
        return json.dumps({"version": 1, "id": "cache-test", "bank_path": "session:cache-test",
                           "sync_filter": False, "tracks": [entry]})

    def load(self, song=None, bpm_hint=None):
        plugin = self.backend.Plugin(self.catalog(song or self.song, bpm_hint), 48000, 512, "deck")
        status = plugin.deck_status()
        self.assertEqual(status["state"], "ready", status)
        self.assertFalse(status["loading"], status)
        del plugin
        gc.collect()
        return status

    def subprocess_status(self, song=None, environment=None, bpm_hint=None):
        script = """
import json, sys
from vcpi.audio_deps import require_native
plugin = require_native().Plugin(sys.argv[1], 48000, 512, "deck")
print(json.dumps(plugin.deck_status()))
"""
        result = subprocess.run([sys.executable, "-c", script, self.catalog(song or self.song, bpm_hint)],
                                cwd=Path(__file__).resolve().parents[1],
                                env=environment, capture_output=True, text=True, timeout=60, check=True)
        status = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(status["state"], "ready", status)
        return status

    def entries(self):
        return sorted((self.cache_home / "vcpi" / "analysis").glob("*.json"))

    def entry(self):
        entries = self.entries()
        self.assertEqual(len(entries), 1, entries)
        return entries[0], json.loads(entries[0].read_text())

    def seed_valid_grid(self, entry, data):
        # A valid persisted analysis is an input, not production instrumentation.
        # Distinctive values demonstrate a cache hit independently of wall time.
        seeded = copy.deepcopy(data)
        seeded.update(bpm=137.25, offset=0.125, confidence=0.875)
        entry.write_text(json.dumps(seeded))
        return seeded

    def assert_grid(self, status, data):
        self.assertEqual(status["bpm"], data["bpm"])
        self.assertEqual(status["beat_offset"], data["offset"])
        self.assertEqual(status["beat_confidence"], data["confidence"])

    def test_sync_filter_does_not_change_analysis_cache_identity_or_contents(self):
        self.load()
        entry, data = self.entry()
        catalog = json.loads(self.catalog(self.song))
        catalog["sync_filter"] = True
        unavailable = entry.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "No sync-ready playlist entries"):
            self.backend.Plugin(json.dumps(catalog), 48000, 512, "deck")
        self.assertEqual(self.entries(), [entry])
        self.assertEqual(entry.read_bytes(), unavailable)
        seeded = self.seed_valid_grid(entry, data)
        ready = entry.read_bytes()
        plugin = self.backend.Plugin(json.dumps(catalog), 48000, 512, "deck")
        self.assertTrue(plugin.deck_status()["sync_filter"])
        self.assert_grid(plugin.deck_status(), seeded)
        catalog["sync_filter"] = False
        plugin.update_deck_catalog(json.dumps(catalog))
        self.assertFalse(plugin.deck_status()["sync_filter"])
        catalog["sync_filter"] = True
        plugin.update_deck_catalog(json.dumps(catalog))
        self.assertTrue(plugin.deck_status()["sync_filter"])
        self.assert_grid(plugin.deck_status(), seeded)
        self.assertEqual(self.entries(), [entry])
        self.assertEqual(entry.read_bytes(), ready)

    def live_deck(self):
        plugin = self.backend.Plugin(self.catalog(self.song), 48000, 512, "deck")
        transport = self.backend.Transport(str(self.root / "reanalyze.mmap"), 512, True)
        runner = self.backend.TrackRunner(transport, plugin, 48000, 512)
        self.addCleanup(runner.shutdown)
        return plugin, runner, transport

    def await_status(self, plugin, runner, predicate):
        deadline = time.monotonic() + 5
        while True:
            status = plugin.deck_status()
            if predicate(status):
                return status
            self.assertLess(time.monotonic(), deadline, status)
            runner.render_once()
            time.sleep(0.001)

    def test_unvisited_entries_reuse_hint_specific_persistent_grids(self):
        expected = {}
        for hint, bpm in ((None, 137.25), (83, 83.25), (174, 166.25)):
            self.load(bpm_hint=hint)
            entry = next(path for path in self.entries()
                         if json.loads(path.read_text())["source"].get("bpm_hint") == hint)
            data = json.loads(entry.read_text())
            data.update(bpm=bpm, offset=0.125, confidence=0.875)
            entry.write_text(json.dumps(data))
            expected[str(hint)] = bpm
        cached = {path: path.read_bytes() for path in self.entries()}
        active = self.tone("active")
        catalog = json.loads(self.catalog(active))
        for hint in (None, 83, 174):
            track = {"id": str(hint), "path": str(self.song), "title": str(hint)}
            if hint is not None:
                track["bpm_hint"] = hint
            catalog["tracks"].append(track)
        plugin = self.backend.Plugin(json.dumps(catalog), 48000, 512, "deck")
        deadline = time.monotonic() + 5
        while True:
            status = plugin.deck_status()
            if all(bpm is not None for bpm in status["song_bpms"].values()):
                break
            self.assertLess(time.monotonic(), deadline, status)
            time.sleep(0.001)
        self.assertEqual(status["song_bpms"], {"song": 0, **expected})
        self.assertEqual(status["track_id"], "song")
        self.assertEqual(status["bpm"], 0)
        self.assertEqual(status["error"], "")
        self.assertFalse(status["play"])
        for path, content in cached.items():
            self.assertEqual(path.read_bytes(), content)
        # Status is a memory snapshot, not a filesystem freshness probe.
        self.song.unlink()
        self.assertEqual(plugin.deck_status()["song_bpms"], status["song_bpms"])
        # A catalog edit DOES revalidate retained source identities, including
        # inactive entries. Removed sources must not keep their old cached BPM.
        plugin.update_deck_catalog(json.dumps(catalog))
        deadline = time.monotonic() + 5
        while True:
            status = plugin.deck_status()
            if all(bpm is not None for bpm in status["song_bpms"].values()):
                break
            self.assertLess(time.monotonic(), deadline, status)
            time.sleep(0.001)
        self.assertEqual(status["song_bpms"], {key: 0 for key in ("song", "None", "83", "174")})
        self.assertEqual(status["error"], "")
        self.assertEqual(status["track_id"], "song")

    def test_hint_validation_rejects_non_numeric_nonfinite_and_out_of_range(self):
        catalog = json.loads(self.catalog(self.song))
        for hint in (True, False, None, "83", [], {}, 59.9, 200.1, float("nan"),
                     float("inf"), -float("inf")):
            with self.subTest(hint=hint):
                catalog["tracks"][0]["bpm_hint"] = hint
                with self.assertRaises((ValueError, RuntimeError)):
                    self.backend.Plugin(json.dumps(catalog), 48000, 512, "deck")
        for hint in (60, 200, 83.25):
            with self.subTest(hint=hint):
                self.assertEqual(self.load(bpm_hint=hint)["bpm"], 0)

    def test_hint_cache_slots_and_embedded_identity_are_isolated_across_loads(self):
        rate = 16000
        audio = np.zeros(rate * 40)
        t = np.arange(int(0.025 * rate)) / rate
        pulse = 0.7 * np.exp(-t * 180) * np.cos(2 * np.pi * 800 * t)
        for beat in np.arange(0.137, 40 - 0.025, 60 / 83):
            begin = round(beat * rate)
            audio[begin:begin + len(pulse)] += pulse
        with wave.open(str(self.song), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(rate)
            output.writeframes((audio * 32767).astype("<i2").tobytes())
        variants = ((None, 83), (174, 166), (83, 83), (174.125, 166))
        snapshots = {}
        for hint, expected in variants:
            with self.subTest(hint=hint):
                status = self.load(bpm_hint=hint)
                self.assertAlmostEqual(status["bpm"], expected, delta=0.2, msg=status)
                entry = next(path for path in self.entries() if path not in snapshots)
                data = json.loads(entry.read_text())
                self.assertEqual(data["source"].get("bpm_hint"), hint)
                if hint is None:
                    self.assertNotIn("bpm_hint", data["source"])
                self.assert_grid(status, data)
                snapshots[entry] = (entry.stat().st_ino, entry.stat().st_mtime_ns, entry.read_bytes())
        self.assertEqual(len(self.entries()), len(variants))
        for hint, expected in reversed(variants):
            self.assertAlmostEqual(self.load(bpm_hint=hint)["bpm"], expected, delta=0.2)
            self.assertAlmostEqual(self.subprocess_status(bpm_hint=hint)["bpm"], expected, delta=0.2)
        # Numeric spelling does not create another hint identity.
        self.assertAlmostEqual(self.load(bpm_hint=83.0)["bpm"], 83, delta=0.2)
        self.assertEqual(set(self.entries()), set(snapshots))
        for entry, before in snapshots.items():
            self.assertEqual((entry.stat().st_ino, entry.stat().st_mtime_ns, entry.read_bytes()), before)
        low = next(path for path in snapshots
                   if json.loads(path.read_text())["source"].get("bpm_hint") == 83)
        high = next(path for path in snapshots
                    if json.loads(path.read_text())["source"].get("bpm_hint") == 174)
        high.write_bytes(low.read_bytes())
        # A correctly named file with the wrong embedded hint must still miss.
        self.assertAlmostEqual(self.load(bpm_hint=174)["bpm"], 166, delta=0.2)
        self.assertEqual(json.loads(high.read_text())["source"]["bpm_hint"], 174)
        self.assertEqual(low.read_bytes(), snapshots[low][2])

    def test_reanalyze_replaces_live_and_disk_grid_without_resetting_deck(self):
        self.load()
        entry, data = self.entry()
        seeded = self.seed_valid_grid(entry, data)
        other = self.tone("other")
        self.load(other)
        other_entry = next(path for path in self.entries() if path != entry)
        other_bytes = other_entry.read_bytes()
        song_before = self.song.stat()

        plugin, runner, transport = self.live_deck()
        self.assert_grid(plugin.deck_status(), seeded)
        plugin.set_parameter("sync", False)
        plugin.set_parameter("pitch", 2.5)
        plugin.set_parameter("nudge", -0.25)
        plugin.set_parameter("phase_offset", -0.125)
        plugin.deck_command("seek", 0.125)
        self.await_status(plugin, runner, lambda status: not status["loading"])
        plugin.deck_command("cue_set")
        plugin.set_parameter("play", True)
        for _ in range(8):
            runner.render_once()
        before = plugin.deck_status()
        self.assertTrue(before["play"])
        plugin.deck_command("reanalyze")
        accepted = plugin.deck_status()
        self.assertFalse(accepted["play"])
        self.assertGreater(accepted["request_id"], before["request_id"])
        self.assertIn(accepted["song_bpms"]["song"], (None, 0))
        self.assert_grid(accepted, seeded)  # Only render may replace the applied grid.
        after = self.await_status(plugin, runner, lambda status: not status["loading"])
        self.assertEqual((after["bpm"], after["beat_offset"], after["beat_confidence"]), (0, 0, 0))
        self.assertEqual(after["song_bpms"], {"song": 0})
        self.assertEqual(after["error"], "")
        self.assertFalse(after["play"])
        self.assertAlmostEqual(after["position"], before["position"])
        for key in ("track_id", "cue", "pitch", "nudge", "sync", "phase_offset"):
            self.assertEqual(after[key], before[key], key)
        fresh = json.loads(entry.read_text())
        self.assertEqual((fresh["bpm"], fresh["offset"], fresh["confidence"]), (0, 0, 0))
        self.assertEqual(fresh["source"], data["source"])
        self.assertEqual(other_entry.read_bytes(), other_bytes)
        song_after = self.song.stat()
        self.assertEqual((song_after.st_mtime_ns, song_after.st_ctime_ns),
                         (song_before.st_mtime_ns, song_before.st_ctime_ns))
        self.assert_grid(self.subprocess_status(), fresh)

    def test_reanalyze_recovers_from_a_cached_no_grid_result(self):
        rate = 16000
        audio = np.zeros(rate * 40, dtype=np.float64)
        t = np.arange(int(0.025 * rate)) / rate
        pulse = 0.7 * np.exp(-t * 180) * np.cos(2 * np.pi * 800 * t)
        for beat in np.arange(0.137, 40 - 0.025, 0.5):
            begin = round(beat * rate)
            audio[begin:begin + len(pulse)] += pulse
        with wave.open(str(self.song), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(rate)
            output.writeframes((audio * 32767).astype("<i2").tobytes())
        self.assertAlmostEqual(self.load()["bpm"], 120, delta=0.15)
        entry, data = self.entry()
        data.update(bpm=0, offset=0, confidence=0)
        entry.write_text(json.dumps(data))
        plugin, runner, transport = self.live_deck()
        self.assertEqual(plugin.deck_status()["bpm"], 0)
        plugin.deck_command("reanalyze")
        after = self.await_status(plugin, runner, lambda status: not status["loading"])
        self.assertAlmostEqual(after["bpm"], 120, delta=0.15)
        self.assertGreater(after["beat_confidence"], 0.65)
        self.assertFalse(after["play"])
        self.assertAlmostEqual(json.loads(entry.read_text())["bpm"], 120, delta=0.15)

    def test_reanalyze_without_disk_entry_still_invalidates_live_grid(self):
        self.load()
        entry, data = self.entry()
        seeded = self.seed_valid_grid(entry, data)
        plugin, runner, transport = self.live_deck()
        self.assert_grid(plugin.deck_status(), seeded)
        entry.unlink()
        plugin.deck_command("reanalyze")
        after = self.await_status(plugin, runner, lambda status: not status["loading"])
        self.assertEqual(after["bpm"], 0)
        self.assertEqual(after["error"], "")
        self.assertEqual(json.loads(entry.read_text())["bpm"], 0)

    def test_reanalyze_reports_cache_removal_failure_without_losing_selected_song(self):
        self.load()
        entry, data = self.entry()
        self.seed_valid_grid(entry, data)
        plugin, runner, transport = self.live_deck()
        entry.unlink()
        entry.mkdir()
        before = plugin.deck_status()
        plugin.set_parameter("play", True)
        plugin.deck_command("reanalyze")
        after = self.await_status(plugin, runner, lambda status: not status["loading"])
        self.assertIn("cache", after["error"])
        self.assertFalse(after["play"])
        self.assertEqual(after["track_id"], before["track_id"])
        self.assertEqual(after["position"], before["position"])
        self.assertTrue(entry.is_dir())


    def test_no_grid_is_persisted_and_reused_across_processes(self):
        cold = self.load()
        self.assertEqual((cold["bpm"], cold["beat_offset"], cold["beat_confidence"]), (0, 0, 0))
        entry, data = self.entry()
        self.assert_grid(cold, data)
        before = entry.stat()
        original = entry.read_bytes()
        self.assert_grid(self.load(), data)
        self.assert_grid(self.subprocess_status(), data)
        after = entry.stat()
        self.assertEqual((after.st_ino, after.st_mtime_ns), (before.st_ino, before.st_mtime_ns))
        self.assertEqual(entry.read_bytes(), original)
        self.assertEqual(stat.S_IMODE(after.st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(entry.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(entry.parent.parent.stat().st_mode), 0o700)
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), ["cache", "song.wav"])

    def test_valid_cached_grid_survives_plugin_and_process_restart(self):
        self.load()
        entry, data = self.entry()
        seeded = self.seed_valid_grid(entry, data)
        self.assert_grid(self.load(), seeded)
        self.assert_grid(self.subprocess_status(), seeded)
        self.assertEqual(json.loads(entry.read_text()), seeded)

    def test_canonical_alias_reuses_same_song_entry(self):
        self.load()
        entry, data = self.entry()
        seeded = self.seed_valid_grid(entry, data)
        alias = self.root / "alias.wav"
        alias.symlink_to(self.song.name)
        self.assert_grid(self.load(alias), seeded)
        self.assertEqual(self.entries(), [entry])

    def test_different_song_cannot_reuse_embedded_identity(self):
        self.load()
        first, data = self.entry()
        seeded = self.seed_valid_grid(first, data)
        other = self.tone("other")
        self.assertEqual(self.load(other)["bpm"], 0)
        entries = self.entries()
        self.assertEqual(len(entries), 2)
        second = next(path for path in entries if path != first)
        second.write_text(json.dumps(seeded))
        self.assertEqual(self.load(other)["bpm"], 0)
        self.assertEqual(json.loads(second.read_text())["source"]["path"], str(other.resolve()))
        self.assert_grid(self.load(), seeded)

    def test_same_size_modification_with_restored_mtime_invalidates(self):
        self.load()
        entry, data = self.entry()
        self.seed_valid_grid(entry, data)
        before = self.song.stat()
        with self.song.open("r+b") as output:
            output.seek(100)
            output.write(b"\0\0")
        os.utime(self.song, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(self.song.stat().st_size, before.st_size)
        self.assertEqual(self.song.stat().st_mtime_ns, before.st_mtime_ns)
        self.assertEqual(self.load()["bpm"], 0)
        self.assertNotEqual(json.loads(entry.read_text())["source"], data["source"])

    def test_replacement_with_same_size_and_mtime_invalidates(self):
        self.load()
        entry, data = self.entry()
        self.seed_valid_grid(entry, data)
        before = self.song.stat()
        replacement = self.tone("replacement")
        os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
        replacement.replace(self.song)
        self.assertEqual(self.song.stat().st_size, before.st_size)
        self.assertEqual(self.song.stat().st_mtime_ns, before.st_mtime_ns)
        self.assertNotEqual(self.song.stat().st_ino, before.st_ino)
        self.assertEqual(self.subprocess_status()["bpm"], 0)
        self.assertNotEqual(json.loads(entry.read_text())["source"], data["source"])

    def test_analyzer_version_change_recomputes(self):
        self.load()
        entry, data = self.entry()
        seeded = self.seed_valid_grid(entry, data)
        seeded["source"]["analyzer_version"] = "retired-detector"
        entry.write_text(json.dumps(seeded))
        self.assertEqual(self.subprocess_status()["bpm"], 0)
        self.assertEqual(json.loads(entry.read_text())["source"]["analyzer_version"],
                         data["source"]["analyzer_version"])

    def test_corruption_and_invalid_values_are_cache_misses(self):
        self.load()
        entry, data = self.entry()
        invalid = ["{truncated", "[]", "null", "x" * 17000]
        for key, value in (("schema", True), ("schema", 99), ("bpm", "137.25"),
                           ("bpm", True), ("bpm", -1), ("bpm", 401), ("bpm", float("nan")),
                           ("offset", -0.1), ("offset", 100), ("offset", float("inf")),
                           ("confidence", "0.9"), ("confidence", -1), ("confidence", 2)):
            altered = copy.deepcopy(data)
            altered.update(bpm=137.25, offset=0.125, confidence=0.875)
            altered[key] = value
            invalid.append(json.dumps(altered))
        inconsistent = copy.deepcopy(data)
        inconsistent.update(bpm=0, offset=0.1, confidence=0.875)
        invalid.append(json.dumps(inconsistent))
        for key, value in (("size", str(data["source"]["size"])), ("inode", 1),
                           ("mtime_nanoseconds", -1), ("path", "/another/song.wav")):
            altered = copy.deepcopy(data)
            altered["source"][key] = value
            invalid.append(json.dumps(altered))
        for content in invalid:
            with self.subTest(content=content[:80]):
                entry.write_text(content)
                self.assertEqual(self.load()["bpm"], 0)
                self.assertEqual(json.loads(entry.read_text()), data)

    def test_missing_entry_recomputes(self):
        self.load()
        entry, data = self.entry()
        entry.unlink()
        self.assert_grid(self.load(), data)
        self.assertEqual(json.loads(entry.read_text()), data)

    def test_unavailable_cache_does_not_prevent_playback(self):
        blocked = self.root / "not-a-directory"
        blocked.write_text("occupied")
        with patch.dict(os.environ, {"XDG_CACHE_HOME": str(blocked)}):
            plugin = self.backend.Plugin(self.catalog(self.song), 48000, 512, "deck")
            self.assertEqual(plugin.deck_status()["state"], "ready")
            transport = self.backend.Transport(str(self.root / "playback.mmap"), 512, True)
            runner = self.backend.TrackRunner(transport, plugin, 48000, 512)
            try:
                plugin.set_parameter("play", True)
                audio = np.concatenate([runner.render_once() for _ in range(8)])
                self.assertGreater(float(np.max(np.abs(audio))), 0.01)
            finally:
                runner.shutdown()
            self.assertEqual(self.subprocess_status()["bpm"], 0)
        self.assertEqual(blocked.read_text(), "occupied")
        self.assertEqual(self.entries(), [])

    def test_relative_xdg_override_uses_platform_home_cache(self):
        home = self.root / "home"
        home.mkdir()
        environment = dict(os.environ, XDG_CACHE_HOME="relative-not-allowed", HOME=str(home))
        self.assertEqual(self.subprocess_status(environment=environment)["bpm"], 0)
        default = home / ("Library/Caches" if sys.platform == "darwin" else ".cache")
        entries = list((default / "vcpi" / "analysis").glob("*.json"))
        self.assertEqual(len(entries), 1)
        self.assertEqual(json.loads(entries[0].read_text())["source"]["path"], str(self.song.resolve()))
        self.assertEqual(self.entries(), [])

    def test_nonregular_cache_entry_does_not_block_or_follow_symlinks(self):
        self.load()
        entry, data = self.entry()
        victim = self.root / "unrelated.json"
        victim.write_text("must not be changed")
        entry.unlink()
        entry.symlink_to(victim)
        self.assertEqual(self.subprocess_status()["bpm"], 0)
        self.assertFalse(entry.is_symlink())
        self.assertEqual(victim.read_text(), "must not be changed")
        self.assertEqual(json.loads(entry.read_text()), data)
        entry.unlink()
        os.mkfifo(entry)
        self.assertEqual(self.subprocess_status()["bpm"], 0)
        self.assertTrue(stat.S_ISREG(entry.stat().st_mode))
        self.assertEqual(json.loads(entry.read_text()), data)

    def test_concurrent_processes_publish_complete_entries(self):
        script = """
import json, sys
from vcpi.audio_deps import require_native
plugin = require_native().Plugin(sys.argv[1], 48000, 512, "deck")
print(json.dumps(plugin.deck_status()))
"""
        processes = [subprocess.Popen([sys.executable, "-c", script, self.catalog(self.song)],
                                      cwd=Path(__file__).resolve().parents[1],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                     for _ in range(3)]
        try:
            for process in processes:
                stdout, stderr = process.communicate(timeout=60)
                self.assertEqual(process.returncode, 0, stderr)
                status = json.loads(stdout.strip().splitlines()[-1])
                self.assertEqual((status["state"], status["bpm"]), ("ready", 0))
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
        entry, data = self.entry()
        self.assert_grid(self.load(), data)
        self.assertEqual(list(entry.parent.iterdir()), [entry])


if __name__ == "__main__":
    unittest.main()
