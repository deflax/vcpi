"""Exercise real process fds and C stdio only inside disposable subprocesses."""

import ctypes
import multiprocessing as mp
from multiprocessing.reduction import DupFd
import os
from pathlib import Path
import select
import subprocess
import sys
import time
import unittest


# Direct script execution is also the isolation harness used by the tests below.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _c_output(stdout, stderr):
    libc = ctypes.CDLL(None)
    libc.fputs.argtypes = [ctypes.c_char_p, ctypes.c_void_p]
    libc.fputs.restype = ctypes.c_int
    names = ("__stdoutp", "__stderrp") if sys.platform == "darwin" else ("stdout", "stderr")
    libc.fputs(stdout, ctypes.c_void_p.in_dll(libc, names[0]))
    libc.fputs(stderr, ctypes.c_void_p.in_dll(libc, names[1]))


def _write_all(descriptor, data):
    while data:
        data = data[os.write(descriptor, data):]


def _identity(descriptor):
    stat = os.fstat(descriptor)
    return stat.st_dev, stat.st_ino


def _scenario_toggle():
    from core.native_output import native_output, set_debug_output

    with native_output("daemon"):
        os.write(1, b"hidden-os-stdout\n")
        os.write(2, b"hidden-os-stderr\n")
        _c_output(b"hidden-c-stdout", b"hidden-c-stderr")
        print("hidden-python", end="")
        set_debug_output(True)
        os.write(1, b"visible-os-stdout\n")
        os.write(2, b"visible-os-stderr\n")
        _c_output(b"visible-c-stdout\n", b"visible-c-stderr\n")
        set_debug_output(False)
        _c_output(b"hidden-late-c", b"hidden-late-stderr")
        os.write(1, b"hidden-late-os\n")
        set_debug_output(True)
        _c_output(b"visible-last\n", b"")


def _scenario_live():
    from core.native_output import native_output, set_debug_output

    with native_output("audio"):
        set_debug_output(True)
        os.write(1, b"os-stdout\n")
        os.write(2, b"os-stderr\n")
        # No explicit fflush: capture must make C stdio line-buffered.
        _c_output(b"c-stdout\n", b"c-stderr\n")
        os.write(1, b"partial")
        if sys.stdin.buffer.readline() != b"continue\n":
            raise AssertionError("parent did not acknowledge live output")


def _scenario_fragments():
    from core.native_output import native_output, set_debug_output

    with native_output("slot 3"):
        set_debug_output(True)
        os.write(1, b"one\npart")
    with native_output("slot 3"):
        os.write(2, b"two\n")
        _write_all(1, b"x" * (128 * 1024))
        os.write(1, b"\ntail")


def _spawn_output(destination, source, nested):
    from core.native_output import native_output, output_destination, set_debug_output

    with native_output(source, destination):
        set_debug_output(True)
        os.write(1, f"{source}-stdout\n".encode())
        _c_output(b"", f"{source}-stderr\n".encode())
        if nested:
            child = mp.get_context("spawn").Process(
                target=_spawn_output, args=(output_destination(), "slot 2", False))
            child.start()
            child.join(10)
            if child.is_alive():
                child.kill()
                child.join()
                raise AssertionError("nested worker failed to exit")
            if child.exitcode != 0:
                raise AssertionError(f"nested worker exit code: {child.exitcode}")


def _scenario_spawn(debug=True):
    from core.native_output import native_output, output_destination

    os.environ["LOG_LEVEL"] = "DEBUG" if debug else "WARNING"
    with native_output("daemon"):
        os.write(1, b"daemon-stdout\n")
        child = mp.get_context("spawn").Process(
            target=_spawn_output, args=(output_destination(), "audio", True))
        child.start()
        child.join(15)
        if child.is_alive():
            child.kill()
            child.join()
            raise AssertionError("audio worker failed to exit")
        if child.exitcode != 0:
            raise AssertionError(f"audio worker exit code: {child.exitcode}")


def _scenario_spawn_off():
    _scenario_spawn(False)


def _scenario_logging():
    import logging
    from core.native_output import logging_stream, native_output, set_debug_output

    before = (_identity(1), _identity(2))
    try:
        with native_output("daemon"):
            logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s",
                                stream=logging_stream(), force=True)
            os.write(1, b"hidden-native\n")
            logging.warning("warning-off")
            set_debug_output(True)
            os.write(2, b"native-on\n")
            logging.error("error-on")
            raise RuntimeError("body failure")
    except RuntimeError:
        pass
    if (_identity(1), _identity(2)) != before:
        raise AssertionError("stdout/stderr not restored after exception")
    logging.warning("warning-after-close")
    os.write(1, b"restored-stdout\n")
    os.write(2, b"restored-stderr\n")


def _scenario_nested_context():
    from core.native_output import native_output, set_debug_output

    with native_output("daemon"):
        set_debug_output(True)
        with native_output("audio"):
            os.write(1, b"nested\n")
        os.write(1, b"outer\n")
    os.write(1, b"restored\n")


def _scenario_failed_setup():
    from unittest.mock import patch
    from core.native_output import native_output, set_debug_output

    before = (_identity(1), _identity(2))
    original = os.dup2
    calls = 0

    def failing_dup2(source, target):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected second-fd setup failure")
        return original(source, target)

    # Fault injection only: all capture I/O and restoration still use real fds.
    with patch("core.native_output.os.dup2", side_effect=failing_dup2):
        try:
            with native_output("daemon"):
                raise AssertionError("setup unexpectedly succeeded")
        except OSError:
            pass
    if (_identity(1), _identity(2)) != before:
        raise AssertionError("partial setup did not restore stdout/stderr")
    with native_output("audio"):
        set_debug_output(True)
        os.write(1, b"recovered\n")
    os.write(2, b"restored-after-setup-failure\n")


def _scenario_closed_terminal():
    from core.native_output import native_output, set_debug_output

    reader, writer = os.pipe()
    os.close(reader)
    try:
        with native_output("audio", DupFd(writer)):
            set_debug_output(True)
            for _ in range(20):
                os.write(1, b"closed-terminal\n")
            time.sleep(0.1)
    finally:
        os.close(writer)
    os.write(1, b"closed-terminal-survived\n")


def _scenario_bounded_cleanup():
    from core.native_output import native_output, set_debug_output

    reader, writer = os.pipe()
    os.set_blocking(writer, False)
    try:
        while True:
            os.write(writer, b"x" * 4096)
    except BlockingIOError:
        pass
    os.set_blocking(writer, True)
    with native_output("audio", DupFd(writer)):
        set_debug_output(True)
        os.write(1, b"blocked-terminal\n")
        time.sleep(0.1)
        started = time.monotonic()
    elapsed = time.monotonic() - started
    if elapsed > 1.5:
        raise AssertionError(f"capture cleanup blocked for {elapsed:.3f}s")
    # A timed-out reader must still own its fds, rather than close reused ones.
    fresh_reader, fresh_writer = os.pipe()
    os.close(reader)
    os.close(writer)
    time.sleep(0.2)
    try:
        os.write(fresh_writer, b"fresh-fds")
        if os.read(fresh_reader, 9) != b"fresh-fds":
            raise AssertionError("timed-out drainer damaged subsequent descriptors")
    finally:
        os.close(fresh_reader)
        os.close(fresh_writer)
    os.write(1, b"bounded-cleanup\n")


def _scenario_no_leaks():
    from core.native_output import native_output, set_debug_output

    before = len(os.listdir("/proc/self/fd")) if sys.platform == "linux" else None
    identities = (_identity(1), _identity(2))
    for _ in range(12):
        with native_output("daemon"):
            set_debug_output(True)
            os.write(1, b"cycle\n")
            set_debug_output(False)
            _c_output(b"hidden-buffered", b"hidden-buffered")
    if (_identity(1), _identity(2)) != identities:
        raise AssertionError("repeated captures changed process fds")
    if before is not None and len(os.listdir("/proc/self/fd")) != before:
        raise AssertionError("repeated captures leaked descriptors")
    os.write(1, b"no-leaks\n")


@unittest.skipUnless(sys.platform in ("linux", "darwin"), "POSIX native stdio capture")
class NativeOutputTests(unittest.TestCase):
    def _command(self, scenario):
        return [sys.executable, str(Path(__file__).resolve()), "--scenario", scenario]

    def _environment(self):
        environment = dict(os.environ, LOG_LEVEL="WARNING")
        environment.pop("PYTHONUNBUFFERED", None)
        return environment

    def _run(self, scenario):
        result = subprocess.run(self._command(scenario), cwd=_ROOT, env=self._environment(),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=25)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        return result.stdout.decode(), result.stderr.decode()

    def test_raw_os_and_c_output_toggles_without_buffered_replay(self):
        stdout, stderr = self._run("toggle")
        self.assertEqual(stderr, "")
        self.assertNotIn("hidden", stdout)
        self.assertCountEqual(stdout.splitlines(), [
            "[daemon] visible-os-stdout", "[daemon] visible-os-stderr",
            "[daemon] visible-c-stdout", "[daemon] visible-c-stderr",
            "[daemon] visible-last",
        ])

    def test_os_and_line_buffered_c_output_arrive_before_context_exits(self):
        process = subprocess.Popen(self._command("live"), cwd=_ROOT, env=self._environment(),
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE)
        try:
            output = bytearray()
            deadline = time.monotonic() + 5
            while output.count(b"\n") < 5 and time.monotonic() < deadline:
                ready, _, _ = select.select([process.stdout], [], [],
                                            max(0, deadline - time.monotonic()))
                if ready:
                    chunk = os.read(process.stdout.fileno(), 4096)
                    if not chunk:
                        break
                    output.extend(chunk)
            self.assertCountEqual(output.decode().splitlines(), [
                "[audio] os-stdout", "[audio] os-stderr",
                "[audio] c-stdout", "[audio] c-stderr",
                "[audio] partial",
            ])
            self.assertIsNone(process.poll(), "producer exited before acknowledgement")
            stdout, stderr = process.communicate(b"continue\n", timeout=5)
            self.assertEqual(process.returncode, 0, stderr.decode(errors="replace"))
            self.assertEqual(stdout, b"")
            self.assertEqual(stderr, b"")
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate()

    def test_partial_lines_and_large_newline_free_output_are_bounded(self):
        stdout, stderr = self._run("fragments")
        self.assertEqual(stderr, "")
        lines = stdout.splitlines()
        self.assertTrue(all(line.startswith("[slot 3] ") for line in lines))
        fragments = [line.removeprefix("[slot 3] ") for line in lines]
        self.assertEqual(fragments[:3], ["one", "part", "two"])
        self.assertEqual(fragments[-1], "tail")
        self.assertTrue(all(len(fragment) <= 4096 for fragment in fragments))
        self.assertEqual("".join(fragments[3:-1]), "x" * (128 * 1024))

    def test_spawned_children_forward_real_terminal_without_parent_prefixes(self):
        stdout, stderr = self._run("spawn")
        self.assertEqual(stderr, "")
        self.assertCountEqual(stdout.splitlines(), [
            "[daemon] daemon-stdout", "[audio] audio-stdout", "[audio] audio-stderr",
            "[slot 2] slot 2-stdout", "[slot 2] slot 2-stderr",
        ])

    def test_spawned_debug_output_bypasses_disabled_parent_devnull(self):
        stdout, stderr = self._run("spawn_off")
        self.assertEqual(stderr, "")
        self.assertCountEqual(stdout.splitlines(), [
            "[audio] audio-stdout", "[audio] audio-stderr",
            "[slot 2] slot 2-stdout", "[slot 2] slot 2-stderr",
        ])

    def test_structured_warnings_survive_capture_and_exception_cleanup(self):
        stdout, stderr = self._run("logging")
        self.assertEqual(stderr, "restored-stderr\n")
        self.assertNotIn("hidden-native", stdout)
        self.assertCountEqual(stdout.splitlines(), [
            "WARNING warning-off", "ERROR error-on", "[daemon] native-on",
            "WARNING warning-after-close", "restored-stdout",
        ])

    def test_nested_context_restores_outer_capture(self):
        stdout, stderr = self._run("nested_context")
        self.assertEqual(stderr, "")
        self.assertCountEqual(stdout.splitlines(), ["[audio] nested", "[daemon] outer", "restored"])

    def test_partial_setup_failure_restores_both_fds(self):
        stdout, stderr = self._run("failed_setup")
        self.assertEqual(stdout, "[audio] recovered\n")
        self.assertEqual(stderr, "restored-after-setup-failure\n")

    def test_closed_terminal_does_not_crash_capture_owner(self):
        stdout, stderr = self._run("closed_terminal")
        self.assertEqual(stdout, "closed-terminal-survived\n")
        self.assertEqual(stderr, "")

    def test_cleanup_is_bounded_with_blocked_terminal_and_no_fd_reuse_race(self):
        stdout, stderr = self._run("bounded_cleanup")
        self.assertEqual(stdout, "bounded-cleanup\n")
        self.assertEqual(stderr, "")

    def test_repeated_capture_closes_descriptors_and_discards_buffered_output(self):
        stdout, stderr = self._run("no_leaks")
        self.assertEqual(stderr, "")
        self.assertEqual(stdout.count("[daemon] cycle\n"), 12)
        self.assertNotIn("hidden", stdout)
        self.assertTrue(stdout.endswith("no-leaks\n"))


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--scenario":
        globals()["_scenario_" + sys.argv[2]]()
    else:
        unittest.main()
