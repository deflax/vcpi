"""A copied checkout must build without reusing absolute-path CMake state."""

from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from build_native import native_build_directory


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("cmake") and shutil.which("cc"),
                     "requires CMake and a C compiler")
class RelocatedBuildTests(unittest.TestCase):
    def build(self, checkout):
        result = subprocess.run(
            [sys.executable, str(checkout / "build_native.py")], cwd=checkout,
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        executable = native_build_directory(checkout) / "cache_probe"
        return subprocess.check_output([str(executable)], text=True).strip()

    def test_copied_cache_is_preserved_and_relocated_source_is_compiled(self):
        with tempfile.TemporaryDirectory(prefix="vcpi-build-cache-") as directory:
            original = Path(directory) / "original checkout"
            source = original / "src" / "engine"
            source.mkdir(parents=True)
            shutil.copy2(ROOT / "build_native.py", original / "build_native.py")
            (source / "CMakeLists.txt").write_text(
                "cmake_minimum_required(VERSION 3.22)\n"
                "project(cache_relocation C)\n"
                "add_executable(cache_probe main.c)\n"
            )
            (source / "main.c").write_text(
                '#include <stdio.h>\nint main(void) { puts("original"); return 0; }\n'
            )
            self.assertEqual(self.build(original), "original")
            original_build = native_build_directory(original)
            original_cache = (original_build / "CMakeCache.txt").read_bytes()

            relocated = Path(directory) / "relocated checkout"
            shutil.copytree(original, relocated)
            (relocated / "src" / "engine" / "main.c").write_text(
                '#include <stdio.h>\nint main(void) { puts("relocated"); return 0; }\n'
            )
            self.assertEqual(self.build(relocated), "relocated")
            copied_build = relocated / original_build.relative_to(original)
            self.assertEqual((copied_build / "CMakeCache.txt").read_bytes(), original_cache)
            self.assertEqual((original_build / "CMakeCache.txt").read_bytes(), original_cache)


if __name__ == "__main__":
    unittest.main()
