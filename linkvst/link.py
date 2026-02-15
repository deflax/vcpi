"""Ableton Link synchronisation (aalink wrapper)."""

from __future__ import annotations

from linkvst.deps import HAS_LINK, aalink


class LinkSync:
    """Minimal wrapper around aalink for tempo sync."""

    def __init__(self, bpm: float = 120.0):
        self._link = None
        self._bpm = bpm
        self._enabled = False

    def enable(self):
        if not HAS_LINK:
            raise RuntimeError("aalink not installed")
        if self._enabled:
            return
        self._link = aalink.Link(self._bpm)
        self._link.enabled = True
        self._enabled = True

    def disable(self):
        if self._link:
            self._link.enabled = False
            self._link = None
        self._enabled = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def bpm(self) -> float:
        return self._link.tempo if self._link else self._bpm

    @bpm.setter
    def bpm(self, value: float):
        self._bpm = value
        if self._link:
            self._link.tempo = value

    @property
    def num_peers(self) -> int:
        return self._link.num_peers if self._link else 0
