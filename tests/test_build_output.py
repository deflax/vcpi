"""Live build output using real subprocesses, without compiling native code."""

import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import unittest

from build_native import OUTPUT_FRAGMENT_BYTES


ROOT = Path(__file__).resolve().parents[1]
RUN_STEP = """
import json
import subprocess
import sys
from build_native import run_build_step
try:
    run_build_step(json.loads(sys.argv[1]))
except subprocess.CalledProcessError as error:
    sys.exit(error.returncode if error.returncode > 0 else 1)
"""


class BuildOutputTests(unittest.TestCase):
    def environment(self, level=None):
        environment = os.environ.copy()
        if level is None:
            environment.pop("LOG_LEVEL", None)
        else:
            environment["LOG_LEVEL"] = level
        return environment

    def command(self, source):
        return [sys.executable, "-c", RUN_STEP,
                json.dumps([sys.executable, "-c", source])]

    def run_step(self, source, level=None):
        return subprocess.run(
            self.command(source), cwd=ROOT, env=self.environment(level),
            capture_output=True, text=True, timeout=10,
        )

    def test_all_levels_stream_both_streams_and_unterminated_output(self):
        for level in (None, "WARNING", "INFO", "ERROR", "debug"):
            with self.subTest(level=level):
                result = self.run_step(
                    "import sys; print('configure progress', flush=True); "
                    "print('compiler warning', file=sys.stderr, flush=True); "
                    "print('unterminated', end='', flush=True)", level,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.splitlines(), [
                    "[build] configure progress", "[build] compiler warning",
                    "[build] unterminated",
                ])
                self.assertEqual(result.stderr, "")

    def test_default_streams_before_child_finishes(self):
        process = subprocess.Popen(
            self.command(
                "import sys; print('ready', flush=True); "
                "sys.stdin.readline(); print('finished', flush=True)"
            ), cwd=ROOT, env=self.environment(), stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                self.assertTrue(selector.select(timeout=5), "output was not streamed live")
            self.assertEqual(process.stdout.readline(), "[build] ready\n")
            self.assertIsNone(process.poll())
            stdout, stderr = process.communicate("continue\n", timeout=5)
            self.assertEqual(process.returncode, 0, stderr)
            self.assertEqual(stdout, "[build] finished\n")
            self.assertEqual(stderr, "")
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate()

    def test_bounds_fragments_without_newlines(self):
        count = OUTPUT_FRAGMENT_BYTES * 3 + 7
        result = self.run_step(f"import os; os.write(1, b'x' * {count})")
        self.assertEqual(result.returncode, 0, result.stderr)
        fragments = result.stdout.splitlines()
        self.assertTrue(all(line.startswith("[build] ") for line in fragments))
        self.assertTrue(all(len(line) <= OUTPUT_FRAGMENT_BYTES + 8 for line in fragments))
        self.assertEqual(sum(len(line.removeprefix("[build] ")) for line in fragments), count)

    def test_failure_streams_diagnostics_and_preserves_exit_status(self):
        result = self.run_step(
            "import sys; print('configure detail', flush=True); "
            "print('fatal compiler error', file=sys.stderr); sys.exit(7)", "WARNING",
        )
        self.assertEqual(result.returncode, 7)
        self.assertEqual(result.stdout.splitlines(), [
            "[build] configure detail", "[build] fatal compiler error",
        ])
        self.assertIn("[build] Command failed (7):", result.stderr)


if __name__ == "__main__":
    unittest.main()
