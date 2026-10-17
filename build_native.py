#!/usr/bin/env python3
"""Build the native audio extension for this interpreter and platform."""

from pathlib import Path
import os
import platform
import subprocess
import shlex
import sys


# Keep only the last 64 KiB of a quiet step for actionable failure diagnostics.
FAILURE_TAIL_BYTES = 64 * 1024
OUTPUT_FRAGMENT_BYTES = 4096


def _write_debug_fragment(fragment):
    text = fragment.decode("utf-8", errors="replace")
    sys.stdout.write("[build] " + text + ("" if text.endswith("\n") else "\n"))
    sys.stdout.flush()


def run_build_step(command):
    """Run a build step, streaming DEBUG output or retaining a bounded failure tail."""
    debug = os.environ.get("LOG_LEVEL", "WARNING").strip().upper() == "DEBUG"
    pending = bytearray()
    truncated = False
    with subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0
    ) as process:
        while chunk := process.stdout.read(OUTPUT_FRAGMENT_BYTES):
            pending.extend(chunk)
            if debug:
                while pending:
                    newline = pending.find(b"\n", 0, OUTPUT_FRAGMENT_BYTES)
                    if newline >= 0:
                        end = newline + 1
                    elif len(pending) >= OUTPUT_FRAGMENT_BYTES:
                        end = OUTPUT_FRAGMENT_BYTES
                    else:
                        break
                    _write_debug_fragment(pending[:end])
                    del pending[:end]
            elif len(pending) > FAILURE_TAIL_BYTES:
                del pending[:-FAILURE_TAIL_BYTES]
                truncated = True
        returncode = process.wait()
    if debug and pending:
        _write_debug_fragment(pending)
    if returncode:
        sys.stderr.write(
            f"[build] Command failed ({returncode}): {shlex.join(command)}\n"
        )
        if not debug:
            if truncated:
                sys.stderr.write(
                    f"[build] Showing the last {FAILURE_TAIL_BYTES} bytes of diagnostics:\n"
                )
            text = pending.decode("utf-8", errors="replace")
            sys.stderr.write(text)
            if text and not text.endswith("\n"):
                sys.stderr.write("\n")
        sys.stderr.flush()
        raise subprocess.CalledProcessError(returncode, command)


def main():
    root = Path(__file__).resolve().parent
    # Never reuse CMake's compiler/Python cache across platforms or ABIs.
    target = f"{sys.platform}-{platform.machine()}-{sys.implementation.cache_tag}"
    build = root / "build" / target
    try:
        run_build_step([
            "cmake", "-S", str(root / "native"), "-B", str(build),
            "-DCMAKE_BUILD_TYPE=Release", f"-DPython_EXECUTABLE={sys.executable}",
        ])
        # Large JUCE translation units can exhaust memory on small machines.
        jobs = os.environ.get("CMAKE_BUILD_PARALLEL_LEVEL", "2")
        run_build_step(["cmake", "--build", str(build), "--parallel", jobs])
    except subprocess.CalledProcessError as error:
        return error.returncode if error.returncode > 0 else 1
    except OSError as error:
        print(f"[build] {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
