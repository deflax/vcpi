"""Read-only routing snapshots and plain-text tables from control-side state.

No discovery, owner requests, parameter reads from native plugins, or routing
changes belong here. Parameter values are the last published owner snapshots,
not live meters or confirmation that a source is producing sound.
"""

from __future__ import annotations

from contextlib import nullcontext
from itertools import islice
from pathlib import Path

from controllers.akai_midimix import (
    FADER_CCS, KNOB_CCS, MASTER_FADER_CC, MUTE_NOTES, SOLO_NOTES,
)
from core.models import NUM_SLOTS
from core.sequencer import NUM_SEQ_BANKS, midi_to_note_name


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


def _target(slots, index) -> str:
    if index is None:
        return "unlinked"
    slot = slots[index]
    return f"Slot {index + 1}: {slot.name if slot is not None else '(empty)'}"


def _chain(slot) -> str:
    if slot is None:
        return "(empty)"
    return " -> ".join([_plugin_name(slot.plugin),
                        *[_plugin_name(effect) for effect in slot.effects]])


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


def _control_rows(index, slot, input_name, output_name) -> list[list[str]]:
    """Mirror the controller's instrument insertion-order mapping exactly."""
    label = f"Slot {index + 1}"
    state = "active" if input_name else "inactive: MIDI Mix input disconnected"
    if slot is None:
        state = "inactive: empty slot"
    rows = [[f"{label} fader", f"CC {FADER_CCS[index]} (0-127)",
             f"{label} gain", f"{_value(slot.gain) if slot else '-'}; range 0 .. 1", state]]
    parameters = (list(islice(slot.plugin.parameters.items(), 3))
                  if slot is not None and slot.plugin is not None else [])
    for knob, cc in enumerate(KNOB_CCS[index]):
        if knob < len(parameters):
            name, parameter = parameters[knob]
            detail = _parameter_detail(slot.plugin, name, parameter)
            knob_state = state
            if parameter.kind == "enum" and not parameter.valid_values:
                knob_state = "inactive: no enum values"
            target = f"{label} instrument: {name}"
        else:
            target, detail = f"{label}: (unbound)", "-"
            knob_state = "inactive: empty slot" if slot is None else "inactive: no instrument parameter"
        rows.append([f"{label} knob {knob + 1}", f"CC {cc} (0-127)",
                     target, detail, knob_state])
    for field, notes, enabled in (("mute", MUTE_NOTES, bool(slot and slot.muted)),
                                  ("solo", SOLO_NOTES, bool(slot and slot.solo))):
        led = f"LED {'on' if enabled else 'off'} (velocity {127 if enabled else 0})"
        led_state = "connected" if output_name else "disconnected"
        rows.append([f"{label} {field}", f"Note {notes[index]} (note-on velocity > 0)",
                     f"{label} {field} toggle", f"{_value(enabled)}; {led}",
                     f"{state}; LED output {led_state}, channel 1"])
    return rows


def _slot_state(slot, any_solo, running, failure, master_gain) -> str:
    if slot is None:
        return "empty; no audio source"
    blocked = []
    if failure:
        blocked.append("audio owner failed")
    if not running:
        blocked.append("audio stopped")
    if not slot.enabled:
        blocked.append("disabled")
    if slot.worker_failure:
        blocked.append(f"worker failed: {slot.worker_failure}")
    if slot.muted:
        blocked.append("muted")
    if any_solo and not slot.solo:
        blocked.append("solo-excluded")
    if slot.gain == 0:
        blocked.append("slot gain zero")
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

    Hold the engine's snapshot lock while reading cached plugin/slot metadata;
    this neither synchronizes with an owner nor probes MIDI/audio hardware.
    """
    engine = host.engine
    with getattr(engine, "state_lock", nullcontext()):
        return _build_flow_snapshot(host, engine)


def _build_flow_snapshot(host, engine) -> dict:
    slots = list(engine.slots)
    channel_map = dict(host.channel_map)
    master_effects = list(engine.master_effects)
    master_gain = engine.master_gain
    # The public health getters may detect an exit and publish a failure.
    # Flow only observes the supervisor's already cached health instead.
    failure = engine._failure
    running = not engine._closed and failure is None and bool(engine._health[0])
    any_solo = any(slot.solo for slot in slots if slot is not None)
    midi_inputs = [(index + 1, ctrl.port_name) for index, ctrl in enumerate(host.midi_inputs)]
    open_names = [name for _, name in midi_inputs if name]
    input_name = host.midimix.input_port_name
    output_name = host.midimix.output_port_name
    banks = list(host.sequencer.banks)
    sequencer_running = host.sequencer._running
    link_enabled = host.link.enabled
    # Link.bpm / num_peers may inspect the native Link object. Flow deliberately
    # uses the configured cached tempo, not a fresh native query.
    tempo = host.link._bpm
    clock = (f"Ableton Link {'enabled' if link_enabled else 'disabled'}; "
             f"configured tempo {_value(tempo)} BPM (cached)")
    timing = "Link beat-grid" if link_enabled else "local wall clock"
    output_name_audio = engine.audio_output_name or "(not opened)"
    backend = engine.backend_label or "(not opened)"
    sample_rate = engine.sample_rate
    buffer_size = engine.buffer_size
    channels = engine.output_channels
    output_format = "stereo (2 channels)" if channels == 2 else f"{channels} channels"
    audio_state = f"{'running' if running else 'stopped'}; failure: {failure or 'none'}"
    master_chain = " -> ".join(_plugin_name(effect) for effect in master_effects) or "(no master FX)"
    routes = [", ".join(f"ch {channel + 1}" for channel in range(16)
                        if channel_map.get(channel) == index) or "(no linked channels)"
              for index in range(NUM_SLOTS)]
    linked_banks = [[number + 1 for number, bank in enumerate(banks)
                     if bank is not None and bank.linked_slot == index]
                    for index in range(NUM_SLOTS)]
    states = [_slot_state(slot, any_solo, running, failure, master_gain) for slot in slots]
    control_rows = [_control_rows(index, slot, input_name, output_name)
                    for index, slot in enumerate(slots)]
    master_control = ["Master fader", f"CC {MASTER_FADER_CC} (0-127)", "Master gain",
                      f"{_value(master_gain)}; range 0 .. 1",
                      "active" if input_name else "inactive: MIDI Mix input disconnected"]

    overview = [
        ["MIDI inputs", "; ".join(open_names) or "(none connected)",
         "all generic inputs share the channel map"],
        ["MIDI Mix", f"input: {input_name or '(disconnected)'}; LED output: {output_name or '(disconnected)'}",
         "faders -> gains; knobs -> first 3 instrument parameters; buttons -> mute/solo"],
        ["Sequencer / clock", clock,
         f"{'running' if sequencer_running else 'stopped'}; {timing}; bank links below"],
    ]
    for index, slot in enumerate(slots):
        sources = routes[index]
        if linked_banks[index]:
            sources += "; banks " + ", ".join(map(str, linked_banks[index]))
        overview.append([f"Slot {index + 1}", f"{sources} -> {_chain(slot)} -> master sum",
                         states[index] + (f"; gain {_value(slot.gain)}" if slot else "")])
    overview.extend([
        ["Master", f"slot sum -> {master_chain} -> master gain {_value(master_gain)}", "-> audio output"],
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
    for channel in range(16):
        target = channel_map.get(channel)
        state = ("unlinked; messages dropped" if target is None else
                 states[target] + ("; no connected generic input" if not open_names else ""))
        midi_rows.append([f"MIDI channel {channel + 1}", _target(slots, target), state])
    midi_rows.extend([
        ["Sequencer", "direct to linked slot (bypasses channel map)",
         f"{'running' if sequencer_running else 'stopped'}; one bar = 4 beats; {timing}"],
        ["Clock", clock, "peer tempo/activity not queried"],
    ])
    for index in range(NUM_SEQ_BANKS):
        bank = banks[index] if index < len(banks) else None
        target = bank.linked_slot if bank is not None else None
        detail = _pattern(bank)
        if bank is not None:
            detail += f"; velocity {bank.velocity}"
        if target is not None:
            detail += "; " + states[target]
        elif bank is not None:
            detail += "; unlinked; no playback destination"
        midi_rows.append([f"Sequencer bank {index + 1}", _target(slots, target), detail])
    sections.append(_section("midi", "MIDI routing & sequencer", ["Source / route", "Destination", "State / detail"], midi_rows))

    controls = [
        ["MIDI Mix input", input_name or "(disconnected)", "CC / note controls", "all MIDI channels accepted",
         "connected" if input_name else "disconnected"],
        ["MIDI Mix LED output", output_name or "(disconnected)", "mute / solo LED notes on channel 1",
         "cached applied state; on=127, off=0", "connected" if output_name else "disconnected"],
        ["Knob mapping", "CC 0-127 -> parameter range", "first three instrument parameters in insertion order",
         "not slot FX or master FX parameters", "unbound knobs are inactive"],
    ]
    for rows in control_rows:
        controls.extend(rows)
    controls.append(master_control)
    sections.append(_section("controls", "Akai MIDI Mix controls", ["Control", "Binding", "Target", "Cached value / range", "State"], controls))

    audio_rows = [
        ["Slot audio", "instrument -> ordered slot FX -> slot gain / mute / solo -> sum", "internal sources only; no external audio input path"],
    ]
    for index, slot in enumerate(slots):
        audio_rows.append([f"Slot {index + 1}", _chain(slot), states[index]])
    audio_rows.extend([
        ["Master path", f"slot sum -> {master_chain} -> gain {_value(master_gain)} -> output", "master FX run in displayed order"],
        ["Output device", output_name_audio, "last selected cached name"],
        ["Backend", backend, audio_state],
        ["Stream format", f"{sample_rate} Hz; {buffer_size} frames/block; {output_format}", "no live signal levels are collected by flow"],
        ["Audio owner failure", failure or "none", "cached owner health"],
    ])
    sections.append(_section("audio", "Audio graph & output", ["Stage", "Path / value", "State / detail"], audio_rows))

    for index, slot in enumerate(slots):
        rows = [["State", states[index], "cached routing eligibility, not measured audibility"],
                ["MIDI channels", routes[index], "shared by all generic MIDI inputs"],
                ["Generic MIDI inputs", "; ".join(open_names) or "(none connected)", "channel routing above"],
                ["Sequencer banks", ", ".join(map(str, linked_banks[index])) or "(none linked)",
                 f"{'running' if sequencer_running else 'stopped'}; {timing}"]]
        for number in linked_banks[index]:
            bank = banks[number - 1]
            rows.append([f"Bank {number}", _pattern(bank), f"velocity {bank.velocity}; {clock}"])
        if slot is None:
            rows.append(["Source", "(empty)", "channel and bank links may still target this slot"])
        else:
            rows.extend([
                ["Source", f"{slot.source_type}: {slot.name}", slot.path],
                ["Instrument", _plugin_name(slot.plugin), "first in the audio chain"],
                ["Mixer", f"gain {_value(slot.gain)}; mute {_value(slot.muted)}; solo {_value(slot.solo)}",
                 f"enabled {_value(slot.enabled)}; any slot solo {_value(any_solo)}"],
                ["Worker", f"PID {slot.worker_pid if slot.worker_pid is not None else '(none)'}",
                 f"failure: {slot.worker_failure or 'none'}; missed blocks: {slot.worker_misses}"],
                ["Worker buffering", f"{slot.buffering_samples} samples", "cached worker configuration"],
            ])
            if slot.vcv_patch_path:
                rows.append(["VCV patch", slot.vcv_patch_path, "loaded source patch"])
            for effect_index, effect in enumerate(slot.effects):
                rows.append([f"Slot FX {effect_index + 1}", _plugin_name(effect),
                             effect.path_to_plugin_file or "(no cached path)"])
            if not slot.effects:
                rows.append(["Slot FX", "(none)", "instrument directly to slot mixer"])
        for control in control_rows[index]:
            label, binding, target, value, state = control
            rows.append([f"{label}: {binding}", f"{target}; {value}", state])
        rows.append(["Audio destination", "master sum -> master FX -> master gain -> output", states[index]])
        sections.append(_section(f"slot{index + 1}", f"Slot {index + 1}", ["Stage / control", "Path / cached value", "State / detail"], rows))

    master_rows = [["Input", "sum of eligible slot outputs", "slot gains / mute / solo applied before sum"]]
    for index, effect in enumerate(master_effects):
        master_rows.append([f"Master FX {index + 1}", _plugin_name(effect), effect.path_to_plugin_file or "(no cached path)"])
    if not master_effects:
        master_rows.append(["Master FX", "(none)", "slot sum directly to master gain"])
    master_rows.extend([
        ["Master gain", _value(master_gain), "range 0 .. 1; " + ("zero: output silenced" if master_gain == 0 else "applied after master FX")],
        ["Master fader", f"CC {MASTER_FADER_CC} (0-127) -> gain 0 .. 1", master_control[4]],
        ["Output", output_name_audio, f"{backend}; {audio_state}"],
        ["Stream", f"{sample_rate} Hz; {buffer_size} frames/block; {output_format}", "no live meters; no external audio input"],
    ])
    sections.append(_section("master", "Master bus & output", ["Stage", "Path / cached value", "State / detail"], master_rows))
    return {"title": "vcpi Signal Flow", "sections": sections}


def render_flow_snapshot(snapshot: dict, section_id: str | None = None) -> str:
    """Render all sections, or one section, as literal non-terminal text."""
    lines = [snapshot["title"]]
    for section in snapshot["sections"]:
        if section_id is not None and section["id"] != section_id:
            continue
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
