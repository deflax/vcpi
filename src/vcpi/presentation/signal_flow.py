"""Compact operational views of cached control-side state, never live probes."""

from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from textwrap import wrap

from vcpi.controllers.akai_midimix import midimix_bindings
from vcpi.controllers.beatstep_pro import PROFILE_NOTE
from vcpi.controllers.midi_input import DECK_NOTE_BINDINGS, DECK_PITCH_CC, DECK_SELECT_CC
from vcpi.models import NUM_TRACKS
from vcpi.presentation.plugin_info import plugin_metadata_rows
from vcpi.presentation.status import format_output_device
from vcpi.sequencer import NUM_SEQ_BANKS, midi_to_note_name


_BINDING_STATES = {
    "mapped": "healthy", "disconnected": "warning", "unbound": "neutral",
    "unsupported": "warning", "unavailable": "warning",
}
_CONTROLLER_NAMES = {"midimix": "MIDI Mix", "beatstep": "BeatStep Pro"}


def _plugin_name(plugin) -> str:
    if plugin is None:
        return "no instrument"
    return plugin.name or Path(plugin.path_to_plugin_file).stem or "Unknown"


def _value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)


def _section(section_id, title, columns, rows, *, states=None) -> dict:
    result = {"id": section_id, "title": title, "columns": list(columns),
              "rows": [[str(cell) for cell in row] for row in rows]}
    result["cell_states"] = [
        [None] * (len(columns) - 1) + [state]
        for state in (states if states is not None else [None] * len(rows))
    ]
    return result


def _target(tracks, index) -> str:
    if index is None:
        return "unlinked"
    track = tracks[index]
    return f"Track {index + 1}: {track.name if track is not None else 'empty'}"


def _source(track) -> str:
    if track is None:
        return "empty"
    if track.source_type == "deck":
        return f"Deck: {track.deck.get('track_title') or track.deck.get('track_id') or 'loading'}"
    return _plugin_name(track.plugin)


def _chain(track) -> str:
    if track is None:
        return "empty"
    return " -> ".join([_source(track), *map(_plugin_name, track.effects)])


def _track_status(track, any_solo, running, failure, master_gain) -> tuple[str, str]:
    if track is None:
        return "empty", "neutral"
    blocked = []
    if failure:
        blocked.append("audio owner failed")
    if track.worker_failure:
        blocked.append(f"worker failed: {track.worker_failure}")
    if track.source_type == "deck" and track.deck.get("error"):
        blocked.append(f"deck error: {track.deck['error']}")
    if not running and not failure:
        blocked.append("audio stopped")
    if not track.enabled:
        blocked.append("disabled")
    if track.source_type != "deck" and track.plugin is None:
        blocked.append("no instrument")
    if track.muted:
        blocked.append("muted")
    if any_solo and not track.solo:
        blocked.append("solo-excluded")
    if track.gain == 0:
        blocked.append("track gain zero")
    if track.source_type == "deck":
        if not track.deck.get("play", False):
            blocked.append("deck paused")
    if master_gain == 0:
        blocked.append("master gain zero")
    if blocked:
        error = bool(failure or track.worker_failure or
                     (track.source_type == "deck" and track.deck.get("error")))
        return "; ".join(blocked), "error" if error else "warning"
    return "eligible", "healthy"


def _pattern(bank) -> str:
    if bank is None or not bank.notes:
        return "empty pattern"
    return " ".join(f"{midi_to_note_name(note)} ({note})" for note in bank.notes)


def _visible_bindings(bindings, details):
    return [binding for binding in bindings
            if details or binding.availability in {"mapped", "disconnected"}]


def _binding_row(binding, details):
    value = binding.value or "-"
    state = binding.availability
    if details:
        value = "; ".join([value, *[f"{label}: {content}" for label, content in
                          (("range", binding.range), ("mode", binding.mode),
                           ("feedback", binding.feedback)) if content]])
        if binding.reason:
            state += f": {binding.reason}"
    return [binding.control, binding.message, binding.target, value, state]


def _effect_section(section_id, title, plugin, details):
    rows = (plugin_metadata_rows(plugin) if details else
            [("Name", _plugin_name(plugin)), ("Type", plugin.category or plugin.info_type or "unknown"),
             ("Latency", f"{plugin.reported_latency_samples} samples")])
    return _section(section_id, title, ["Field", "Value"], rows)


def build_flow_snapshot(host, *, details=False) -> dict:
    """Observe published caches under the snapshot lock, without owner requests."""
    engine = host.engine
    with getattr(engine, "state_lock", nullcontext()):
        return _build_flow_snapshot(host, engine, details)


def _build_flow_snapshot(host, engine, details):
    tracks = list(engine.tracks)
    channel_map = dict(host.channel_map)
    master_effects = list(engine.master_effects)
    master_gain = engine.master_gain
    # Public health getters can detect exits and mutate state; read caches only.
    failure = engine._failure
    running = not engine._closed and failure is None and bool(engine._health[0])
    any_solo = any(track.solo for track in tracks if track is not None)
    statuses = [_track_status(track, any_solo, running, failure, master_gain) for track in tracks]
    inputs = [(index + 1, ctrl.port_name) for index, ctrl in enumerate(host.midi_inputs)]
    open_names = [name for _, name in inputs if name]
    midimix_input = host.midimix.input_port_name
    midimix_output = host.midimix.output_port_name
    beatstep_input = host.beatstep.input_port_name
    bindings = {
        "midimix": midimix_bindings(engine, midimix_input, midimix_output),
        "beatstep": host.beatstep.control_bindings(),
    }
    ports = {"midimix": midimix_input, "beatstep": beatstep_input}
    banks = list(host.sequencer.banks)
    sample = host.timeline.cached_snapshot
    sequencer_running = host.sequencer._running and sample.running
    clock = f"{sample.active_source}; {_value(sample.bpm)} BPM; {sample.status}"
    output = format_output_device(engine.audio_output_name, engine.audio_output_index)
    backend = engine.backend_label or "not opened"
    audio_state = "failed" if failure else "running" if running else "stopped"
    audio_token = "error" if failure else "healthy" if running else "warning"
    stream = f"{engine.sample_rate} Hz; {engine.buffer_size} frames; {engine.output_channels} channels"
    master_chain = " -> ".join(map(_plugin_name, master_effects)) or "no FX"
    routes = [[channel + 1 for channel in range(16) if channel_map.get(channel) == index]
              for index in range(NUM_TRACKS)]
    linked_banks = [[number + 1 for number, bank in enumerate(banks)
                     if bank is not None and bank.linked_track == index]
                    for index in range(NUM_TRACKS)]

    overview, overview_states = [], []
    for index, track in enumerate(tracks):
        source = _source(track)
        if track is not None and track.effects:
            source += f" +{len(track.effects)} FX"
        route = []
        if routes[index]:
            route.append("ch " + ",".join(map(str, routes[index])))
        if linked_banks[index]:
            route.append("bank " + ",".join(map(str, linked_banks[index])))
        overview.append([f"Track {index + 1}", source, "; ".join(route) or "-", statuses[index][0]])
        overview_states.append(statuses[index][1])
    connected = [_CONTROLLER_NAMES[key] for key, port in ports.items() if port]
    problems = sum(state in {"warning", "error"} for _, state in statuses)
    problem_parts = []
    if failure:
        problem_parts.append("audio owner failed")
    elif not running:
        problem_parts.append("audio stopped")
    if master_gain == 0:
        problem_parts.append("master gain zero")
    if problems:
        problem_parts.append(f"{problems} blocked track{'s' if problems != 1 else ''}")
    overview.extend([
        ["Master", master_chain, f"gain {_value(master_gain)}", "gain zero" if master_gain == 0 else audio_state],
        ["Output", output, f"{engine.sample_rate} Hz", audio_state],
        ["Controls", ", ".join(connected) or "none connected", f"{len(open_names)} MIDI inputs", "connected" if connected else "-"],
        ["Problems", "; ".join(problem_parts) or "none cached", "", "see track/audio" if problem_parts else "-"],
    ])
    overview_states.extend(["warning" if master_gain == 0 else audio_token, audio_token,
                            "healthy" if connected else "neutral",
                            "error" if failure else "warning" if problem_parts else "neutral"])
    if details:
        overview.extend([["Clock", clock, "sequencer", "running" if sequencer_running else "stopped"],
                         ["Scope", "cached eligibility only", "not live meters", "hardware not verified"]])
        overview_states.extend([None, None])
    sections = [_section("overview", "Overview", ["Stage", "Source / path", "Routes / value", "State"],
                         overview, states=overview_states)]

    midi_rows, midi_states = [], []
    def midi_row(source, target, state, token=None):
        midi_rows.append([source, target, state])
        midi_states.append(token)

    for number, name in inputs:
        if name or details:
            midi_row(f"Input {number}", name or "disconnected", "shared channel map" if name else "disconnected",
                     "healthy" if name else "warning")
    for channel in range(16):
        target = channel_map.get(channel)
        if target is None and not details:
            continue
        state, token = statuses[target] if target is not None else ("unused", "neutral")
        if target is not None and not open_names:
            state += "; no input"
            token = "warning" if token != "error" else token
        midi_row(f"MIDI channel {channel + 1}", _target(tracks, target), state, token)
    unused = sum(channel_map.get(channel) is None for channel in range(16))
    if not details:
        midi_row("Unused channels", str(unused), "unlinked", "neutral")
    if beatstep_input or details:
        for name, channel, target in zip(("SEQ1", "SEQ2", "DRUM"), host.beatstep.profile.sequence_channels, (2, 3, 4)):
            state, token = statuses[target]
            if not beatstep_input:
                state = "disconnected; " + state
                token = "warning" if token != "error" else token
            midi_row(f"BeatStep {name} ch {channel + 1}", _target(tracks, target), state, token)
    midi_row("Sequencer", "direct bank links", "running" if sequencer_running else "stopped")
    midi_row("Clock", clock, "cached; four-beat bar grid")
    for index in range(NUM_SEQ_BANKS):
        bank = banks[index] if index < len(banks) else None
        target = bank.linked_track if bank is not None else None
        if target is None and not details:
            continue
        state, token = statuses[target] if target is not None else ("unlinked", "neutral")
        if details:
            state += "; " + _pattern(bank)
            if bank is not None:
                state += f"; velocity {bank.velocity}"
        midi_row(f"Sequencer bank {index + 1}", _target(tracks, target), state, token)
    if details:
        for note, action in DECK_NOTE_BINDINGS.items():
            midi_row(f"Deck note {note}", action, "routed generic MIDI; note release ends nudge")
        midi_row(f"Deck CC {DECK_PITCH_CC}", "pitch -16 .. +16%", "linear 0-127")
        midi_row(f"Deck CC {DECK_SELECT_CC}", "track_select 1 .. N", "scaled 0-127")
        midi_row("Position", "confirmed" if sample.position_confirmed else "unconfirmed", "cached clock position")
    sections.append(_section("midi", "MIDI routing & sequencer", ["Source / route", "Destination", "State"],
                             midi_rows, states=midi_states))

    controller_rows, controller_states = [], []
    for key, controller_bindings in bindings.items():
        bound = sum(binding.availability in {"mapped", "disconnected"} for binding in controller_bindings)
        inactive = len(controller_bindings) - bound
        profile = "fixed map" if key == "midimix" else "vcpi profile"
        controller_rows.append([_CONTROLLER_NAMES[key], ports[key] or "disconnected",
                                f"{profile} (unverified)", f"{bound} bound; {inactive} inactive",
                                "connected" if ports[key] else "disconnected"])
        controller_states.append("healthy" if ports[key] else "warning")
    sections.append(_section("controls", "Hardware controls", ["Controller", "Input", "Profile", "Bindings", "State"],
                             controller_rows, states=controller_states))
    for key, controller_bindings in bindings.items():
        visible = sorted(_visible_bindings(controller_bindings, details),
                         key=lambda binding: binding.track if binding.track is not None else NUM_TRACKS + 1)
        rows = [_binding_row(binding, details) for binding in visible]
        states = [_BINDING_STATES[binding.availability] for binding in visible]
        if not rows:
            rows.append(["Bindings", "-", "No available targets", "-", "unbound"])
            states.append("neutral")
        if details and key == "midimix":
            rows.append(["LED output", "channel 1", midimix_output or "disconnected", "cached mute / solo", "connected" if midimix_output else "disconnected"])
            states.append("healthy" if midimix_output else "warning")
        section = _section(f"controls{key}", _CONTROLLER_NAMES[key],
                           ["Control", "Message", "Target", "Cached value", "State"], rows,
                           states=states)
        section["description"] = (
            "Fixed CC/note map; all input channels. Device template not verified."
            if key == "midimix" else PROFILE_NOTE)
        sections.append(section)

    audio_rows = [["Output", output, audio_state], ["Backend", backend, "cached configuration"],
                  ["Stream", stream, "output only"], ["Master", f"{master_chain} -> gain {_value(master_gain)}", "gain zero" if master_gain == 0 else "track sum -> output"],
                  ["Problems", failure or ("audio stopped" if not running else "none cached"), audio_state]]
    audio_states = [audio_token, None, None, "warning" if master_gain == 0 else None, audio_token]
    if details:
        audio_rows.append(["Scope", "cached owner health", "no live meters or external audio input"])
        audio_states.append(None)
    sections.append(_section("audio", "Audio output", ["Field", "Value", "State"], audio_rows, states=audio_states))

    all_bindings = [binding for controller in bindings.values() for binding in controller]
    for index, track in enumerate(tracks):
        rows, states = [], []
        def track_row(label, value, note="", state=None):
            rows.append([label, value, note])
            states.append(state)

        track_row("State", statuses[index][0], "cached eligibility, not signal level", statuses[index][1])
        track_row("Chain", _chain(track), "-> track mixer -> master")
        track_row("MIDI channels", ", ".join(map(str, routes[index])) or "none", "; ".join(open_names) or "no generic input")
        track_row("Sequencer banks", ", ".join(map(str, linked_banks[index])) or "none", "running" if sequencer_running else "stopped")
        if track is not None:
            track_row("Mixer", f"gain {_value(track.gain)}; mute {_value(track.muted)}; solo {_value(track.solo)}",
                      f"enabled {_value(track.enabled)}")
            if track.source_type == "deck":
                deck = track.deck
                track_row("Deck transport", "playing" if deck.get("play") else "paused",
                          f"{_value(deck.get('position', 0))} / {_value(deck.get('duration', 0))} s; "
                          f"{deck.get('state', 'not cached')}")
                bpm = deck.get("bpm")
                detected = (f"{_value(bpm)} BPM" if bpm is not None and bpm > 0 else
                            "analyzing" if deck.get("state") in {"loading", "analyzing"} else "unavailable")
                track_row("Detected BPM", detected, "source grid, not global tempo")
                track_row("Deck sync", f"requested {_value(deck.get('sync', 'not cached'))}; "
                          f"active {_value(deck.get('sync_active', 'not cached'))}")
                for field in ("error", "sync_error"):
                    if deck.get(field):
                        track_row(f"Deck {field}", deck[field], state="error" if field == "error" else "warning")
            if details:
                track_row("Source path", track.path)
                track_row("Worker", f"PID {track.worker_pid if track.worker_pid is not None else 'none'}",
                          f"failure: {track.worker_failure or 'none'}; missed blocks: {track.worker_misses}")
                track_row("Worker buffering", f"{track.buffering_samples} samples")
                if track.plugin is not None:
                    for field, value in plugin_metadata_rows(track.plugin):
                        track_row(field, value)
                for field, path in (("VCV patch", track.vcv_patch_path), ("PD patch", track.pd_patch_path)):
                    if path:
                        track_row(field, path)
                if track.source_type == "deck":
                    for field in ("bank_id", "track_id", "track_title", "requested_track_id", "request_id",
                                  "cue", "pitch", "nudge", "phase_offset", "beat_offset",
                                  "beat_confidence", "playback_speed", "beat_phase_error"):
                        track_row(f"Deck {field}", _value(track.deck.get(field, "not cached")))
                    for ordinal, entry in enumerate(track.deck.get("tracks", []), 1):
                        track_row(f"Deck selection {ordinal}", entry.get("title") or entry["id"],
                                  f"{entry['id']} — {entry.get('path', 'not cached')}")
                for number in linked_banks[index]:
                    track_row(f"Bank {number}", _pattern(banks[number - 1]), f"velocity {banks[number - 1].velocity}")
        for binding in _visible_bindings([binding for binding in all_bindings if binding.track == index + 1], details):
            control, message, target, value, state = _binding_row(binding, details)
            track_row(f"{_CONTROLLER_NAMES[binding.controller]}: {control}", f"{target}: {value}",
                      f"{message}; {state}", _BINDING_STATES[binding.availability])
        track_row("Output", "master sum -> master FX -> master gain -> output")
        section_id = f"track{index + 1}"
        sections.append(_section(section_id, f"Track {index + 1}", ["Field / control", "Value / target", "State / route"], rows, states=states))
        if track is not None:
            for effect_index, effect in enumerate(track.effects, 1):
                sections.append(_effect_section(f"{section_id}fx{effect_index}", f"Track {index + 1} FX {effect_index}", effect, details))

    master_rows = [["Input", "sum of eligible track outputs", "after track gain / mute / solo"],
                   ["Chain", master_chain, "-> master gain -> output"],
                   ["Master gain", _value(master_gain), "gain zero" if master_gain == 0 else "range 0 .. 1"],
                   ["Output", output, audio_state]]
    master_states = [None, None, "warning" if master_gain == 0 else "healthy", audio_token]
    for binding in _visible_bindings([binding for binding in bindings["midimix"] if binding.track is None], details):
        control, message, target, value, state = _binding_row(binding, details)
        master_rows.append([control, f"{target}: {value}", f"{message}; {state}"])
        master_states.append(_BINDING_STATES[binding.availability])
    if details:
        master_rows.append(["Stream", stream, backend])
        master_states.append(None)
    sections.append(_section("master", "Master bus", ["Field", "Value", "State / route"], master_rows, states=master_states))
    for index, effect in enumerate(master_effects, 1):
        sections.append(_effect_section(f"masterfx{index}", f"Master FX {index}", effect, details))
    return {"title": "vcpi Signal Flow (cached)", "sections": sections}


def render_flow_snapshot(snapshot: dict, section_id: str | None = None) -> str:
    """Render every section or one target as literal, bounded-width plain text."""
    sections = [section for section in snapshot["sections"]
                if section_id is None or section["id"] == section_id]
    if not sections:
        raise ValueError(f"No loaded show target: {section_id}")
    lines = [snapshot["title"]]
    for section in sections:
        lines.extend(["", f"== {section['title']} =="])
        if section.get("description"):
            lines.extend(wrap(section["description"], width=80))
        columns = section["columns"]
        text_rows = [[cell.replace("\r", "\\r").replace("\n", "\\n").replace("\t", "\\t")
                      for cell in row] for row in [columns, *section["rows"]]]
        widths = [max(len(row[index]) for row in text_rows) for index in range(len(columns))]
        available = 80 - 3 * (len(columns) - 1)
        while sum(widths) > available:
            widest = max(range(len(widths)), key=widths.__getitem__)
            widths[widest] -= 1
        for row_index, row in enumerate(text_rows):
            if section["id"] == "overview":
                # The overview stays one line per stage; target views retain full cells.
                wrapped = [[cell if len(cell) <= width else cell[:width - 3] + "..."]
                           for cell, width in zip(row, widths)]
            else:
                wrapped = [wrap(cell, width=width, replace_whitespace=False) or [""]
                           for cell, width in zip(row, widths)]
            for line_index in range(max(map(len, wrapped))):
                lines.append(" | ".join((cell[line_index] if line_index < len(cell) else "").ljust(width)
                                        for cell, width in zip(wrapped, widths)).rstrip())
            if row_index == 0:
                lines.append("-+-".join("-" * width for width in widths))
    return "\n".join(lines)
