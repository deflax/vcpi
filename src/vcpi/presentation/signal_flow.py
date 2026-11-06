"""Read-only routing snapshots and plain-text tables from control-side state.

No discovery, owner requests, parameter reads from native plugins, or routing
changes belong here. Parameter values are the last published owner snapshots,
not live meters or confirmation that a source is producing sound.
"""

from __future__ import annotations

from contextlib import nullcontext
from itertools import islice
from pathlib import Path

from vcpi.controllers.akai_midimix import (
    FADER_CCS, KNOB_CCS, MASTER_FADER_CC, MUTE_NOTES, SOLO_NOTES,
)
from vcpi.controllers.midi_input import DECK_NOTE_BINDINGS, DECK_PITCH_CC, DECK_SELECT_CC
from vcpi.models import NUM_TRACKS
from vcpi.sequencer import NUM_SEQ_BANKS, midi_to_note_name
from vcpi.presentation.plugin_info import plugin_metadata_rows
from vcpi.presentation.status import format_output_device


def _plugin_name(plugin) -> str:
    if plugin is None:
        return "(no instrument)"
    return (plugin.name or Path(plugin.path_to_plugin_file).stem or "Unknown")


def _value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)


def _section(section_id, title, columns, rows) -> dict:
    return {"id": section_id, "title": title, "columns": list(columns),
            "rows": [[str(cell) for cell in row] for row in rows]}


def show_view(arguments: list[str]) -> str:
    """Resolve public show arguments to a snapshot section, without owner calls."""
    if not arguments:
        return "overview"
    target = arguments[0]
    tracks = {str(index) for index in range(1, NUM_TRACKS + 1)}
    if len(arguments) == 1:
        if target in tracks:
            return f"track{target}"
        if target in {"all", "midi", "controls", "audio", "master"}:
            return target
    elif len(arguments) == 3 and target in tracks | {"master"} and arguments[1] == "fx":
        try:
            effect_index = int(arguments[2])
        except ValueError:
            raise ValueError("fx_index must be a positive integer") from None
        if effect_index < 1:
            raise ValueError("fx_index must be a positive integer")
        return f"{'master' if target == 'master' else 'track' + target}fx{effect_index}"
    raise ValueError(
        "Usage: show [all|midi|controls|audio|1-8|master] | "
        "show <1-8|master> fx <fx_index>"
    )


def _target(tracks, index) -> str:
    if index is None:
        return "unlinked"
    track = tracks[index]
    return f"Track {index + 1}: {track.name if track is not None else '(empty)'}"


def _chain(track) -> str:
    if track is None:
        return "(empty)"
    source = _plugin_name(track.plugin)
    if track.source_type == "deck":
        status = track.deck
        source = f"Deck: {status.get('track_title') or status.get('track_id', '(loading)')}"
    return " -> ".join([source, *[_plugin_name(effect) for effect in track.effects]])


def _parameter_detail(plugin, name, parameter) -> str:
    current = plugin.values.get(name)
    value = _value(current) if name in plugin.values else "(not cached)"
    if parameter.string_value and parameter.string_value != value:
        value += f" ({parameter.string_value})"
    if parameter.kind == "bool":
        bounds = "false / true; CC 0-63 / 64-127"
    elif parameter.kind == "enum":
        bounds = (f"indices 0-{max(0, len(parameter.valid_values) - 1)}: "
                  + ", ".join(parameter.valid_values))
        if not parameter.valid_values:
            bounds = "no enum values; binding inactive"
    else:
        bounds = " .. ".join(_value(bound) for bound in parameter.range) or "unknown"
    units = f" {parameter.units}" if parameter.units else ""
    return f"{value}{units}; range {bounds}"


def _control_rows(index, track, input_name, output_name) -> list[list[str]]:
    """Mirror the controller's first-three sound-control mapping exactly."""
    label = f"Track {index + 1}"
    state = "active" if input_name else "inactive: MIDI Mix input disconnected"
    if track is None:
        state = "inactive: empty track"
    rows = [[f"{label} fader", f"CC {FADER_CCS[index]} (0-127)",
             f"{label} gain", f"{_value(track.gain) if track else '-'}; range 0 .. 1", state]]
    parameters = (list(islice(track.plugin.knob_parameter_items(), 3))
                  if track is not None and track.plugin is not None else [])
    for knob, cc in enumerate(KNOB_CCS[index]):
        if knob < len(parameters):
            name, parameter = parameters[knob]
            detail = _parameter_detail(track.plugin, name, parameter)
            knob_state = state
            if parameter.kind == "enum" and not parameter.valid_values:
                knob_state = "inactive: no enum values"
            target = f"{label} instrument: {name}"
        else:
            target, detail = f"{label}: (unbound)", "-"
            knob_state = "inactive: empty track" if track is None else "inactive: no instrument parameter"
        rows.append([f"{label} knob {knob + 1}", f"CC {cc} (0-127)",
                     target, detail, knob_state])
    for field, notes, enabled in (("mute", MUTE_NOTES, bool(track and track.muted)),
                                  ("solo", SOLO_NOTES, bool(track and track.solo))):
        led = f"LED {'on' if enabled else 'off'} (velocity {127 if enabled else 0})"
        led_state = "connected" if output_name else "disconnected"
        rows.append([f"{label} {field}", f"Note {notes[index]} (note-on velocity > 0)",
                     f"{label} {field} toggle", f"{_value(enabled)}; {led}",
                     f"{state}; LED output {led_state}, channel 1"])
    return rows


def _track_state(track, any_solo, running, failure, master_gain) -> str:
    if track is None:
        return "empty; no audio source"
    blocked = []
    if failure:
        blocked.append("audio owner failed")
    if not running:
        blocked.append("audio stopped")
    if not track.enabled:
        blocked.append("disabled")
    if track.worker_failure:
        blocked.append(f"worker failed: {track.worker_failure}")
    if track.muted:
        blocked.append("muted")
    if any_solo and not track.solo:
        blocked.append("solo-excluded")
    if track.gain == 0:
        blocked.append("track gain zero")
    if track.source_type == "deck" and not track.deck.get("play", False):
        blocked.append("deck paused")
    if master_gain == 0:
        blocked.append("master gain zero")
    return ("blocked: " + "; ".join(blocked) if blocked else
            "eligible for output; signal activity not measured")


def _pattern(bank) -> str:
    if bank is None or not bank.notes:
        return "(empty pattern)"
    return " ".join(f"{midi_to_note_name(note)} ({note})" for note in bank.notes)


def build_flow_snapshot(host) -> dict:
    """Build the complete explorer model using only existing local state.

    Hold the engine's snapshot lock while reading cached plugin/track metadata;
    this neither synchronizes with an owner nor probes MIDI/audio hardware.
    """
    engine = host.engine
    with getattr(engine, "state_lock", nullcontext()):
        return _build_flow_snapshot(host, engine)


def _build_flow_snapshot(host, engine) -> dict:
    tracks = list(engine.tracks)
    channel_map = dict(host.channel_map)
    master_effects = list(engine.master_effects)
    master_gain = engine.master_gain
    # The public health getters may detect an exit and publish a failure.
    # Flow only observes the supervisor's already cached health instead.
    failure = engine._failure
    running = not engine._closed and failure is None and bool(engine._health[0])
    any_solo = any(track.solo for track in tracks if track is not None)
    midi_inputs = [(index + 1, ctrl.port_name) for index, ctrl in enumerate(host.midi_inputs)]
    open_names = [name for _, name in midi_inputs if name]
    input_name = host.midimix.input_port_name
    output_name = host.midimix.output_port_name
    banks = list(host.sequencer.banks)
    sample = host.timeline.cached_snapshot
    sequencer_running = host.sequencer._running and sample.running
    tempo = sample.bpm
    clock = (f"{sample.active_source}; {_value(tempo)} BPM; {sample.status}; "
             f"position {'confirmed' if sample.position_confirmed else 'unconfirmed'} (cached)")
    timing = "shared four-beat bar grid"
    beatstep_name = host.beatstep.input_port_name
    output_name_audio = format_output_device(engine.audio_output_name, engine.audio_output_index)
    backend = engine.backend_label or "(not opened)"
    sample_rate = engine.sample_rate
    buffer_size = engine.buffer_size
    channels = engine.output_channels
    output_format = "stereo (2 channels)" if channels == 2 else f"{channels} channels"
    audio_state = f"{'running' if running else 'stopped'}; failure: {failure or 'none'}"
    master_chain = " -> ".join(_plugin_name(effect) for effect in master_effects) or "(no master FX)"
    routes = [", ".join(f"ch {channel + 1}" for channel in range(16)
                        if channel_map.get(channel) == index) or "(no linked channels)"
              for index in range(NUM_TRACKS)]
    linked_banks = [[number + 1 for number, bank in enumerate(banks)
                     if bank is not None and bank.linked_track == index]
                    for index in range(NUM_TRACKS)]
    states = [_track_state(track, any_solo, running, failure, master_gain) for track in tracks]
    control_rows = [_control_rows(index, track, input_name, output_name)
                    for index, track in enumerate(tracks)]
    master_control = ["Master fader", f"CC {MASTER_FADER_CC} (0-127)", "Master gain",
                      f"{_value(master_gain)}; range 0 .. 1",
                      "active" if input_name else "inactive: MIDI Mix input disconnected"]

    overview = [
        ["MIDI inputs", "; ".join(open_names) or "(none connected)",
         "all generic inputs share the channel map"],
        ["MIDI Mix", f"input: {input_name or '(disconnected)'}; LED output: {output_name or '(disconnected)'}",
         "faders -> gains; knobs -> first 3 instrument sound controls; buttons -> mute/solo"],
        ["BeatStep Pro", beatstep_name or "(disconnected)",
         "SEQ1 -> track 3; SEQ2 -> track 4; DRUM -> track 5; CONTROL -> tracks 1/2"],
        ["Sequencer / clock", clock,
         f"{'running' if sequencer_running else 'stopped'}; {timing}; bank links below"],
    ]
    for index, track in enumerate(tracks):
        sources = routes[index]
        if linked_banks[index]:
            sources += "; banks " + ", ".join(map(str, linked_banks[index]))
        overview.append([f"Track {index + 1}", f"{sources} -> {_chain(track)} -> master sum",
                         states[index] + (f"; gain {_value(track.gain)}" if track else "")])
    overview.extend([
        ["Master", f"track sum -> {master_chain} -> master gain {_value(master_gain)}", "-> audio output"],
        ["Output", f"{output_name_audio}; {backend}; {output_format}",
         f"{audio_state}; {sample_rate} Hz; {buffer_size} frames/block"],
        ["Scope", "cached routing and applied control state only", "no live meters; no external audio input path"],
    ])
    sections = [_section("overview", "Overview", ["Stage", "Routing / chain", "State"], overview)]

    midi_rows = [[f"Input {number}", name or "(disconnected)",
                  "connected; shared channel map" if name else "disconnected"]
                 for number, name in midi_inputs]
    if not midi_inputs:
        midi_rows.append(["Generic MIDI inputs", "(none connected)", "no hardware discovery performed"])
    midi_rows.append(["Shared input messages", "note on/off, CC, pitch bend, channel aftertouch",
                      "each open generic input uses the same 16-channel map"])
    midi_rows.append(["BeatStep Pro", beatstep_name or "(disconnected)",
                      "dedicated device routing, independent of generic channel map"])
    for section, track_index in zip(("SEQ1", "SEQ2", "DRUM"), (2, 3, 4)):
        midi_rows.append([f"BeatStep Pro {section}", _target(tracks, track_index),
                          states[track_index] + ("; connected" if beatstep_name else "; disconnected")])
    for channel in range(16):
        target = channel_map.get(channel)
        state = ("unlinked; messages dropped" if target is None else
                 states[target] + ("; no connected generic input" if not open_names else ""))
        midi_rows.append([f"MIDI channel {channel + 1}", _target(tracks, target), state])
    midi_rows.extend([
        ["Sequencer", "direct to linked track (bypasses channel map)",
         f"{'running' if sequencer_running else 'stopped'}; one bar = 4 beats; {timing}"],
        ["Clock", clock, "peer tempo/activity not queried"],
    ])
    midi_rows.extend([
        [f"Deck note {note}", action,
         "routed generic input only; press once, note-off/velocity-zero releases nudge"]
        for note, action in DECK_NOTE_BINDINGS.items()
    ])
    midi_rows.extend([
        [f"Deck CC {DECK_PITCH_CC}", "pitch -16 .. +16%", "routed generic input; linear 0-127"],
        [f"Deck CC {DECK_SELECT_CC}", "track_select 1 .. N", "scaled 0-127; CLI selects every ordinal"],
    ])
    for index in range(NUM_SEQ_BANKS):
        bank = banks[index] if index < len(banks) else None
        target = bank.linked_track if bank is not None else None
        detail = _pattern(bank)
        if bank is not None:
            detail += f"; velocity {bank.velocity}"
        if target is not None:
            detail += "; " + states[target]
        elif bank is not None:
            detail += "; unlinked; no playback destination"
        midi_rows.append([f"Sequencer bank {index + 1}", _target(tracks, target), detail])
    sections.append(_section("midi", "MIDI routing & sequencer", ["Source / route", "Destination", "State / detail"], midi_rows))

    controls = [
        ["MIDI Mix input", input_name or "(disconnected)", "CC / note controls", "all MIDI channels accepted",
         "connected" if input_name else "disconnected"],
        ["MIDI Mix LED output", output_name or "(disconnected)", "mute / solo LED notes on channel 1",
         "cached applied state; on=127, off=0", "connected" if output_name else "disconnected"],
        ["Knob mapping", "CC 0-127 -> parameter range", "first three instrument sound controls in insertion order",
         "not track FX or master FX parameters", "unbound knobs are inactive"],
    ]
    for rows in control_rows:
        controls.extend(rows)
    controls.append(master_control)
    controls.append(["BeatStep Pro input", beatstep_name or "(disconnected)",
                     "CONTROL -> tracks 1/2", "dedicated device profile",
                     "connected" if beatstep_name else "disconnected"])
    controls.extend(host.beatstep.control_bindings())
    sections.append(_section("controls", "Hardware controls", ["Control", "Binding", "Target", "Cached value / range", "State"], controls))

    audio_rows = [
        ["Track audio", "instrument -> ordered track FX -> track gain / mute / solo -> sum", "internal sources only; no external audio input path"],
    ]
    for index, track in enumerate(tracks):
        audio_rows.append([f"Track {index + 1}", _chain(track), states[index]])
    audio_rows.extend([
        ["Master path", f"track sum -> {master_chain} -> gain {_value(master_gain)} -> output", "master FX run in displayed order"],
        ["Output device", output_name_audio,
         "opened PortAudio entry (cached)" if running else "last opened PortAudio entry; stopped"],
        ["Backend", backend, audio_state],
        ["Stream format", f"{sample_rate} Hz; {buffer_size} frames/block; {output_format}", "no live signal levels are collected by show"],
        ["Audio owner failure", failure or "none", "cached owner health"],
    ])
    sections.append(_section("audio", "Audio graph & output", ["Stage", "Path / value", "State / detail"], audio_rows))

    for index, track in enumerate(tracks):
        rows = [["State", states[index], "cached routing eligibility, not measured audibility"],
                ["MIDI channels", routes[index], "shared by all generic MIDI inputs"],
                ["Generic MIDI inputs", "; ".join(open_names) or "(none connected)", "channel routing above"],
                ["Sequencer banks", ", ".join(map(str, linked_banks[index])) or "(none linked)",
                 f"{'running' if sequencer_running else 'stopped'}; {timing}"]]
        for number in linked_banks[index]:
            bank = banks[number - 1]
            rows.append([f"Bank {number}", _pattern(bank), f"velocity {bank.velocity}; {clock}"])
        if track is None:
            rows.append(["Source", "(empty)", "channel and bank links may still target this track"])
        else:
            rows.extend([
                ["Source", f"{track.source_type}: {track.name}", track.path],
                ["Instrument", _plugin_name(track.plugin), "first in the audio chain"],
                ["Mixer", f"gain {_value(track.gain)}; mute {_value(track.muted)}; solo {_value(track.solo)}",
                 f"enabled {_value(track.enabled)}; any track solo {_value(any_solo)}"],
                ["Worker", f"PID {track.worker_pid if track.worker_pid is not None else '(none)'}",
                 f"failure: {track.worker_failure or 'none'}; missed blocks: {track.worker_misses}"],
                ["Worker buffering", f"{track.buffering_samples} samples", "cached worker configuration"],
            ])
            if track.source_type == "deck":
                for field in ("bank_id", "track_id", "track_title", "requested_track_id",
                              "request_id", "state", "play", "position", "duration",
                              "cue", "pitch", "nudge", "error"):
                    rows.append([f"Deck {field}", _value(track.deck.get(field, "(not cached)")),
                                 "cached transport; independent of Link"])
                for ordinal, entry in enumerate(track.deck.get("tracks", []), 1):
                    rows.append([f"Deck selection {ordinal}", entry.get("title") or entry["id"],
                                 entry["id"]])
            if track.plugin is not None:
                rows.extend([field, value, "cached instrument metadata"]
                            for field, value in plugin_metadata_rows(track.plugin)
                            if field != "Name" and (field != "Path" or value != track.path))
            if track.vcv_patch_path:
                rows.append(["VCV patch", track.vcv_patch_path, "loaded source patch"])
            if track.pd_patch_path:
                rows.append(["PD patch", track.pd_patch_path, "loaded source patch; external assets remain on disk"])
            for effect_index, effect in enumerate(track.effects):
                rows.append([f"Track FX {effect_index + 1}", _plugin_name(effect),
                             effect.path_to_plugin_file or "(no cached path)"])
            if not track.effects:
                rows.append(["Track FX", "(none)", "instrument directly to track mixer"])
        for control in control_rows[index]:
            label, binding, target, value, state = control
            rows.append([f"{label}: {binding}", f"{target}; {value}", state])
        rows.append(["Audio destination", "master sum -> master FX -> master gain -> output", states[index]])
        sections.append(_section(f"track{index + 1}", f"Track {index + 1}", ["Stage / control", "Path / cached value", "State / detail"], rows))
        if track is not None:
            for effect_index, effect in enumerate(track.effects, 1):
                sections.append(_section(
                    f"track{index + 1}fx{effect_index}",
                    f"Track {index + 1} FX {effect_index}",
                    ["Field", "Value"], plugin_metadata_rows(effect)))

    master_rows = [["Input", "sum of eligible track outputs", "track gains / mute / solo applied before sum"]]
    for index, effect in enumerate(master_effects):
        master_rows.append([f"Master FX {index + 1}", _plugin_name(effect), effect.path_to_plugin_file or "(no cached path)"])
    if not master_effects:
        master_rows.append(["Master FX", "(none)", "track sum directly to master gain"])
    master_rows.extend([
        ["Master gain", _value(master_gain), "range 0 .. 1; " + ("zero: output silenced" if master_gain == 0 else "applied after master FX")],
        ["Master fader", f"CC {MASTER_FADER_CC} (0-127) -> gain 0 .. 1", master_control[4]],
        ["Output", output_name_audio, f"{backend}; {audio_state}"],
        ["Stream", f"{sample_rate} Hz; {buffer_size} frames/block; {output_format}", "no live meters; no external audio input"],
    ])
    sections.append(_section("master", "Master bus & output", ["Stage", "Path / cached value", "State / detail"], master_rows))
    for index, effect in enumerate(master_effects, 1):
        sections.append(_section(
            f"masterfx{index}", f"Master FX {index}",
            ["Field", "Value"], plugin_metadata_rows(effect)))
    return {"title": "vcpi Signal Flow", "sections": sections}


def render_flow_snapshot(snapshot: dict, section_id: str | None = None) -> str:
    """Render all sections, or one section, as literal non-terminal text."""
    sections = [section for section in snapshot["sections"]
                if section_id is None or section["id"] == section_id]
    if not sections:
        raise ValueError(f"No loaded show target: {section_id}")
    lines = [snapshot["title"]]
    for section in sections:
        lines.extend(["", f"== {section['title']} =="])
        columns = section["columns"]
        rows = section["rows"]
        # Escape line breaks, not markup: a cell cannot inject extra table rows.
        text_rows = [[cell.replace("\r", "\\r").replace("\n", "\\n").replace("\t", "\\t")
                      for cell in row] for row in [columns, *rows]]
        widths = [max(len(row[index]) for row in text_rows) for index in range(len(columns))]
        for row_index, row in enumerate(text_rows):
            lines.append(" | ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip())
            if row_index == 0:
                lines.append("-+-".join("-" * width for width in widths))
    return "\n".join(lines)
