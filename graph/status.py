"""System status panel renderer for vcpi.

Displays audio engine state, backend, MIDI connections, Ableton Link,
session path, and render thread pool information in an ASCII box.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from core.deps import HAS_SOUNDDEVICE, sd

if TYPE_CHECKING:
    from core.host import VcpiCore


def _audio_backend_label(engine) -> str:
    """Best-effort host-audio backend label (ALSA/JACK/Pulse/PipeWire/etc.)."""
    if not HAS_SOUNDDEVICE or sd is None:
        return "sounddevice unavailable"
    try:
        device_index = None
        stream = engine._stream
        if stream is not None:
            stream_device = getattr(stream, "device", None)
            if isinstance(stream_device, (tuple, list)):
                if len(stream_device) >= 2 and isinstance(stream_device[1], int):
                    device_index = stream_device[1]
                elif len(stream_device) == 1 and isinstance(stream_device[0], int):
                    device_index = stream_device[0]
            elif isinstance(stream_device, int):
                device_index = stream_device

        if not isinstance(device_index, int):
            default_device = sd.default.device
            if isinstance(default_device, (tuple, list)):
                if len(default_device) >= 2 and isinstance(default_device[1], int):
                    device_index = default_device[1]
                elif len(default_device) == 1 and isinstance(default_device[0], int):
                    device_index = default_device[0]
            elif isinstance(default_device, int):
                device_index = default_device

        if not isinstance(device_index, int) or device_index < 0:
            return "unknown"

        device_info = sd.query_devices(device_index)
        hostapi_index = device_info.get("hostapi")
        if not isinstance(hostapi_index, int):
            return "unknown"
        hostapi_name = sd.query_hostapis(hostapi_index).get("name", "unknown")
        device_name = device_info.get("name")
        if device_name:
            return f"{hostapi_name} ({device_name})"
        return str(hostapi_name)
    except Exception:
        return "unknown"


def _pool_status(engine) -> tuple[int, int]:
    """Return (max_workers, active_threads) for the render pool."""
    pool = getattr(engine, "_render_pool", None)
    if pool is None:
        return 0, 0
    max_w = getattr(pool, "_max_workers", 0)
    # _threads is the set of alive worker threads in ThreadPoolExecutor
    active = len(getattr(pool, "_threads", set()))
    return max_w, active


def render_status(host: VcpiCore) -> str:
    """Return an ASCII status box summarising the host state.

    Parameters
    ----------
    host : VcpiCore
        The running host instance.
    """
    engine = host.engine
    link = host.link

    rows: list[tuple[str, str]] = []

    # -- Audio ---------------------------------------------------------------
    audio_state = "RUNNING" if engine.running else "STOPPED"
    rows.append(("Audio", f"{audio_state}  (sr={host.sample_rate} buf={host.buffer_size})"))
    rows.append(("Backend", _audio_backend_label(engine)))

    # -- Render pool ---------------------------------------------------------
    max_w, active = _pool_status(engine)
    cpus = os.cpu_count() or 0
    rows.append(("Render", f"{max_w} workers / {cpus} CPUs  ({active} active)"))

    rows.append(("", ""))  # spacer

    # -- MIDI ----------------------------------------------------------------
    if host.midi_inputs:
        for i, ctrl in enumerate(host.midi_inputs):
            label = f"MIDI IN[{i + 1}]"
            rows.append((label, ctrl.port_name or getattr(ctrl, "label", "?")))
    else:
        rows.append(("MIDI IN", "(none)"))

    rows.append(("MIDIMix IN", host.mixer_midi_name or "closed"))
    rows.append(("MIDIMix OUT", host.mixer_midi_out_name or "closed"))

    rows.append(("", ""))  # spacer

    # -- Link ----------------------------------------------------------------
    if link.enabled:
        rows.append(("Link", f"{link.bpm:.1f} BPM  ({link.num_peers} peers)"))
    else:
        rows.append(("Link", "disabled"))

    # -- Session -------------------------------------------------------------
    if host.loaded_session_name:
        rows.append(("Session", f"{host.loaded_session_name}  ({host.session_path})"))
    else:
        rows.append(("Session", str(host.session_path)))

    # -- render box ----------------------------------------------------------
    title = "vcpi Status"
    key_width = max(len(k) for k, _ in rows)
    val_width = max(len(v) for _, v in rows)
    body_width = max(len(title), key_width + 3 + val_width)

    border = "+" + "-" * (body_width + 2) + "+"
    lines = [
        border,
        f"| {title:^{body_width}} |",
        border,
    ]
    for key, val in rows:
        if not key and not val:
            lines.append(f"| {'':<{body_width}} |")
        elif not key:
            lines.append(f"| {val:^{body_width}} |")
        else:
            line_text = f"{key:<{key_width}} : {val}"
            lines.append(f"| {line_text:<{body_width}} |")
    lines.append(border)

    return "\n".join(lines)
