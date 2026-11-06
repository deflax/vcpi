"""Beat-grid and phase-lock regressions through real native deck audio rendering."""
import json
import math
from pathlib import Path
import tempfile
import time
import unittest
import wave

import numpy as np

from vcpi.audio_deps import require_native


class NativeDeckSyncTests(unittest.TestCase):
    rate = 16000
    frames = 256

    def setUp(self):
        self.backend = require_native()
        self.directory = tempfile.TemporaryDirectory(prefix="vcpi-deck-sync-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.runners = []
        self.addCleanup(self.close_runners)

    def close_runners(self):
        for runner in self.runners:
            runner.shutdown()

    def percussion(self, name, bpm=120, offset=0.137, seconds=40, rate=None, intro=0):
        rate = rate or self.rate
        audio = np.zeros(int(seconds * rate), dtype=np.float64)
        if bpm:
            # A short decaying broadband transient, not a metadata BPM tag.
            t = np.arange(int(0.025 * rate)) / rate
            pulse = 0.7 * np.exp(-t * 180) * np.cos(2 * math.pi * 800 * t)
            for beat in np.arange(intro + offset, seconds - 0.025, 60 / bpm):
                begin = round(beat * rate)
                audio[begin:begin + len(pulse)] += pulse
        path = self.root / f"{name}.wav"
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(rate)
            output.writeframes((audio * 32767).astype("<i2").tobytes())
        return path

    def deck(self, paths, **initial):
        catalog = {"id": "sync-bank", "bank_path": str(self.root / "bank.json"),
                   "tracks": [{"id": str(i), "title": path.stem, "path": str(path)}
                              for i, path in enumerate(paths)], **initial}
        plugin = self.backend.Plugin(json.dumps(catalog), self.rate, self.frames, "deck")
        transport = self.backend.Transport(str(self.root / f"{len(self.runners)}.mmap"), self.frames, True)
        runner = self.backend.TrackRunner(transport, plugin, self.rate, self.frames)
        self.runners.append(runner)
        return plugin, runner

    def render(self, decks, seconds):
        blocks = round(seconds * self.rate / self.frames)
        audio = [[] for _ in decks]
        for block in range(blocks):
            for index, (plugin, runner) in enumerate(decks):
                audio[index].append(runner.render_once()[:, 0])
                if plugin.deck_status()["buffering"]:
                    time.sleep(0.003)
            if block % 4 == 0:
                time.sleep(0.0005)  # Yield to actual read-ahead, not an alternate renderer.
        return [np.concatenate(chunks) for chunks in audio]

    def clock(self, decks, bpm, beat=0):
        for plugin, _ in decks:
            plugin.deck_command("sync_tempo", bpm)
            plugin.deck_command("sync_beat", beat)

    def settle_request(self, deck):
        plugin, runner = deck
        deadline = time.monotonic() + 5
        while plugin.deck_status()["loading"]:
            self.assertLess(time.monotonic(), deadline, plugin.deck_status())
            runner.render_once()
            time.sleep(0.001)
        return plugin.deck_status()

    def pulse_times(self, audio):
        indices = np.flatnonzero(np.abs(audio) > 0.08)
        self.assertGreater(len(indices), 0)
        starts = indices[np.r_[True, np.diff(indices) > self.rate * 0.15]]
        return starts / self.rate

    def test_detects_generated_bpm_and_offset_without_starting(self):
        for bpm, offset in ((90, 0.281), (120, 0.137), (123, 0.193), (150, 0.071)):
            with self.subTest(bpm=bpm):
                plugin, _ = self.deck([self.percussion(str(bpm), bpm, offset)])
                status = plugin.deck_status()
                self.assertAlmostEqual(status["bpm"], bpm, delta=0.15)
                # QM novelty advances at 11.6 ms; the constant fit removes tempo
                # drift, but instrument-dependent onset phase retains that scale.
                self.assertLess(abs(math.remainder(status["beat_offset"] - offset, 60 / bpm)), 0.015)
                self.assertGreater(status["beat_confidence"], 0.65)
                self.assertTrue(status["sync"])
                self.assertFalse(status["sync_active"])
                self.assertFalse(status["play"])
                self.assertEqual(plugin.parameter("sync")["kind"], "bool")

    def test_whole_track_grid_after_seventy_five_second_beatless_intro(self):
        plugin, _ = self.deck([self.percussion("long-intro", bpm=120, offset=0.137,
                                               intro=75, seconds=115)])
        status = plugin.deck_status()
        self.assertAlmostEqual(status["bpm"], 120, delta=0.15)
        self.assertLess(abs(math.remainder(status["beat_offset"] - 0.137, 0.5)), 0.015)
        self.assertGreater(status["beat_confidence"], 0.65)
        self.assertFalse(status["play"])

    def test_grid_and_rendered_sync_are_independent_of_source_sample_rate(self):
        decks = []
        for rate in (16000, 44100, 48000, 96000):
            deck = self.deck([self.percussion(f"source-{rate}", bpm=123, offset=0.193, rate=rate)])
            status = deck[0].deck_status()
            self.assertAlmostEqual(status["bpm"], 123, delta=0.15)
            self.assertLess(abs(math.remainder(status["beat_offset"] - 0.193, 60 / 123)), 0.015)
            self.assertGreater(status["beat_confidence"], 0.65)
            decks.append(deck)
        self.clock(decks, 120)
        for plugin, _ in decks:
            plugin.set_parameter("play", True)
        self.render(decks, 8)
        rendered = self.render(decks, 3)
        reference = self.pulse_times(rendered[0])
        for (plugin, _), audio in zip(decks[1:], rendered[1:]):
            status = plugin.deck_status()
            self.assertTrue(status["sync_active"], status)
            self.assertAlmostEqual(status["bpm"] * status["playback_speed"], 120, delta=0.3)
            pulses = self.pulse_times(audio)
            np.testing.assert_allclose(np.diff(pulses), 0.5, atol=0.012)
            for pulse in reference[1:-1]:
                self.assertLess(float(np.min(np.abs(pulses - pulse))), 0.018)

    def test_kick_grid_survives_eighth_note_hats_and_drives_sync(self):
        for hat_level in (0.1, 0.3, 0.6):
            with self.subTest(hat_level=hat_level):
                seconds = 24
                audio = np.zeros(seconds * self.rate)
                kick_time = np.arange(int(0.12 * self.rate)) / self.rate
                kick = 0.45 * np.exp(-kick_time * 35) * np.sin(2 * math.pi * 65 * kick_time)
                hat_time = np.arange(int(0.04 * self.rate)) / self.rate
                noise = np.random.default_rng(42).standard_normal(len(hat_time))
                hat = hat_level * np.r_[noise[0], np.diff(noise)] * np.exp(-hat_time * 150)
                for spacing, pulse in ((0.5, kick), (0.25, hat)):
                    for beat in np.arange(0.137, seconds - 0.12, spacing):
                        start = round(beat * self.rate)
                        audio[start:start + len(pulse)] += pulse
                path = self.root / f"kick-hats-{hat_level}.wav"
                with wave.open(str(path), "wb") as output:
                    output.setnchannels(1)
                    output.setsampwidth(2)
                    output.setframerate(self.rate)
                    output.writeframes((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())
                deck = self.deck([path])
                plugin, _ = deck
                status = plugin.deck_status()
                self.assertAlmostEqual(status["bpm"], 120, delta=0.15)
                self.assertGreater(status["beat_confidence"], 0.65)
                self.assertLess(abs(math.remainder(status["beat_offset"] - 0.137, 0.5)), 0.03)
                self.clock([deck], 126)
                plugin.set_parameter("play", True)
                self.render([deck], 8)
                status = plugin.deck_status()
                self.assertTrue(status["sync_active"], status)
                self.assertEqual(status["sync_error"], "")
                self.assertAlmostEqual(status["bpm"] * status["playback_speed"], 126, delta=0.3)
                self.assertLess(abs(status["beat_phase_error"]), 0.015)

    def test_sustained_bass_and_aperiodic_noise_do_not_invent_a_grid(self):
        time_axis = np.arange(24 * self.rate) / self.rate
        audio = (0.2 * np.sin(2 * math.pi * 65 * time_axis)
                 + 0.02 * np.random.default_rng(17).standard_normal(len(time_axis)))
        path = self.root / "bass-without-beats.wav"
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(self.rate)
            output.writeframes((audio * 32767).astype("<i2").tobytes())
        plugin, _ = self.deck([path])
        status = plugin.deck_status()
        self.assertEqual(status["bpm"], 0)
        self.assertEqual(status["beat_confidence"], 0)
        self.assertFalse(status["sync_active"])
        self.assertIn("No reliable beat grid", status["sync_error"])

    def test_two_different_tempos_converge_in_audio_and_follow_tempo_change(self):
        decks = [self.deck([self.percussion("slow", 90, 0.281)]),
                 self.deck([self.percussion("fast", 150, 0.071)])]
        self.clock(decks, 120)
        for plugin, _ in decks:
            plugin.set_parameter("play", True)
        self.render(decks, 8)
        audio = self.render(decks, 4)
        pulses = [self.pulse_times(samples) for samples in audio]
        for (plugin, _), times in zip(decks, pulses):
            status = plugin.deck_status()
            self.assertTrue(status["sync_active"], status)
            self.assertEqual(status["sync_error"], "")
            self.assertAlmostEqual(status["bpm"] * status["playback_speed"], 120, delta=0.3)
            self.assertLess(abs(status["beat_phase_error"]), 0.01)
            self.assertGreater(len(times), 5)
            np.testing.assert_allclose(np.diff(times), 0.5, atol=0.012)
        for pulse in pulses[0][1:-1]:
            self.assertLess(float(np.min(np.abs(pulses[1] - pulse))), 0.018)
        self.clock(decks, 135, beat=24)
        self.render(decks, 6)
        for plugin, _ in decks:
            status = plugin.deck_status()
            self.assertAlmostEqual(status["bpm"] * status["playback_speed"], 135, delta=0.3)
            self.assertLess(abs(status["beat_phase_error"]), 0.01)

    def test_synced_nudge_retains_opposite_acoustic_offsets_without_changing_tempo(self):
        path = self.percussion("phase", seconds=60)
        decks = [self.deck([path]) for _ in range(3)]
        self.clock(decks, 120)
        for plugin, _ in decks:
            plugin.set_parameter("play", True)
        self.render(decks, 8)
        for (plugin, _), direction in zip(decks[1:], (1, -1)):
            plugin.set_parameter("nudge", direction)
        self.render(decks, 4)
        for (plugin, runner), direction in zip(decks[1:], (1, -1)):
            self.assertAlmostEqual(plugin.deck_status()["phase_offset"], direction * 0.16, places=8)
            plugin.set_parameter("nudge", 0)
            retained = plugin.deck_status()["phase_offset"]
            # Render the first release block as well as the long settling tail:
            # a smoothed nudge target must not keep moving the saved correction.
            runner.render_once()
            self.assertEqual(plugin.deck_status()["phase_offset"], retained)
        decks[0][1].render_once()
        self.render(decks, 6)
        audio = self.render(decks, 3)
        reference = self.pulse_times(audio[0])
        for (plugin, _), samples, direction in zip(decks[1:], audio[1:], (1, -1)):
            status = plugin.deck_status()
            self.assertAlmostEqual(status["phase_offset"], direction * 0.16, places=8)
            self.assertAlmostEqual(status["bpm"] * status["playback_speed"], 120, delta=0.3)
            self.assertLess(abs(status["beat_phase_error"]), 0.01)
            pulses = self.pulse_times(samples)
            np.testing.assert_allclose(np.diff(pulses), 0.5, atol=0.012)
            for pulse in reference[1:-1]:
                nearest = pulses[np.argmin(np.abs(pulses - pulse))]
                self.assertAlmostEqual(nearest - pulse, -direction * 0.08, delta=0.012)
        reference_status = decks[0][0].deck_status()
        self.assertEqual(reference_status["phase_offset"], 0)
        self.assertAlmostEqual(reference_status["bpm"] * reference_status["playback_speed"], 120, delta=0.3)

    def test_direct_phase_and_reset_slew_audio_without_transport_jumps(self):
        path = self.percussion("direct-phase", seconds=50)
        decks = [self.deck([path]), self.deck([path])]
        self.clock(decks, 120)
        for plugin, _ in decks:
            plugin.set_parameter("play", True)
        self.render(decks, 8)
        plugin, _ = decks[1]
        for offset in (0.25, -0.25, 0):
            with self.subTest(offset=offset):
                before = plugin.deck_status()
                if offset:
                    plugin.set_parameter("phase_offset", offset)
                else:
                    plugin.deck_command("phase_reset")
                after = plugin.deck_status()
                self.assertEqual(after["phase_offset"], offset)
                for key in ("position", "cue", "play", "playback_speed", "request_id"):
                    self.assertEqual(after[key], before[key], key)
                self.render(decks, 6)
                reference, corrected = map(self.pulse_times, self.render(decks, 2))
                status = plugin.deck_status()
                self.assertLess(abs(status["beat_phase_error"]), 0.01)
                self.assertAlmostEqual(status["bpm"] * status["playback_speed"], 120, delta=0.3)
                for pulse in reference[1:-1]:
                    nearest = corrected[np.argmin(np.abs(corrected - pulse))]
                    self.assertAlmostEqual(nearest - pulse, -offset * 0.5, delta=0.012)

    def test_phase_wrap_pause_end_and_sync_toggle_preserve_correction(self):
        path = self.percussion("wrap")
        decks = [self.deck([path], phase_offset=direction * 0.499) for direction in (1, -1)]
        self.clock(decks, 120)
        for (plugin, _), direction in zip(decks, (1, -1)):
            plugin.set_parameter("nudge", direction)
        self.render(decks, 0.4)
        for (plugin, _), direction in zip(decks, (1, -1)):
            self.assertEqual(plugin.deck_status()["phase_offset"], direction * 0.499)
            plugin.set_parameter("play", True)
        self.render(decks, 0.4)
        for (plugin, _), direction in zip(decks, (1, -1)):
            self.assertAlmostEqual(plugin.deck_status()["phase_offset"],
                                   math.remainder(direction * 0.515, 1), places=8)
            plugin.set_parameter("play", False)
            plugin.set_parameter("sync", False)
        frozen = [plugin.deck_status()["phase_offset"] for plugin, _ in decks]
        self.clock(decks, 130)
        self.render(decks, 0.4)
        for deck, phase in zip(decks, frozen):
            plugin, _ = deck
            self.assertEqual(plugin.deck_status()["phase_offset"], phase)
            plugin.set_parameter("sync", True)
            plugin.deck_command("seek", plugin.deck_status()["duration"])
            self.assertTrue(self.settle_request(deck)["ended"])
            plugin.set_parameter("nudge", 1)
        self.render(decks, 0.4)
        for (plugin, _), phase in zip(decks, frozen):
            self.assertEqual(plugin.deck_status()["phase_offset"], phase)

    def test_silence_and_missing_clock_keep_manual_cursor(self):
        for name, bpm in (("silence", 0), ("no-clock", 120)):
            with self.subTest(name=name):
                deck = self.deck([self.percussion(name, bpm)])
                plugin, _ = deck
                if not bpm:
                    self.clock([deck], 120)
                    status = plugin.deck_status()
                    self.assertEqual(status["bpm"], 0)
                    self.assertEqual(status["beat_confidence"], 0)
                    self.assertIn("reliable", status["sync_error"])
                plugin.set_parameter("pitch", 8)
                plugin.set_parameter("play", True)
                plugin.set_parameter("nudge", 1)
                self.render([deck], 0.4)
                start = plugin.deck_status()["position"]
                audio = self.render([deck], 0.4)[0]
                status = plugin.deck_status()
                self.assertFalse(status["sync_active"])
                self.assertAlmostEqual(status["position"] - start, 0.4 * 1.08 * 1.02, delta=0.0001)
                self.assertEqual(status["phase_offset"], 0)
                if not bpm:
                    np.testing.assert_array_equal(audio, 0)

    def test_disable_sync_restores_pitch_nudge_and_catalog_false(self):
        deck = self.deck([self.percussion("manual")], sync=False)
        plugin, _ = deck
        self.assertFalse(plugin.deck_status()["sync"])
        self.clock([deck], 150)
        plugin.set_parameter("sync", True)
        plugin.set_parameter("play", True)
        self.render([deck], 2)
        self.assertTrue(plugin.deck_status()["sync_active"])
        plugin.set_parameter("sync", False)
        plugin.set_parameter("pitch", -8)
        plugin.set_parameter("nudge", 1)
        self.render([deck], 0.4)
        start = plugin.deck_status()["position"]
        self.render([deck], 0.4)
        status = plugin.deck_status()
        self.assertFalse(status["sync_active"])
        self.assertEqual(status["sync_error"], "")
        self.assertAlmostEqual(status["position"] - start, 0.4 * 0.92 * 1.02, delta=0.0001)
        self.assertEqual(status["phase_offset"], 0)

    def test_pause_seek_and_selection_reacquire_grid_phase(self):
        deck = self.deck([self.percussion("first", 120), self.percussion("second", 100, 0.21)])
        plugin, _ = deck
        self.clock([deck], 125)
        plugin.set_parameter("play", True)
        self.render([deck], 3)
        plugin.set_parameter("play", False)
        frozen = plugin.deck_status()["position"]
        self.render([deck], 0.4)
        self.assertEqual(plugin.deck_status()["position"], frozen)
        plugin.deck_command("seek", 2.137)
        status = self.settle_request(deck)
        self.assertAlmostEqual(status["position"], 2.137)
        self.assertAlmostEqual(status["bpm"], 120, delta=0.15)
        plugin.set_parameter("play", True)
        self.render([deck], 6)
        self.assertLess(abs(plugin.deck_status()["beat_phase_error"]), 0.015)
        plugin.set_parameter("track_select", 2)
        status = self.settle_request(deck)
        self.assertEqual(status["track_id"], "1")
        self.assertFalse(status["play"])
        self.assertAlmostEqual(status["bpm"], 100, delta=0.15)
        plugin.set_parameter("play", True)
        self.render([deck], 6)
        self.assertLess(abs(plugin.deck_status()["beat_phase_error"]), 0.015)

    def test_unsupported_sync_ratio_is_explicit_and_manual(self):
        deck = self.deck([self.percussion("unsupported")])
        plugin, _ = deck
        self.clock([deck], 400)
        plugin.set_parameter("play", True)
        plugin.set_parameter("nudge", -1)
        self.render([deck], 0.4)
        status = plugin.deck_status()
        self.assertFalse(status["sync_active"])
        self.assertIn("Unsupported sync speed", status["sync_error"])
        self.assertAlmostEqual(status["position"], 0.4 * 0.98 + 0.0002, delta=0.0001)
        self.assertEqual(status["phase_offset"], 0)
        with self.assertRaises(ValueError):
            plugin.deck_command("sync_tempo", 0)
