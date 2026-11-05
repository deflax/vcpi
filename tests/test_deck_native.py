"""Deck playback through the real native TrackRunner, without an audio device."""
import gc
import json
import math
from pathlib import Path
import struct
import tempfile
import threading
import time
import unittest
import wave

import numpy as np

from core.audio_deps import require_native


class NativeDeckTests(unittest.TestCase):
    def setUp(self):
        self.backend = require_native()
        self.directory = tempfile.TemporaryDirectory(prefix="vcpi-deck-native-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.runners = []
        self.addCleanup(self.close_runners)
        self.serial = 0

    def close_runners(self):
        for runner in self.runners:
            runner.shutdown()
        self.runners.clear()

    def tone(self, name="tone", rate=48000, seconds=4, frequency=1000, stereo=False):
        path = self.root / (name + ".wav")
        phases = np.arange(int(rate * seconds), dtype=np.float64) * (2 * math.pi * frequency / rate)
        left = np.sin(phases) * 0.25
        audio = np.column_stack((left, left * -0.5)) if stereo else left
        with wave.open(str(path), "wb") as output:
            output.setnchannels(2 if stereo else 1)
            output.setsampwidth(2)
            output.setframerate(rate)
            output.writeframes((audio * 32767).astype("<i2").tobytes())
        return path

    def sparse_wav(self, name, rate, seconds):
        """Large logical duration, without allocating the corresponding test audio."""
        path = self.root / name
        size = int(rate * seconds) * 2
        with path.open("wb") as output:
            output.write(struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + size, b"WAVE",
                                     b"fmt ", 16, 1, 1, rate, rate * 2, 2, 16, b"data", size))
            output.seek(44 + size - 1)
            output.write(b"\0")
        return path

    def catalog(self, paths, ids=None, **initial):
        ids = range(1, len(paths) + 1) if ids is None else ids
        return json.dumps({
            "version": 1, "id": "test-bank", "bank_path": "session:test-bank",
            "tracks": [{"id": f"track-{i}", "path": str(path), "title": f"Title {i}"}
                       for i, path in zip(ids, paths)], **initial})

    def deck(self, paths, rate=48000, frames=512, **initial):
        plugin = self.backend.Plugin(self.catalog(paths, **initial), rate, frames, "deck")
        self.serial += 1
        transport = self.backend.Transport(str(self.root / f"deck-{self.serial}.mmap"), frames, True)
        runner = self.backend.TrackRunner(transport, plugin, rate, frames)
        self.runners.append(runner)
        return plugin, runner, transport

    def await_state(self, plugin, runner, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while True:
            status = plugin.deck_status()
            if predicate(status):
                return status
            self.assertLess(time.monotonic(), deadline, status)
            runner.render_once()
            # This yields to read-ahead only while awaiting an observed state.
            time.sleep(0.001)

    def render(self, runner, blocks):
        return np.concatenate([runner.render_once() for _ in range(blocks)])

    def frequency(self, audio, rate):
        signal = audio[:, 0].astype(np.float64)
        spectrum = np.abs(np.fft.rfft(signal * np.hanning(len(signal))))
        return float(np.argmax(spectrum)) * rate / len(signal)

    def test_constructor_prepares_paused_restore_without_rendering(self):
        path = self.tone()
        plugin, runner, _ = self.deck([path], cue=1.125, pitch=-3.5)
        status = plugin.deck_status()
        self.assertEqual((status["track_id"], status["state"], status["play"]),
                         ("track-1", "ready", False))
        self.assertAlmostEqual(status["position"], 1.125)
        self.assertEqual((status["cue"], status["pitch"], status["nudge"]), (1.125, -3.5, 0))
        np.testing.assert_array_equal(runner.render_once(), 0)
        self.assertEqual(plugin.deck_status()["position"], status["position"])
        self.assertEqual(plugin.parameter("track_select")["range"], (1, 1))
        self.assertEqual(plugin.parameter("pitch")["range"], (-16, 16))
        self.assertEqual(plugin.parameter("nudge")["range"], (-1, 1))
        self.assertEqual(plugin.parameter("play")["kind"], "bool")
        plugin.set_parameter("pitch", 2.25)
        plugin.deck_command("cue_set")
        self.assertEqual(plugin.deck_status()["pitch"], 2.25)
        self.assertEqual(plugin.deck_status()["cue"], 1.125)
        self.assertAlmostEqual(plugin.parameter("pitch")["raw_value"], (2.25 + 16) / 32)
        with self.assertRaisesRegex(ValueError, "explicit session"):
            plugin.get_state()
        with self.assertRaisesRegex(ValueError, "explicit session"):
            plugin.load_state(b"not-a-plugin-state")

    def test_seek_parameter_order_normalization_and_live_duration(self):
        paths = [self.tone("short", seconds=2), self.tone("long", seconds=5)]
        plugin, runner, _ = self.deck(paths, cue=0.5)
        self.assertEqual(list(plugin.parameters()),
                         ["track_select", "seek", "nudge", "play", "pitch", "sync"])
        seek = plugin.parameter("seek")
        self.assertEqual((seek["kind"], seek["range"], seek["units"]), ("float", (0, 2), "s"))
        self.assertAlmostEqual(seek["raw_value"], 0.25)
        self.assertEqual(plugin.get_parameter("seek"), 0.5)
        self.assertEqual(plugin.set_parameter("seek", 1.25), 1.25)
        status = self.await_state(plugin, runner, lambda s: not s["loading"])
        self.assertEqual((status["position"], status["cue"], status["play"]), (1.25, 0.5, False))
        self.assertEqual(plugin.set_parameter("seek", 10), 2)
        self.await_state(plugin, runner, lambda s: not s["loading"])
        self.assertEqual(plugin.parameter("seek")["raw_value"], 1)
        plugin.set_parameter("track_select", 2)
        self.await_state(plugin, runner, lambda s: not s["loading"])
        self.assertEqual(plugin.parameter("seek")["range"], (0, 5))
        self.assertEqual(plugin.parameter("seek")["raw_value"], 0)
        plugin.set_parameter("play", True)
        plugin.set_parameter("seek", 2.5)
        status = self.await_state(plugin, runner, lambda s: not s["loading"])
        self.assertTrue(status["play"])
        self.assertGreaterEqual(plugin.get_parameter("seek"), 2.5)
        for invalid in (float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                plugin.set_parameter("seek", invalid)

    def test_append_and_nonactive_removal_preserve_exact_active_audio(self):
        paths = [self.tone("first"), self.tone("active"), self.tone("appended")]
        edited, runner, _ = self.deck(paths[:2], initial_track=2, cue=0.25, pitch=2, sync=0)
        reference, reference_runner, _ = self.deck(paths[:2], initial_track=2, cue=0.25, pitch=2, sync=0)
        for plugin, owner in ((edited, runner), (reference, reference_runner)):
            plugin.set_parameter("nudge", 0.5)
            plugin.set_parameter("play", True)
            self.render(owner, 3)
        for catalog, ordinal, count in ((self.catalog(paths), 2, 3),
                                         (self.catalog(paths[1:], ids=[2, 3]), 1, 2)):
            before = edited.deck_status()
            edited.update_deck_catalog(catalog)
            after = edited.deck_status()
            for key in ("track_id", "position", "cue", "play", "pitch", "nudge", "sync"):
                self.assertEqual(after[key], before[key], key)
            self.assertEqual((after["track_select"], after["requested_track_select"]), (ordinal, ordinal))
            self.assertFalse(after["loading"])
            self.assertEqual(edited.parameter("track_select")["range"], (1, count))
            np.testing.assert_array_equal(runner.render_once(), reference_runner.render_once())

    def test_removing_active_entry_chooses_next_then_previous_paused_at_zero(self):
        paths = [self.tone("first", seconds=2), self.tone("middle"), self.tone("last", seconds=6)]
        plugin, runner, _ = self.deck(paths, initial_track=2, cue=0.4, pitch=3, sync=0)
        plugin.set_parameter("nudge", 0.75)
        plugin.set_parameter("play", True)
        self.render(runner, 2)
        plugin.update_deck_catalog(self.catalog([paths[0], paths[2]], ids=[1, 3]))
        status = plugin.deck_status()
        self.assertEqual((status["track_id"], status["track_select"]), ("track-3", 2))
        self.assertEqual((status["position"], status["cue"], status["play"], status["nudge"]),
                         (0, 0, False, 0))
        self.assertEqual((status["pitch"], status["sync"], status["duration"]), (3, False, 6))
        self.assertFalse(status["loading"])
        np.testing.assert_array_equal(runner.render_once(), 0)
        plugin.set_parameter("play", True)
        runner.render_once()
        plugin.update_deck_catalog(self.catalog(paths[:1]))
        status = plugin.deck_status()
        self.assertEqual((status["track_id"], status["track_select"], status["position"], status["play"]),
                         ("track-1", 1, 0, False))
        self.assertEqual(plugin.parameter("seek")["range"], (0, 2))

    def test_playlist_edit_cancels_pending_selection_and_seek(self):
        paths = [self.tone("active"), self.tone("pending", rate=96000)]
        plugin, runner, _ = self.deck(paths, cue=0.2)
        plugin.set_parameter("play", True)
        runner.render_once()
        plugin.set_parameter("track_select", 2)
        plugin.update_deck_catalog(self.catalog(paths[:1]))
        status = plugin.deck_status()
        self.assertEqual((status["track_id"], status["requested_track_id"]), ("track-1", "track-1"))
        self.assertEqual((status["track_select"], status["requested_track_select"]), (1, 1))
        self.assertTrue(status["play"])
        self.assertFalse(status["loading"])
        plugin.set_parameter("seek", 3)
        before = plugin.deck_status()["position"]
        plugin.update_deck_catalog(self.catalog(paths))
        status = plugin.deck_status()
        self.assertEqual(status["position"], before)
        self.assertFalse(status["loading"])
        self.render(runner, 4)
        self.assertEqual(plugin.deck_status()["track_id"], "track-1")
        self.assertLess(plugin.deck_status()["position"], 1)
        self.assertTrue(plugin.deck_status()["play"])

    def test_invalid_catalog_and_failed_active_replacement_leave_audio_usable(self):
        paths = [self.tone("active"), self.root / "missing.wav"]
        plugin, runner, _ = self.deck(paths)
        plugin.set_parameter("play", True)
        runner.render_once()
        before = plugin.deck_status()
        for catalog in (self.catalog([]), self.catalog(paths, ids=[1, 1]),
                        self.catalog(paths[::-1]), self.catalog(paths[1:], ids=[2])):
            with self.assertRaises((ValueError, RuntimeError)):
                plugin.update_deck_catalog(catalog)
            status = plugin.deck_status()
            self.assertEqual((status["track_id"], status["position"], status["play"]),
                             (before["track_id"], before["position"], True))
            self.assertEqual(plugin.parameter("track_select")["range"], (1, 2))
        self.assertGreater(np.max(np.abs(runner.render_once())), 0.1)
        plugin.update_deck_catalog(self.catalog(paths[:1]))
        self.assertEqual(plugin.parameter("track_select")["range"], (1, 1))

    def test_catalog_edits_while_native_rendering_preserve_running_transport(self):
        paths = [self.tone("playing", seconds=12), self.tone("extra")]
        plugin, runner, _ = self.deck(paths[:1], sync=0)
        plugin.set_parameter("play", True)
        self.render(runner, 2)
        stop = threading.Event()
        started = threading.Event()
        errors, peaks = [], []

        def render_audio():
            try:
                while not stop.is_set():
                    peaks.append(float(np.max(np.abs(runner.render_once()))))
                    started.set()
                    stop.wait(0.001)
            except BaseException as error:
                errors.append(error)
                started.set()

        thread = threading.Thread(target=render_audio)
        thread.start()
        try:
            self.assertTrue(started.wait(2))
            for _ in range(12):
                plugin.update_deck_catalog(self.catalog(paths))
                plugin.update_deck_catalog(self.catalog(paths[:1]))
                status = plugin.deck_status()
                self.assertEqual((status["track_id"], status["track_select"]), ("track-1", 1))
                self.assertTrue(status["play"])
                self.assertFalse(status["loading"])
                time.sleep(0.002)
        finally:
            stop.set()
            thread.join(3)
        self.assertFalse(thread.is_alive(), "Catalog updates must not deadlock the render owner")
        self.assertEqual(errors, [])
        self.assertGreater(len(peaks), 2)
        self.assertGreater(min(peaks), 0.1)

    def test_generated_mono_stereo_and_different_sample_rates(self):
        for source_rate, stereo in ((32000, False), (44100, True), (96000, False)):
            with self.subTest(source_rate=source_rate, stereo=stereo):
                path = self.tone(str(source_rate), rate=source_rate, stereo=stereo)
                plugin, runner, transport = self.deck([path])
                plugin.set_parameter("play", True)
                self.render(runner, 5)
                audio = self.render(runner, 16)
                self.assertGreater(float(np.max(np.abs(audio))), 0.20)
                self.assertAlmostEqual(self.frequency(audio, 48000), 1000, delta=6)
                np.testing.assert_allclose(audio[:, 1], audio[:, 0] * (-0.5 if stereo else 1), atol=1e-4)
                self.assertEqual(transport.stats()["failure"], "")
                self.assertGreater(transport.stats()["progress"], 0)
                self.assertAlmostEqual(plugin.deck_status()["position"], 21 * 512 / 48000, places=7)

    def test_pcm24_and_float_wav_decoding(self):
        rate = 48000
        samples = np.sin(np.arange(rate // 2) * (2 * math.pi * 1000 / rate)) * 0.25
        pcm = np.round(samples * 8388607).astype(np.int32)
        pcm_bytes = np.column_stack((pcm & 255, (pcm >> 8) & 255, (pcm >> 16) & 255)).astype(np.uint8).tobytes()
        for name, encoding, bits, data in (("pcm24", 1, 24, pcm_bytes),
                                           ("float32", 3, 32, samples.astype("<f4").tobytes())):
            with self.subTest(encoding=name):
                path = self.root / f"{name}.wav"
                width = bits // 8
                path.write_bytes(struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + len(data), b"WAVE",
                                             b"fmt ", 16, encoding, 1, rate, rate * width, width, bits,
                                             b"data", len(data)) + data)
                plugin, runner, _ = self.deck([path])
                plugin.set_parameter("play", True)
                self.render(runner, 5)
                audio = self.render(runner, 8)
                self.assertAlmostEqual(float(np.sqrt(np.mean(audio[:, 0] ** 2))), 0.25 / math.sqrt(2), delta=0.002)
                np.testing.assert_array_equal(audio[:, 0], audio[:, 1])

    def test_pause_preserves_cursor_and_ramps_without_autoplay(self):
        plugin, runner, _ = self.deck([self.tone()])
        plugin.set_parameter("play", True)
        self.render(runner, 10)
        plugin.set_parameter("play", False)
        paused = plugin.deck_status()["position"]
        first = runner.render_once()
        self.assertEqual(plugin.deck_status()["position"], paused)
        np.testing.assert_array_equal(runner.render_once(), 0)
        self.assertLess(float(np.max(np.abs(np.diff(first[:, 0])))), 0.01)
        plugin.set_parameter("play", True)
        runner.render_once()
        self.assertAlmostEqual(plugin.deck_status()["position"] - paused, 512 / 48000, places=7)
        plugin.deck_command("play_toggle")
        plugin.deck_command("play_toggle")
        self.assertTrue(plugin.deck_status()["play"])

    def test_pitch_nudge_frequency_cursor_and_release(self):
        plugin, runner, _ = self.deck([self.tone(seconds=8)])
        plugin.set_parameter("play", True)
        plugin.set_parameter("pitch", 8)
        self.render(runner, 24)
        start = plugin.deck_status()["position"]
        pitched = self.render(runner, 24)
        self.assertAlmostEqual(self.frequency(pitched, 48000), 1080, delta=5)
        self.assertAlmostEqual(plugin.deck_status()["position"] - start, 24 * 512 / 48000 * 1.08, delta=1e-5)
        plugin.set_parameter("nudge", 1)
        self.render(runner, 16)
        start = plugin.deck_status()["position"]
        nudged = self.render(runner, 24)
        self.assertAlmostEqual(self.frequency(nudged, 48000), 1101.6, delta=5)
        self.assertAlmostEqual(plugin.deck_status()["position"] - start, 24 * 512 / 48000 * 1.08 * 1.02, delta=1e-5)
        plugin.set_parameter("nudge", 0)
        self.render(runner, 16)
        released = self.render(runner, 24)
        self.assertAlmostEqual(self.frequency(released, 48000), 1080, delta=5)

    def test_seek_cue_eof_and_restart_at_cue(self):
        plugin, runner, _ = self.deck([self.tone(seconds=2)])
        plugin.deck_command("seek", 0.75)
        self.await_state(plugin, runner, lambda s: not s["loading"] and abs(s["position"] - 0.75) < 1e-8)
        plugin.deck_command("cue_set")
        plugin.set_parameter("play", True)
        self.render(runner, 10)
        plugin.deck_command("cue")
        status = self.await_state(plugin, runner, lambda s: not s["loading"] and not s["play"])
        self.assertAlmostEqual(status["cue"], 0.75)
        self.assertAlmostEqual(status["position"], 0.75)
        plugin.set_parameter("play", True)
        plugin.deck_command("seek", 1.95)
        self.await_state(plugin, runner, lambda s: not s["loading"] and s["position"] >= 1.95)
        self.await_state(plugin, runner, lambda s: s["ended"])
        status = plugin.deck_status()
        self.assertEqual((status["position"], status["duration"], status["play"]), (2, 2, False))
        self.render(runner, 4)
        self.assertEqual(plugin.deck_status()["position"], 2)
        plugin.set_parameter("play", True)
        status = self.await_state(plugin, runner, lambda s: not s["loading"] and not s["ended"])
        self.assertTrue(status["play"])
        self.assertGreater(status["position"], 0.75)
        self.assertLess(status["position"], 0.80)
        plugin.deck_command("seek", -10)
        plugin.set_parameter("play", False)
        status = self.await_state(plugin, runner, lambda s: not s["loading"])
        self.assertEqual(status["position"], 0)

    def test_restore_and_seek_at_duration_remain_ended_not_silently_playing(self):
        plugin, runner, _ = self.deck([self.tone(seconds=2)], cue=2)
        status = plugin.deck_status()
        self.assertTrue(status["ended"])
        self.assertFalse(status["play"])
        plugin.set_parameter("play", True)
        status = self.await_state(plugin, runner, lambda s: not s["loading"])
        self.assertEqual(status["position"], 2)
        self.assertTrue(status["ended"])
        self.assertFalse(status["play"])
        plugin.deck_command("seek", 0)
        self.await_state(plugin, runner, lambda s: not s["loading"])
        plugin.set_parameter("play", True)
        runner.render_once()
        plugin.deck_command("seek", 2)
        status = self.await_state(plugin, runner, lambda s: not s["loading"])
        self.assertEqual(status["position"], 2)
        self.assertTrue(status["ended"])
        self.assertFalse(status["play"])

    def test_newest_selection_failed_load_and_active_cancellation(self):
        paths = [self.tone("a"), self.tone("b", rate=96000), self.tone("c", frequency=700), self.root / "missing.wav"]
        plugin, runner, _ = self.deck(paths)
        plugin.set_parameter("pitch", -2)
        plugin.set_parameter("play", True)
        self.render(runner, 2)
        plugin.set_parameter("track_select", 2)
        plugin.set_parameter("track_select", 3)
        status = self.await_state(plugin, runner, lambda s: not s["loading"])
        self.assertEqual((status["track_id"], status["requested_track_id"]), ("track-3", "track-3"))
        self.assertEqual((status["play"], status["position"], status["cue"], status["pitch"], status["nudge"]),
                         (False, 0, 0, -2, 0))
        plugin.set_parameter("play", True)
        self.render(runner, 2)
        before = plugin.deck_status()["position"]
        plugin.set_parameter("track_select", 4)
        status = self.await_state(plugin, runner, lambda s: bool(s["error"]))
        self.assertEqual(status["track_id"], "track-3")
        self.assertEqual(status["requested_track_id"], "track-4")
        self.assertTrue(status["play"])
        self.assertGreaterEqual(status["position"], before)
        plugin.set_parameter("track_select", 2)
        plugin.set_parameter("track_select", 3)
        status = self.await_state(plugin, runner, lambda s: not s["loading"])
        self.render(runner, 12)
        self.assertEqual(plugin.deck_status()["track_id"], "track-3")
        self.assertTrue(plugin.deck_status()["play"])
        self.assertEqual(status["error"], "")

    def test_alias_rejection_above_output_nyquist_at_maximum_speed(self):
        low, low_runner, _ = self.deck([self.tone("pass", rate=192000, frequency=1000)])
        high, high_runner, _ = self.deck([self.tone("stop", rate=192000, frequency=30000)])
        samples = []
        for plugin, runner in ((low, low_runner), (high, high_runner)):
            plugin.set_parameter("pitch", 16)
            plugin.set_parameter("nudge", 1)
            plugin.set_parameter("play", True)
            self.render(runner, 20)
            audio = self.render(runner, 20)
            samples.append(float(np.sqrt(np.mean(audio[:, 0] ** 2))))
        self.assertGreater(samples[0], 0.15)
        self.assertLess(samples[1] / samples[0], 0.01, "Require at least 40 dB rejection of a would-be aliased tone")

    def test_large_ratio_starvation_freezes_and_recovers_without_wrapping(self):
        # At 96 source frames/output frame, this unpaced offline request consumes
        # several rings before the decoding worker can produce them. This is a
        # real shortage, not an injected alternate rendering implementation.
        path = self.sparse_wav("fast-source.wav", 768000, 20)
        plugin, runner, _ = self.deck([path], rate=8000, frames=65536)
        plugin.set_parameter("play", True)
        runner.render_once()
        status = plugin.deck_status()
        self.assertTrue(status["buffering"], status)
        self.assertGreater(status["position"], 0)
        self.assertLess(status["position"], 65536 / 8000)
        self.assertFalse(status["ended"])
        plugin.set_parameter("play", False)
        frozen = status["position"]
        runner.render_once()
        self.assertEqual(plugin.deck_status()["position"], frozen)
        plugin.set_parameter("play", True)
        recovered = self.await_state(plugin, runner, lambda s: s["position"] > frozen)
        self.assertLess(recovered["position"], recovered["duration"])
        plugin.deck_command("seek", 1.25)
        plugin.set_parameter("play", False)
        status = self.await_state(plugin, runner, lambda s: not s["loading"])
        self.assertEqual(status["position"], 1.25)

    def test_long_sparse_track_seek_and_constructor_failures(self):
        long_track = self.sparse_wav("long.wav", 48000, 3600)
        plugin, runner, _ = self.deck([long_track], cue=3599.5)
        self.assertEqual(plugin.deck_status()["duration"], 3600)
        self.assertEqual(plugin.deck_status()["position"], 3599.5)
        np.testing.assert_array_equal(runner.render_once(), 0)
        with self.assertRaises((RuntimeError, ValueError)):
            self.deck([self.root / "missing.wav"])
        invalid = self.root / "surround.wav"
        with wave.open(str(invalid), "wb") as output:
            output.setnchannels(3)
            output.setsampwidth(2)
            output.setframerate(48000)
            output.writeframes(b"\0" * 600)
        with self.assertRaises(ValueError):
            self.deck([invalid])
        # A failed constructor must not leave a joinable read-ahead thread.
        healthy, healthy_runner, _ = self.deck([self.tone("healthy")])
        healthy.set_parameter("play", True)
        self.assertGreater(np.max(np.abs(healthy_runner.render_once())), 0.1)

    def test_two_decks_are_isolated_and_repeated_teardown_completes(self):
        paths = [self.tone("a"), self.tone("b", frequency=700)]
        first, first_runner, _ = self.deck(paths)
        second, second_runner, _ = self.deck(paths)
        second.set_parameter("play", True)
        first.set_parameter("track_select", 2)
        self.await_state(first, first_runner, lambda s: not s["loading"])
        self.render(second_runner, 4)
        self.assertEqual(second.deck_status()["track_id"], "track-1")
        self.assertTrue(second.deck_status()["play"])
        self.assertGreater(second.deck_status()["position"], 0)
        deadline = time.monotonic() + 10
        for _ in range(8):
            plugin, runner, transport = self.deck(paths)
            plugin.set_parameter("track_select", 2)
            runner.shutdown()
            self.runners.remove(runner)
            del plugin, runner, transport
            gc.collect()
            self.assertLess(time.monotonic(), deadline, "Deck teardown must join cancelled preparation")


if __name__ == "__main__":
    unittest.main()
