"""System status panel renderer for vcpi.

Displays audio engine state, backend, MIDI connections, Ableton Link,
session path, render worker capacity, and callback health in an ASCII box.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.host import VcpiCore


def format_realtime(info: dict) -> str:
    """Describe observed native scheduling, not merely the requested policy."""
    if not info:
        return "not reported"
    state = info["state"]
    if state == "unsupported":
        return "unsupported (Linux-only)"
    requested = info["requested_priority"]
    if state in {"inactive", "pending"}:
        return f"{state} (requested SCHED_FIFO priority {requested})"
    text = (
        f"{state}: {info['policy']} priority {info['priority']}"
        f" (requested {requested}), TID {info['thread_id']}"
    )
    if info["error"]:
        text += f", errno={info['error']} ({os.strerror(info['error'])})"
    return text


def render_status(host: VcpiCore) -> str:
    """Return an ASCII status box using control state and cached audio health.

    Parameters
    ----------
    host : VcpiCore
        The running host instance.
    """
    engine = host.engine
    link = host.link

    rows: list[tuple[str, str]] = []

    # -- Audio ---------------------------------------------------------------
    # Failure wins over the last shared running flag: an owner failure is
    # explicitly stopped, even if its final callback could not publish health.
    failure = engine.failure
    audio_state = "RUNNING" if engine.running and not failure else "STOPPED"
    rows.append(("Audio", f"{audio_state}  (sr={host.sample_rate} buf={host.buffer_size})"))
    rows.append(("Audio owner", f"PID {engine.owner_pid} (output mixer and master effects)"))
    rows.append(("Audio failure", str(failure) if failure else "(none)"))
    rows.append(("Backend", engine.backend_label))
    rows.append(("Output realtime", format_realtime(engine.output_realtime)))

    # -- Render capacity and callback health ---------------------------------
    # Capacity is the maximum worker count, not active threads/eligible slots.
    workers = engine.render_workers
    slots = engine.render_slots
    cpus = os.cpu_count() or 0
    mode = "idle" if slots == 0 else "isolated processes"
    slot_label = "slot" if slots == 1 else "slots"
    rows.append(
        ("Render", f"max: {workers} slot processes / {cpus} CPUs"
         f"  (last callback: {slots} eligible {slot_label}, {mode})")
    )
    rows.append(("Callbacks", str(engine.callback_count)))
    rows.append(
        ("Callback time", f"last: {engine.last_callback_ms:.3f} ms"
         f"  peak: {engine.peak_callback_ms:.3f} ms"
         f"  deadline: {engine.block_period_ms:.3f} ms")
    )
    rows.append(("Deadline misses", str(engine.deadline_misses)))
    rows.append(("Underruns", str(engine.underruns)))
    rows.append(("Render errors", str(engine.render_errors)))
    buffering_ms = host.buffer_size * 1000.0 / host.sample_rate
    rows.append(("Slot buffering", f"{host.buffer_size} samples / {buffering_ms:.3f} ms (1 block)"))
    for index, slot in enumerate(engine.slots):
        if slot is None:
            continue
        state = f"FAILED: {slot.worker_failure}" if slot.worker_failure else "ready"
        rows.append((f"Slot {index + 1} worker",
                     f"PID {slot.worker_pid}  {state}  missing blocks: {slot.worker_misses}"))
        rows.append((f"Slot {index + 1} realtime", format_realtime(slot.worker_realtime)))

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
    if host.loaded_session_path:
        rows.append(("Session", str(host.loaded_session_path)))
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
