"""Native audio backend, imported only in audio ownership processes."""

from __future__ import annotations

import numpy as np

try:
    from core import _vcpi_native as native
except ImportError:
    native = None

HAS_NATIVE = native is not None


def require_native():
    if native is None:
        raise RuntimeError("native audio backend is not built; run python build_native.py")
    return native
