"""Low-level MIDI input port wrapper (python-rtmidi)."""

from __future__ import annotations

from typing import Callable, Optional

from linkvst.deps import HAS_RTMIDI, rtmidi


class MidiPort:
    """Opens a single hardware or virtual MIDI input port with a callback."""

    def __init__(self):
        self._port = None
        self._name: Optional[str] = None

    # -- class helpers -------------------------------------------------------

    @staticmethod
    def list_ports() -> list[str]:
        if not HAS_RTMIDI:
            return []
        m = rtmidi.MidiIn()
        ports = [m.get_port_name(i) for i in range(m.get_port_count())]
        m.delete()
        return ports

    # -- open / close --------------------------------------------------------

    def open(self, port_index: int, callback: Callable) -> str:
        self.close()
        if not HAS_RTMIDI:
            raise RuntimeError("python-rtmidi not installed")
        self._port = rtmidi.MidiIn()
        self._port.open_port(port_index)
        self._name = self._port.get_port_name(port_index)
        self._port.set_callback(callback)
        return self._name

    def open_virtual(self, name: str, callback: Callable) -> str:
        self.close()
        if not HAS_RTMIDI:
            raise RuntimeError("python-rtmidi not installed")
        self._port = rtmidi.MidiIn()
        self._port.open_virtual_port(name)
        self._name = name
        self._port.set_callback(callback)
        return name

    def close(self):
        if self._port:
            self._port.close_port()
            self._port.delete()
            self._port = None
            self._name = None

    @property
    def is_open(self) -> bool:
        return self._port is not None

    @property
    def name(self) -> Optional[str]:
        return self._name
