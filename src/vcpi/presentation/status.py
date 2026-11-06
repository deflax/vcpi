"""System status panel renderer for vcpi.

Displays audio engine state, backend, MIDI connections, Ableton Link,
session path, render worker capacity, and callback health in an ASCII box.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vcpi.host import VcpiCore


def format_output_device(name, index) -> str:
    """Identify the opened PortAudio entry without guessing alias destinations."""
    if name is None:
        return "(not opened)"
    label = f"[{index}] {name}" if index is not None else name
    if name.lower() == "default":
        label += " (system alias; physical output not identified)"
    return label


def format_realtime(info: dict) -> str:
    """Describe native scheduling observations with platform-specific semantics."""
    if not info:
        return "not reported"
    state = info["state"]
    if state == "unsupported":
        return "unsupported"
    policy = info.get("policy", "unknown")
    requested_policy = info.get("requested_policy") or (
        policy if policy in {"THREAD_TIME_CONSTRAINT", "CoreAudio"} else "SCHED_FIFO"
    )
    requested = info["requested_priority"]
    if state in {"inactive", "pending"}:
        request = requested_policy
        if requested_policy == "SCHED_FIFO":
            request += f" priority {requested}"
        return f"{state} (requested {request})"
    if requested_policy == "CoreAudio":
        text = f"{state}: {policy}"
    elif requested_policy == "THREAD_TIME_CONSTRAINT":
        text = (
            f"{state}: {policy} (requested {requested_policy}), TID {info['thread_id']}"
        )
        if state == "configured":
            text += " (startup accepted; live policy not monitored)"
    else:
        text = (
            f"{state}: {policy} priority {info['priority']}"
            f" (requested {requested}), TID {info['thread_id']}"
        )
    if info["error"]:
        domain = info.get("error_domain") or (
            "mach" if requested_policy == "THREAD_TIME_CONSTRAINT" else "errno"
        )
        message = (
            os.strerror(info["error"])
            if domain == "errno"
            else info.get("error_message")
        )
        text += f", {domain}={info['error']}"
        if message:
            text += f" ({message})"
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
    device = format_output_device(engine.audio_output_name, engine.audio_output_index)
    if engine.audio_output_name and audio_state == "STOPPED":
        device += " (last opened; stopped)"
    rows.append(("Output device", device))
    preferred = getattr(host, "preferred_output_device", None)
    rows.append(("Preferred output", str(preferred) if preferred is not None else "(system default)"))
    rows.append(("Backend", engine.backend_label))
    rows.append(("Output realtime", format_realtime(engine.output_realtime)))

    # -- Render capacity and callback health ---------------------------------
    # Capacity is the maximum worker count, not active threads/eligible tracks.
    workers = engine.render_workers
    tracks = engine.render_tracks
    cpus = os.cpu_count() or 0
    mode = "idle" if tracks == 0 else "isolated processes"
    track_label = "track" if tracks == 1 else "tracks"
    rows.append(
        ("Render", f"max: {workers} track processes / {cpus} CPUs"
         f"  (last callback: {tracks} eligible {track_label}, {mode})")
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
    rows.append(("Track buffering", f"{host.buffer_size} samples / {buffering_ms:.3f} ms (1 block)"))
    for index, track in enumerate(engine.tracks):
        if track is None:
            continue
        state = f"FAILED: {track.worker_failure}" if track.worker_failure else "ready"
        rows.append((f"Track {index + 1} worker",
                     f"PID {track.worker_pid}  {state}  missing blocks: {track.worker_misses}"))
        rows.append((f"Track {index + 1} realtime", format_realtime(track.worker_realtime)))
        if track.source_type == "deck":
            deck = track.deck
            title = deck.get("track_title") or deck.get("track_id", "(loading)")
            rows.append((f"Track {index + 1} deck",
                         f"{title} | {deck.get('state', 'unknown')} | "
                         f"{'playing' if deck.get('play') else 'paused'} | "
                         f"{deck.get('position', 0):.2f}/{deck.get('duration', 0):.2f}s | "
                         f"pitch {deck.get('pitch', 0):+.2f}%"))
            if deck.get("error"):
                rows.append((f"Track {index + 1} deck error", str(deck["error"])))

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
    rows.append(("BeatStep Pro IN", host.beatstep.input_port_name or "closed"))

    rows.append(("", ""))  # spacer

    # -- Link ----------------------------------------------------------------
    if link.enabled:
        rows.append(("Link", f"{link.bpm:.1f} BPM  ({link.num_peers} peers)"))
    else:
        rows.append(("Link", "disabled"))
    timeline = host.timeline
    sample = timeline.snapshot()
    rows.append(("Clock", f"{sample.bpm:.2f} BPM | selected={sample.source} "
                 f"active={sample.active_source} | {sample.status}"))
    rows.append(("Transport", f"{'running' if sample.running else 'stopped'} | "
                 f"beat {sample.beat:.3f} | bar phase {sample.beat % timeline.BAR:.3f}/{timeline.BAR:g} | "
                 f"position {'confirmed' if sample.position_confirmed else 'unconfirmed'}"))

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
