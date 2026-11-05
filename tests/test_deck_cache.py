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
import unittest
from unittest.mock import patch
import wave

import numpy as np

from core.audio_deps import require_native


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

    def catalog(self, song):
        return json.dumps({"version": 1, "id": "cache-test", "bank_path": "session:cache-test",
                           "tracks": [{"id": "song", "path": str(song), "title": song.stem}]})

    def load(self, song=None):
        plugin = self.backend.Plugin(self.catalog(song or self.song), 48000, 512, "deck")
        status = plugin.deck_status()
        self.assertEqual(status["state"], "ready", status)
        self.assertFalse(status["loading"], status)
        del plugin
        gc.collect()
        return status

    def subprocess_status(self, song=None, environment=None):
        script = """
import json, sys
from core.audio_deps import require_native
plugin = require_native().Plugin(sys.argv[1], 48000, 512, "deck")
print(json.dumps(plugin.deck_status()))
"""
        result = subprocess.run([sys.executable, "-c", script, self.catalog(song or self.song)],
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
from core.audio_deps import require_native
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
