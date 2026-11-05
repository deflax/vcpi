"""Bundled checkout assets and working-directory overrides."""

import os
from pathlib import Path


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _asset_directory(name: str) -> Path:
    local = Path.cwd() / "assets" / name
    return local if local.is_dir() else repo_root() / "assets" / name


def samples_root() -> Path:
    return _asset_directory("samples")


def configured_patches_root() -> Path:
    override = os.environ.get("VCPI_PATCHES_DIR")
    if override is not None:
        return Path(override).expanduser()
    return _asset_directory("patches")
