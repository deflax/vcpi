"""Deterministic arithmetic/validity tests, independent of audio hardware."""

import copy
import unittest

from vcpi.benchmark import (
    CpuSample, ProcessSample, aggregate_cpu, audio_delta, cpu_delta,
    histogram_percentile, snapshot_issues, timing_delta,
)


def timing(samples, cpu_samples=None):
    if cpu_samples is None:
        cpu_samples = samples
    histograms = []
    for durations in (samples, cpu_samples):
        histogram = [0] * 64
        for duration in durations:
            histogram[min(63, max(1, duration).bit_length() - 1)] += 1
        histograms.append(histogram)
    return {"coherent": True, "count": len(samples), "cpu_clock_errors": 0,
            "wakeup_ns_sum": sum(samples), "render_ns_sum": sum(samples),
            "render_cpu_ns_sum": sum(cpu_samples),
            "wakeup_histogram": histograms[0], "render_histogram": histograms[0].copy(),
            "render_cpu_histogram": histograms[1]}


def snapshot(at=1, samples=(1, 2, 4), callbacks=10):
    return {
        "daemon_pid": 100, "sampled_at": at,
        "config": {"sample_rate": 48000, "buffer_size": 256, "revision": 0,
                   "output_index": 1, "owner_pid": 101},
        "output": {"running": True, "callback_count": callbacks,
                   "underruns": 0, "render_errors": 0, "deadline_misses": 0},
        "tracks": [{"index": 1, "name": "instrument", "generation": 1,
                    "transport_path": "/tmp/transport-a", "renderer_pid": 102,
                    "worker_pid": 102, "failure": "", "worker_failure": None,
                    "gain": 1.0, "muted": False, "solo": False,
                    "timing_enabled": True, "timing": timing(samples),
                    "wakeup_mode": "notify",
                    "sequence": callbacks, "submitted": callbacks,
                    "progress": callbacks, "misses": 0,
                    "midi_sent": 1, "midi_consumed": 1}],
    }


class CpuArithmeticTests(unittest.TestCase):
    def test_process_sum_uses_wall_seconds_and_one_core_percent(self):
        before = CpuSample(10, (ProcessSample(1, 1, 10, "daemon"),
                                ProcessSample(2, 2, 5, "renderer")))
        after = CpuSample(12, (ProcessSample(1, 1, 10.5, "daemon"),
                               ProcessSample(2, 2, 8, "renderer")))
        result = cpu_delta(before, after)
        self.assertTrue(result["complete"])
        self.assertEqual(result["cpu_seconds"], 3.5)
        self.assertEqual(result["cpu_percent"], 175)
        self.assertEqual([row["cpu_percent"] for row in result["processes"]], [25, 150])

    def test_aggregate_weights_intervals_instead_of_averaging_percentages(self):
        first = cpu_delta(CpuSample(0, (ProcessSample(1, 1, 0, "daemon"),)),
                          CpuSample(1, (ProcessSample(1, 1, 1, "daemon"),)))
        second = cpu_delta(CpuSample(1, (ProcessSample(1, 1, 1, "daemon"),)),
                           CpuSample(4, (ProcessSample(1, 1, 7, "daemon"),)))
        total = aggregate_cpu([{"cpu": first}, {"cpu": second}])
        self.assertEqual(total["cpu_percent"], 175)
        self.assertEqual(total["elapsed_seconds"], 4)
        self.assertEqual(total["processes"][0]["cpu_seconds"], 7)
        self.assertEqual(total["processes"][0]["cpu_percent"], 175)

    def test_process_contributions_share_full_window_denominator(self):
        samples = [
            CpuSample(0, (ProcessSample(1, 1, 0, "Python"), ProcessSample(2, 1, 0, "Python"))),
            CpuSample(1, (ProcessSample(1, 1, 0.25, "Python"), ProcessSample(2, 1, 1, "Python"))),
            CpuSample(4, (ProcessSample(1, 1, 1, "Python"), ProcessSample(2, 1, 7, "Python"))),
        ]
        total = aggregate_cpu([{"cpu": cpu_delta(old, new)}
                               for old, new in zip(samples, samples[1:])])
        self.assertEqual(total["cpu_seconds"], 8)
        self.assertEqual(total["cpu_percent"], 200)
        self.assertEqual([row["cpu_percent"] for row in total["processes"]], [25, 175])
        self.assertEqual(sum(row["cpu_seconds"] for row in total["processes"]), 8)

    def test_pid_reuse_is_not_subtracted_as_one_process(self):
        before = CpuSample(1, (ProcessSample(1, 1, 100, "old"),
                               ProcessSample(2, 1, 4, "stable")))
        after = CpuSample(2, (ProcessSample(1, 2, 1, "new"),
                              ProcessSample(2, 1, 5, "stable")))
        result = cpu_delta(before, after)
        self.assertFalse(result["complete"])
        self.assertIsNone(result["cpu_percent"])
        self.assertEqual(result["observed_cpu_percent"], 100)
        self.assertEqual([row["status"] for row in result["processes"]],
                         ["exited_or_detached", "new", "measured"])
        total = aggregate_cpu([{"cpu": result}])
        self.assertFalse(total["complete"])
        self.assertIsNone(total["cpu_seconds"])
        old, reused, stable = total["processes"]
        self.assertEqual((old["pid"], reused["pid"]), (1, 1))
        self.assertNotEqual(old["create_time"], reused["create_time"])
        for partial in (old, reused):
            self.assertFalse(partial["complete"])
            self.assertIsNone(partial["cpu_percent"])
        self.assertTrue(stable["complete"])
        self.assertEqual(stable["cpu_percent"], 100)

    def test_partial_process_cpu_is_observed_not_reported_as_complete(self):
        samples = [
            CpuSample(0, (ProcessSample(1, 1, 0, "worker"),)),
            CpuSample(1, (ProcessSample(1, 1, 1, "worker"),)),
            CpuSample(2, (), ("access denied",)),
            CpuSample(4, (ProcessSample(1, 1, 3, "worker"),)),
            CpuSample(5, (ProcessSample(1, 1, 5, "worker"),)),
        ]
        total = aggregate_cpu([{"cpu": cpu_delta(old, new)}
                               for old, new in zip(samples, samples[1:])])
        process, = total["processes"]
        self.assertFalse(process["complete"])
        self.assertIsNone(process["cpu_seconds"])
        self.assertIsNone(process["cpu_percent"])
        self.assertEqual(process["observed_cpu_seconds"], 3)
        self.assertEqual(process["observed_cpu_percent"], 60)

    def test_inaccessible_process_and_counter_regression_are_incomplete(self):
        before = CpuSample(1, (ProcessSample(1, 1, 4, "daemon"),))
        after = CpuSample(2, (ProcessSample(1, 1, 3, "daemon"),), ("access denied",))
        result = cpu_delta(before, after)
        self.assertFalse(result["complete"])
        self.assertIn("access denied", result["issues"])
        self.assertTrue(any("regressed" in issue for issue in result["issues"]))


class TimingArithmeticTests(unittest.TestCase):
    def test_interval_deltas_exclude_warmup_and_prior_intervals(self):
        warmup = timing([1] * 1000)
        middle = timing([1] * 1000 + [8, 9, 15])
        end = timing([1] * 1000 + [8, 9, 15, 16, 31])
        first = timing_delta(warmup, middle)
        second = timing_delta(middle, end)
        total = timing_delta(warmup, end)
        self.assertEqual(first["count"], 3)
        self.assertEqual(second["count"], 2)
        self.assertEqual(first["wakeup"]["mean_ns"], 32 / 3)
        self.assertEqual(second["wakeup"]["p50"]["upper_bound_ns"], 31)
        self.assertEqual(total["wakeup"]["histogram"],
                         [a + b for a, b in zip(first["wakeup"]["histogram"],
                                                second["wakeup"]["histogram"])])
        self.assertEqual(total["wakeup"]["p50"]["upper_bound_ns"], 15)
        self.assertEqual(total["wakeup"]["p95"]["upper_bound_ns"], 31)
        self.assertEqual(total["render"]["sum_ns"], 79)

    def test_empty_and_open_ended_percentiles_are_explicit(self):
        self.assertIsNone(histogram_percentile([0] * 64, .99))
        histogram = [0] * 64
        histogram[63] = 1
        self.assertEqual(histogram_percentile(histogram, .99),
                         {"lower_bound_ns": 1 << 63, "upper_bound_ns": None})
        empty = timing_delta(timing([]), timing([]))
        self.assertIsNone(empty["render"]["mean_ns"])
        self.assertIsNone(empty["render_cpu"]["mean_ns"])
        self.assertIsNone(empty["render_wall_minus_cpu_mean_ns"])

    def test_renderer_cpu_deltas_are_separate_from_elapsed_time(self):
        before = timing([100, 200], [20, 30])
        after = timing([100, 200, 400, 600], [20, 30, 80, 120])
        result = timing_delta(before, after)
        self.assertEqual(result["render"]["mean_ns"], 500)
        self.assertEqual(result["render_cpu"]["sum_ns"], 200)
        self.assertEqual(result["render_cpu"]["mean_ns"], 100)
        self.assertEqual(result["render_cpu"]["p95"]["upper_bound_ns"], 127)
        self.assertEqual(result["render_wall_minus_cpu_mean_ns"], 400)
        # Independent clock resolution can produce a small signed difference.
        rounded = timing_delta(timing([]), timing([1], [2]))
        self.assertEqual(rounded["render_wall_minus_cpu_mean_ns"], -1)

    def test_cpu_clock_failures_invalidate_only_the_affected_window(self):
        before = {**timing([1, 1, 1], [1]), "cpu_clock_errors": 2}
        after = {**timing([1, 1, 1, 2], [1, 2]), "cpu_clock_errors": 2}
        self.assertEqual(timing_delta(before, after)["render_cpu"]["count"], 1)
        for errors in (1, 3):
            with self.subTest(errors=errors), self.assertRaises(ValueError):
                timing_delta(before, {**after, "cpu_clock_errors": errors})

    def test_missing_cpu_timing_is_not_treated_as_zero_cpu(self):
        before, after = snapshot(), snapshot(at=2, samples=(1, 2, 4, 8), callbacks=20)
        for field in ("render_cpu_ns_sum", "render_cpu_histogram", "cpu_clock_errors"):
            after["tracks"][0]["timing"].pop(field)
        self.assertTrue(snapshot_issues(before, after))
        result = audio_delta(before, after)
        self.assertTrue(result["issues"])
        self.assertIsNone(result["tracks"][0]["timing"])

    def test_incoherent_regressing_or_mismatched_counters_are_rejected(self):
        before = timing([1])
        for after in (timing([]), {**timing([1, 2]), "coherent": False},
                      {**timing([1, 2]), "count": 5}):
            with self.subTest(after=after), self.assertRaises(ValueError):
                timing_delta(before, after)


class MeasurementValidityTests(unittest.TestCase):
    def test_requested_renderer_policy_change_invalidates_comparison(self):
        before = snapshot()
        before["tracks"][0]["realtime"] = {"requested_policy": "THREAD_TIME_CONSTRAINT"}
        after = copy.deepcopy(before)
        self.assertEqual(snapshot_issues(before, after), [])
        after["tracks"][0]["realtime"]["requested_policy"] = "none"
        self.assertTrue(snapshot_issues(before, after))

    def test_wakeup_mode_changes_or_missing_modes_invalidate_measurement(self):
        before = snapshot()
        after = copy.deepcopy(before)
        after["tracks"][0]["wakeup_mode"] = "poll"
        self.assertTrue(snapshot_issues(before, after))
        self.assertEqual(snapshot_issues(after, after), [])
        del after["tracks"][0]["wakeup_mode"]
        self.assertTrue(audio_delta(before, after)["issues"])

    def test_audio_window_returns_counter_and_timing_deltas(self):
        before = snapshot()
        after = snapshot(at=3, samples=(1, 2, 4, 8, 16), callbacks=110)
        after["output"]["underruns"] = 2
        result = audio_delta(before, after)
        self.assertEqual(result["issues"], [])
        self.assertEqual(result["elapsed_seconds"], 2)
        self.assertEqual(result["output_counters"]["callback_count"], 100)
        self.assertEqual(result["output_counters"]["underruns"], 2)
        self.assertEqual(result["tracks"][0]["timing"]["count"], 2)
        self.assertEqual(result["tracks"][0]["counters"]["progress"], 100)

    def test_invalid_workloads_are_detected_even_if_visible_config_restored(self):
        before = snapshot()
        cases = [
            (("config", "revision"), 2, "lifecycle"),
            (("config", "sample_rate"), 96000, "configuration"),
            (("output", "running"), False, "stopped"),
            (("tracks", 0, "transport_path"), "/tmp/transport-b", "identity"),
            (("tracks", 0, "renderer_pid"), 103, "identity"),
            (("tracks", 0, "generation"), 3, "identity"),
            (("tracks", 0, "worker_failure"), "crashed", "failed"),
            (("tracks", 0, "timing_enabled"), False, "not enabled"),
            (("tracks", 0, "timing", "coherent"), False, "not coherent"),
            (("tracks", 0, "muted"), True, "mixer"),
        ]
        for path, value, expected in cases:
            after = copy.deepcopy(before)
            target = after
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            with self.subTest(path=path):
                self.assertTrue(any(expected in issue for issue in snapshot_issues(before, after)))

    def test_stalled_callback_and_render_errors_invalidate_audio(self):
        before = snapshot()
        after = snapshot(at=2)
        after["output"]["render_errors"] = 1
        issues = audio_delta(before, after)["issues"]
        self.assertIn("audio callbacks did not advance", issues)
        self.assertIn("audio render errors occurred", issues)

    def test_removed_tracks_and_regressing_output_counters_are_explicit(self):
        before = snapshot()
        after = snapshot(at=2, callbacks=2)
        after["tracks"] = []
        issues = audio_delta(before, after)["issues"]
        self.assertIn("track set or renderer identity changed", issues)
        self.assertIn("no tracks available for timing", issues)
        self.assertTrue(any("regressed" in issue for issue in issues))
