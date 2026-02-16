"""Low-level MIDI input/output port wrappers (python-rtmidi)."""

from __future__ import annotations

from typing import Callable, Optional

from core.deps import HAS_RTMIDI, rtmidi


def list_midi_input_ports() -> list[str]:
    if not HAS_RTMIDI:
        return []
    midi_in = rtmidi.MidiIn()
    ports = [midi_in.get_port_name(i) for i in range(midi_in.get_port_count())]
    midi_in.delete()
    return ports


def list_midi_output_ports() -> list[str]:
    if not HAS_RTMIDI:
        return []
    midi_out = rtmidi.MidiOut()
    ports = [midi_out.get_port_name(i) for i in range(midi_out.get_port_count())]
    midi_out.delete()
    return ports


class MidiInPort:
    """Open one hardware or virtual MIDI input port and dispatch callbacks."""

    def __init__(self):
        self._port = None
        self._name: Optional[str] = None

    @staticmethod
    def list_ports() -> list[str]:
        return list_midi_input_ports()

    def open_input_port(self, port_index: int, callback: Callable) -> str:
        self.close()
        if not HAS_RTMIDI:
            raise RuntimeError("python-rtmidi not installed")
        self._port = rtmidi.MidiIn()
        self._port.open_port(port_index)
        self._name = self._port.get_port_name(port_index) or f"midi-in-{port_index}"
        self._port.set_callback(callback)
        return str(self._name)

    def open_virtual_input_port(self, name: str, callback: Callable) -> str:
        self.close()
        if not HAS_RTMIDI:
            raise RuntimeError("python-rtmidi not installed")
        self._port = rtmidi.MidiIn()
        self._port.open_virtual_port(name)
        self._name = name
        self._port.set_callback(callback)
        return str(self._name)

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


class MidiOutPort:
    """Open one hardware or virtual MIDI output port and send bytes."""

    def __init__(self):
        self._port = None
        self._name: Optional[str] = None

    @staticmethod
    def list_ports() -> list[str]:
        return list_midi_output_ports()

    def open_output_port(self, port_index: int) -> str:
        self.close()
        if not HAS_RTMIDI:
            raise RuntimeError("python-rtmidi not installed")
        self._port = rtmidi.MidiOut()
        self._port.open_port(port_index)
        self._name = self._port.get_port_name(port_index) or f"midi-out-{port_index}"
        return str(self._name)

    def open_virtual_output_port(self, name: str) -> str:
        self.close()
        if not HAS_RTMIDI:
            raise RuntimeError("python-rtmidi not installed")
        self._port = rtmidi.MidiOut()
        self._port.open_virtual_port(name)
        self._name = name
        return str(self._name)

    def send(self, data: list[int]):
        if self._port is None:
            raise RuntimeError("MIDI output port is not open")
        self._port.send_message(data)

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
