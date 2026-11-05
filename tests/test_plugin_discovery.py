"""Plugin discovery no longer searches repository-local vst3 subdirectories."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vcpi.commands import assets
from vcpi.plugin_host import PluginHost


class PluginDiscoveryTests(unittest.TestCase):
    def test_local_bundles_require_an_explicit_path_or_search_override(self):
        with tempfile.TemporaryDirectory(prefix="vcpi-discovery-") as directory:
            root = Path(directory)
            checkout = root / "checkout"
            working = root / "working"
            home = root / "home"
            installed = home / ".vst3" / "Installed.vst3"
            configured = root / "custom" / "Configured.vst3"
            local = working / "vst3" / "Local.vst3"
            for bundle in (checkout / "vst3" / "Repo.vst3", checkout / "Root.vst3",
                           working / "Working.vst3", local, installed, configured):
                bundle.mkdir(parents=True)
            previous = Path.cwd()
            try:
                os.chdir(working)
                with patch.dict(os.environ, {"HOME": str(home), "VST3_PATH": "",
                                             "VST_PATH": ""}), \
                        patch.object(assets, "repo_root", return_value=checkout):
                    discovered = assets.iter_vst_plugin_paths()
                    self.assertIn(installed, discovered)
                    self.assertNotIn(checkout / "vst3" / "Repo.vst3", discovered)
                    self.assertNotIn(local, discovered)
                    self.assertEqual(assets.resolve_vst_token("Installed"), str(installed))
                    self.assertEqual(assets.resolve_vst_token("Local"), "Local")
                    self.assertEqual(assets.resolve_vst_token(str(local)), str(local))
                    for variable in ("VST3_PATH", "VST_PATH"):
                        with patch.dict(os.environ, {variable: str(configured.parent)}):
                            self.assertEqual(assets.resolve_vst_token("Configured"), str(configured))
                    with patch.dict(os.environ, {"VST3_PATH": str(local.parent)}):
                        self.assertEqual(assets.resolve_vst_token("Local"), str(local))
            finally:
                os.chdir(previous)

    def test_plugdata_ignores_working_directory_but_honors_override(self):
        with tempfile.TemporaryDirectory(prefix="vcpi-plugdata-discovery-") as directory:
            root = Path(directory)
            local = root / "vst3" / "plugdata.vst3"
            installed = root / "installed" / "plugdata.vst3"
            local.mkdir(parents=True)
            installed.mkdir(parents=True)
            owner = PluginHost.__new__(PluginHost)
            previous = Path.cwd()
            try:
                os.chdir(root)
                with patch.dict(os.environ, {"PLUGDATA_VST3_PATH": ""}), \
                        patch("vcpi.plugin_host.PLUGDATA_VST3_CANDIDATES", (str(installed),)):
                    self.assertEqual(owner._resolve_plugdata_path(None), installed)
                    installed.rmdir()
                    with self.assertRaises(FileNotFoundError):
                        owner._resolve_plugdata_path(None)
                    self.assertEqual(owner._resolve_plugdata_path(str(local)), local)
                    with patch.dict(os.environ, {"PLUGDATA_VST3_PATH": str(local)}):
                        self.assertEqual(owner._resolve_plugdata_path(None), local)
            finally:
                os.chdir(previous)
