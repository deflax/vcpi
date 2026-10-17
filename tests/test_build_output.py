"""Build output gating using real subprocesses, without compiling native code."""

import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import tempfile
import unittest

from build_native import FAILURE_TAIL_BYTES, OUTPUT_FRAGMENT_BYTES


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

    def test_successful_non_debug_steps_discard_stdout_and_stderr(self):
        for level in (None, "WARNING", "INFO", "ERROR"):
            with self.subTest(level=level):
                result = self.run_step(
                    "import sys; print('configure progress'); "
                    "print('compiler warning', file=sys.stderr)", level,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, "")

    def test_debug_prefixes_both_streams_and_unterminated_output(self):
        result = self.run_step(
            "import sys; print('configure progress', flush=True); "
            "print('compiler warning', file=sys.stderr, flush=True); "
            "print('unterminated', end='', flush=True)", "debug",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [
            "[build] configure progress", "[build] compiler warning",
            "[build] unterminated",
        ])
        self.assertEqual(result.stderr, "")

    def test_debug_streams_before_child_finishes(self):
        process = subprocess.Popen(
            self.command(
                "import sys; print('ready', flush=True); "
                "sys.stdin.readline(); print('finished', flush=True)"
            ), cwd=ROOT, env=self.environment("DEBUG"), stdin=subprocess.PIPE,
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

    def test_debug_bounds_fragments_without_newlines(self):
        count = OUTPUT_FRAGMENT_BYTES * 3 + 7
        result = self.run_step(f"import os; os.write(1, b'x' * {count})", "DEBUG")
        self.assertEqual(result.returncode, 0, result.stderr)
        fragments = result.stdout.splitlines()
        self.assertTrue(all(line.startswith("[build] ") for line in fragments))
        self.assertTrue(all(len(line) <= OUTPUT_FRAGMENT_BYTES + 8 for line in fragments))
        self.assertEqual(sum(len(line.removeprefix("[build] ")) for line in fragments), count)

    def test_failed_quiet_step_reports_both_streams_and_original_status(self):
        result = self.run_step(
            "import sys; print('configure detail', flush=True); "
            "print('fatal compiler error', file=sys.stderr); sys.exit(7)", "WARNING",
        )
        self.assertEqual(result.returncode, 7)
        self.assertEqual(result.stdout, "")
        self.assertIn("[build] Command failed (7):", result.stderr)
        self.assertIn("configure detail", result.stderr)
        self.assertIn("fatal compiler error", result.stderr)

    def test_failed_quiet_step_keeps_only_bounded_diagnostic_tail(self):
        result = self.run_step(
            "import os; import sys; print('discarded' + '-start', flush=True); "
            f"os.write(1, b'x' * {FAILURE_TAIL_BYTES * 3}); "
            "print('actionable' + '-end', flush=True); sys.exit(9)", "WARNING",
        )
        self.assertEqual(result.returncode, 9)
        self.assertEqual(result.stdout, "")
        self.assertNotIn("discarded-start", result.stderr)
        self.assertIn("actionable-end", result.stderr)
        self.assertIn(f"last {FAILURE_TAIL_BYTES} bytes", result.stderr)
        self.assertLess(len(result.stderr.encode()), FAILURE_TAIL_BYTES + 1024)

    def test_failed_debug_step_keeps_live_diagnostics_and_nonzero_status(self):
        result = self.run_step("print('build failed'); raise SystemExit(11)", "DEBUG")
        self.assertEqual(result.returncode, 11)
        self.assertEqual(result.stdout, "[build] build failed\n")
        self.assertIn("[build] Command failed (11):", result.stderr)

    def test_build_entrypoint_gates_both_cmake_steps_without_log_files(self):
        with tempfile.TemporaryDirectory(prefix="vcpi-build-output-") as directory:
            cmake = Path(directory) / "cmake"
            cmake.write_text(
                f"#!{sys.executable}\n"
                "import os, sys\n"
                "stage = 'build' if '--build' in sys.argv else 'configure'\n"
                "print(stage + ' diagnostics', flush=True)\n"
                "sys.exit(13 if os.environ.get('FAIL_STEP') == stage else 0)\n"
            )
            cmake.chmod(0o755)
            for level, failed_step in ((None, None), ("WARNING", None),
                                       ("DEBUG", None), ("WARNING", "configure"),
                                       ("WARNING", "build")):
                with self.subTest(level=level, failed_step=failed_step):
                    environment = self.environment(level)
                    environment["PATH"] = directory + os.pathsep + environment.get("PATH", "")
                    environment["CMAKE_BUILD_PARALLEL_LEVEL"] = "3"
                    environment.pop("FAIL_STEP", None)
                    if failed_step:
                        environment["FAIL_STEP"] = failed_step
                    result = subprocess.run(
                        [sys.executable, str(ROOT / "build_native.py")],
                        cwd=directory, env=environment, capture_output=True,
                        text=True, timeout=10,
                    )
                    if failed_step:
                        self.assertEqual(result.returncode, 13)
                        self.assertEqual(result.stdout, "")
                        self.assertIn(failed_step + " diagnostics", result.stderr)
                        if failed_step == "configure":
                            self.assertNotIn("build diagnostics", result.stderr)
                    elif level == "DEBUG":
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertIn("[build] configure diagnostics", result.stdout)
                        self.assertIn("[build] build diagnostics", result.stdout)
                        self.assertTrue(all(line.startswith("[build] ")
                                            for line in result.stdout.splitlines()))
                        self.assertEqual(result.stderr, "")
                    else:
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(result.stdout, "")
                        self.assertEqual(result.stderr, "")
                    self.assertEqual(set(Path(directory).iterdir()), {cmake})



if __name__ == "__main__":
    unittest.main()
