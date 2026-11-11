"""Bundled checkout assets and working-directory overrides."""

import os
from pathlib import Path


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def configured_patches_root() -> Path:
    override = os.environ.get("VCPI_PATCHES_DIR")
    if override is not None:
        return Path(override).expanduser()
    local = Path.cwd() / "patches"
    return local if local.is_dir() else repo_root() / "patches"
