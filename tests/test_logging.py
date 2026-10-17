"""Runtime logging changes affect emitted records without replacing handlers."""

import io
import logging
import os
import unittest

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


if __name__ == "__main__":
    unittest.main()
