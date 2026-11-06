"""Runtime logging changes affect emitted records without replacing handlers."""

from collections import deque
import io
import logging
import os
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from vcpi.audio.owner import _OwnerRuntime

from vcpi.logging_setup import set_log_level


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
        with patch("vcpi.audio.owner._discover_devices", return_value=[]):
            runtime = _OwnerRuntime(SimpleNamespace(engine=engine), None, None, None,
                                    SimpleNamespace(value=False))
        runtime.generations[0] = 1
        return runtime, worker

    def test_failed_and_retired_knob_changes_never_report_success(self):
        runtime, worker = self.runtime()
        with self.assertNoLogs("vcpi.audio.owner", level="INFO"):
            with patch("vcpi.audio.owner.time.monotonic", return_value=10):
                worker.request.side_effect = ValueError("rejected")
                runtime.pending_parameters.append((0, 1, "cutoff", 100, "parameter", None, None))
                runtime.apply_track_parameters()
                self.assertIn("rejected", worker.failure)
                worker.request.side_effect = None
                runtime.pending_parameters.append((0, 1, "cutoff", 100, "parameter", None, None))
                runtime.apply_track_parameters()
                runtime.generations[0] = 2
                runtime.pending_parameters.append((0, 1, "attack", 800, "parameter", None, None))
                runtime.apply_track_parameters()
                runtime._flush_parameter_logs(force=True)



if __name__ == "__main__":
    unittest.main()
