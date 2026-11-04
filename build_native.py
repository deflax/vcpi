#!/usr/bin/env python3
"""Build the native audio extension for this interpreter and platform."""

from pathlib import Path
import os
import platform
import subprocess
import shlex
import sys


OUTPUT_FRAGMENT_BYTES = 4096


def _write_build_fragment(fragment):
    text = fragment.decode("utf-8", errors="replace")
    sys.stdout.write("[build] " + text + ("" if text.endswith("\n") else "\n"))
    sys.stdout.flush()


def run_build_step(command):
    """Run a build step, streaming prefixed output regardless of logging level."""
    pending = bytearray()
    with subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0
    ) as process:
        while chunk := process.stdout.read(OUTPUT_FRAGMENT_BYTES):
            pending.extend(chunk)
            while pending:
                newline = pending.find(b"\n", 0, OUTPUT_FRAGMENT_BYTES)
                if newline >= 0:
                    end = newline + 1
                elif len(pending) >= OUTPUT_FRAGMENT_BYTES:
                    end = OUTPUT_FRAGMENT_BYTES
                else:
                    break
                _write_build_fragment(pending[:end])
                del pending[:end]
        returncode = process.wait()
    if pending:
        _write_build_fragment(pending)
    if returncode:
        sys.stderr.write(
            f"[build] Command failed ({returncode}): {shlex.join(command)}\n"
        )
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
