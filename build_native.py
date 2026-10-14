#!/usr/bin/env python3
"""Build the native audio extension for this interpreter and platform."""

from pathlib import Path
import os
import platform
import subprocess
import sys


def main():
    root = Path(__file__).resolve().parent
    # Never reuse CMake's compiler/Python cache across platforms or ABIs.
    target = f"{sys.platform}-{platform.machine()}-{sys.implementation.cache_tag}"
    build = root / "build" / target
    _ = subprocess.run([
        "cmake", "-S", str(root / "native"), "-B", str(build),
        "-DCMAKE_BUILD_TYPE=Release", f"-DPython_EXECUTABLE={sys.executable}",
    ], check=True)
    # Large JUCE translation units can exhaust memory on small machines.
    jobs = os.environ.get("CMAKE_BUILD_PARALLEL_LEVEL", "2")
    _ = subprocess.run(["cmake", "--build", str(build), "--parallel", jobs], check=True)


if __name__ == "__main__":
    main()
