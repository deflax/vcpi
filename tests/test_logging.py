"""Runtime logging changes affect emitted records without replacing handlers."""

from collections import deque
import io
import logging
import os
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from core.audio_process import _OwnerRuntime

from core.logging_setup import set_log_level


class RuntimeLoggingTests(unittest.TestCase):
    def test_debug_enable_disable_preserves_handlers_info_and_warnings(self):
        root = logging.getLogger()
        previous_level = root.level
        previous_environment = os.environ.get("LOG_LEVEL")
        output = io.StringIO()
        handler = logging.StreamHandler(output)
        logger = logging.getLogger("vcpi.runtime_logging_test")
        root.addHandler(handler)
        try:
            set_log_level("INFO")
            logger.debug("hidden-before")
            logger.info("info-before")
            logger.warning("warning-before")
            set_log_level("DEBUG")
            logger.debug("visible-debug")
            self.assertEqual(os.environ["LOG_LEVEL"], "DEBUG")
            set_log_level("INFO")
            logger.debug("hidden-after")
            logger.info("info-after")
            logger.warning("warning-after")
            self.assertEqual(os.environ["LOG_LEVEL"], "INFO")
            self.assertEqual(output.getvalue().splitlines(),
                             ["info-before", "warning-before", "visible-debug",
                              "info-after", "warning-after"])
            self.assertIn(handler, root.handlers)
        finally:
            root.removeHandler(handler)
            root.setLevel(previous_level)
            if previous_environment is None:
                os.environ.pop("LOG_LEVEL", None)
            else:
                os.environ["LOG_LEVEL"] = previous_environment

    def test_invalid_level_leaves_logging_and_worker_inheritance_unchanged(self):
        root = logging.getLogger()
        previous_level = root.level
        previous_environment = os.environ.get("LOG_LEVEL")
        with self.assertRaises(ValueError):
            set_log_level("not-a-level")
        self.assertEqual(root.level, previous_level)
        self.assertEqual(os.environ.get("LOG_LEVEL"), previous_environment)


class AppliedKnobLoggingTests(unittest.TestCase):
    def runtime(self):
        track = SimpleNamespace(plugin=None, effects=[], deck={})
        worker = Mock(track=track, failure=None)
        worker.request.return_value = {"result": 0.75}
        track.render_owner = worker
        engine = SimpleNamespace(tracks=[track] + [None] * 7, _applied_updates=deque(),
                                 control_error=None, clear_track=Mock(return_value=track))
        with patch("core.audio_process._discover_devices", return_value=[]):
            runtime = _OwnerRuntime(SimpleNamespace(engine=engine), None, None, None,
                                    SimpleNamespace(value=False))
        runtime.generations[0] = 1
        return runtime, worker

    def test_knob_summaries_wait_for_each_parameters_quiet_and_use_applied_values(self):
        runtime, worker = self.runtime()
        with self.assertLogs("core.audio_process", level="INFO") as captured:
            with patch("core.audio_process.time.monotonic") as clock:
                clock.return_value = 10.0
                runtime.pending_parameters.append((0, 1, "cutoff", 100))
                runtime.apply_track_parameters()
                self.assertEqual(captured.records, [])
                clock.return_value = 10.2
                worker.request.return_value = {"result": 0.9}
                runtime.pending_parameters.append((0, 1, "cutoff", 200))
                runtime.apply_track_parameters()
                clock.return_value = 10.31
                worker.request.return_value = {"result": 7.0}
                runtime.pending_parameters.append((0, 1, "attack", 8))
                runtime.apply_track_parameters()
                self.assertEqual(captured.records, [])
                clock.return_value = 10.51
                runtime.apply_track_parameters()
                self.assertEqual([record.args for record in captured.records],
                                 [(1, "cutoff", 0.9)])
                clock.return_value = 10.62
                runtime.apply_track_parameters()
                self.assertEqual([record.args for record in captured.records],
                                 [(1, "cutoff", 0.9), (1, "attack", 7.0)])
                clock.return_value = 12
                runtime.apply_track_parameters()
                self.assertEqual(len(captured.records), 2)

    def test_failed_and_retired_knob_changes_never_report_success(self):
        runtime, worker = self.runtime()
        with self.assertNoLogs("core.audio_process", level="INFO"):
            with patch("core.audio_process.time.monotonic", return_value=10):
                worker.request.side_effect = ValueError("rejected")
                runtime.pending_parameters.append((0, 1, "cutoff", 100))
                runtime.apply_track_parameters()
                self.assertIn("rejected", worker.failure)
                worker.request.side_effect = None
                runtime.pending_parameters.append((0, 1, "cutoff", 100))
                runtime.apply_track_parameters()
                runtime.generations[0] = 2
                runtime.pending_parameters.append((0, 1, "attack", 800))
                runtime.apply_track_parameters()
                runtime._flush_parameter_logs(force=True)

    def test_shutdown_flushes_final_applied_knob_value_without_waiting(self):
        runtime, worker = self.runtime()
        with self.assertLogs("core.audio_process", level="INFO") as captured:
            with patch("core.audio_process.time.monotonic", return_value=10):
                runtime.pending_parameters.append((0, 1, "cutoff", 100))
                runtime.apply_track_parameters()
                self.assertEqual(captured.records, [])
                runtime.shutdown_tracks()
                self.assertEqual([record.args for record in captured.records],
                                 [(1, "cutoff", 0.75)])


if __name__ == "__main__":
    unittest.main()
