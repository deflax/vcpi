"""Finite CLI choices from daemon-owned snapshots and asset discovery.

Completion never executes a command or asks an audio/plugin owner for live state.
The replacement span is measured from the cursor, not from a shell word boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import shlex
from typing import TYPE_CHECKING

from core import midi
from core.models import NUM_SLOTS
from core.sequencer import NUM_SEQ_BANKS

if TYPE_CHECKING:
    from core.cli import HostCLI


_SLOTS = tuple(str(i) for i in range(1, NUM_SLOTS + 1))
_CHANNELS = tuple(str(i) for i in range(1, 17))
_BANKS = tuple(str(i) for i in range(1, NUM_SEQ_BANKS + 1))


@dataclass(frozen=True)
class _Word:
    value: str
    start: int


def _words(text: str, *, shell: bool) -> list[_Word] | None:
    """Keep raw replacement offsets while tolerating an unfinished last quote."""
    if not shell:
        words = [_Word(m.group(), m.start()) for m in re.finditer(r"\S+", text)]
    else:
        words = []
        start = None
        quote = None
        escaped = False
        for i, char in enumerate(text):
            if start is None:
                if char.isspace():
                    continue
                start = i
            if escaped:
                escaped = False
            elif char == "\\" and quote != "'":
                escaped = True
            elif quote:
                if char == quote:
                    quote = None
            elif char in ("'", '"'):
                quote = char
            elif char.isspace():
                try:
                    words.append(_Word(shlex.split(text[start:i])[0], start))
                except (ValueError, IndexError):
                    return None
                start = None
        if start is not None:
            if escaped:
                return None
            try:
                words.append(_Word(shlex.split(text[start:] + (quote or ""))[0], start))
            except (ValueError, IndexError):
                return None
        # Whitespace inside an open quote belongs to that word.
        if start is not None:
            return words
    if not words or text[-1:].isspace():
        words.append(_Word("", len(text)))
    return words


def _effects(cli: HostCLI, target: str) -> list:
    if target == "master":
        return cli.host.engine.master_effects
    if target not in _SLOTS:
        return []
    slots = cli.host.engine.slots
    index = int(target) - 1
    slot = slots[index] if index < len(slots) else None
    return slot.effects if slot is not None else []


def _indexes(items) -> list[str]:
    return [str(i) for i in range(1, len(items) + 1)]


def _port_indexes(direction: str, prefix: str) -> list[str]:
    # A mistyped/non-numeric port cannot match an index; do not probe hardware.
    if prefix and not prefix.isdecimal():
        return []
    try:
        ports = (midi.list_midi_input_ports() if direction == "input"
                 else midi.list_midi_output_ports())
    except Exception:
        # Hardware backends can be unavailable even when rtmidi is installed.
        # Keep unrelated grammar choices usable without opening any ports.
        return []
    return [str(i) for i in range(len(ports))]


def complete_line(cli: HostCLI, before: str) -> dict:
    """Return ready-to-insert choices and their cursor-relative replacement span."""
    empty = {"start_position": 0, "matches": []}
    if not isinstance(before, str) or any(
            (ord(c) < 32 and c != "\t") or ord(c) == 127 for c in before):
        return empty
    head = re.match(r"\s*(\S+)(\s*)", before)
    commands = [name for name, _ in cli._command_entries()]
    if head is None:
        return {"start_position": 0, "matches": commands}
    command = head.group(1)
    if not head.group(2):
        return {"start_position": -len(command),
                "matches": cli._filter_prefix(commands, command)}
    if command not in commands:
        return empty

    rest_start = head.end()
    rest = before[rest_start:]
    shell = command in ("get", "set")
    words = _words(rest, shell=shell)
    if words is None:
        return empty
    args = [word.value for word in words[:-1]]
    current = words[-1]
    prefix = current.value
    start = rest_start + current.start
    # Match the case handling of each existing execution parser.
    if args and command in ("midi", "midimix", "seq"):
        args[0] = args[0].lower()
    if command == "midi" and len(args) > 1 and args[0] == "input":
        args[1] = args[1].lower()

    def choices(values, *, quoted=shell, wanted=prefix, replace=start):
        values = [str(value) for value in values
                  if not any(ord(c) < 32 or ord(c) == 127 for c in str(value))]
        matches = cli._filter_prefix(sorted(set(values)), wanted)
        return {"start_position": replace - len(before),
                "matches": [shlex.quote(value) if quoted else value for value in matches]}

    def asset_tokens(values):
        return [value for value in values if value and not any(c.isspace() for c in value)]

    if command in ("save", "load"):
        # These parsers consume the entire raw argument, not shell tokens.
        return choices(cli._session_names(), quoted=False, wanted=rest, replace=rest_start)
    if command == "help":
        return choices(commands) if not args else empty
    if command in ("mute", "solo", "note", "gain"):
        if not args:
            return choices((*_SLOTS, "master") if command == "gain" else _SLOTS)
        return empty
    if command == "debug":
        return choices(("on", "off")) if not args else empty
    if command == "ableton":
        return choices(("link", "cut")) if not args else empty
    if command == "flow":
        return choices(("all", "midi", "controls", "audio", *_SLOTS, "master", "json", "explore")) if not args else empty
    if command == "audio":
        if not args:
            return choices(("start", "stop", "devices", "diagnose"))
        if args == ["start"] and (not prefix or prefix.isdecimal()):
            return choices(str(device["index"]) for device in cli.host.engine.audio_devices
                           if device["max_output_channels"] > 0)
        return empty
    if command == "midi":
        if not args:
            return choices(("ports", "input", "link", "cut"))
        if args == ["ports"]:
            return choices(("input", "output"))
        if args == ["input"]:
            return choices(["close", *_port_indexes("input", prefix)])
        if args == ["input", "close"]:
            return choices(_indexes(cli.host.midi_inputs))
        if len(args) == 1 and args[0] in ("link", "cut"):
            return choices(_CHANNELS)
        if len(args) == 2 and args[0] == "link" and args[1] in _CHANNELS:
            return choices(_SLOTS)
        return empty
    if command == "midimix":
        if not args:
            return choices(("input", "output"))
        if len(args) == 1 and args[0] in ("input", "output"):
            return choices(_port_indexes(args[0], prefix))
        return empty
    if command == "seq":
        if not args:
            return choices((*_BANKS, "link", "cut", "clear"))
        if len(args) == 1 and args[0] in ("link", "clear", "cut"):
            return choices(_SLOTS if args[0] == "cut" else _BANKS)
        if len(args) == 2 and args[0] == "link" and args[1] in _BANKS:
            return choices(_SLOTS)
        return empty
    if command == "slot":
        if not args:
            return choices((*_SLOTS, "master"))
        target = args[0]
        if target not in (*_SLOTS, "master"):
            return empty
        if len(args) == 1:
            return choices(("fx",) if target == "master" else cli.SLOT_TYPES)
        mode = args[1].lower()
        if mode == "fx" and len(args) == 3 and args[2].lower() == "clear":
            return choices(_indexes(_effects(cli, target)))
        if mode == "fx" and len(args) > 2 and args[2].lower() == "clear":
            return empty
        if mode in ("vst", "fx") and (target != "master" or mode == "fx"):
            # _parse_vst_args joins shell tokens, so replace the complete plugin
            # fragment, including already typed words (e.g. Surge X).
            plugin_start = words[2].start if len(words) > 2 else len(rest)
            fragment = rest[plugin_start:]
            plugin_words = _words(fragment, shell=True)
            if plugin_words is None:
                return empty
            tokens = [word.value for word in plugin_words]
            if "--name" in tokens:
                return empty
            if len(tokens) > 1 and tokens[-1].startswith("--"):
                if any(tokens[:-1]):
                    return choices(("--name",), quoted=True, wanted=tokens[-1],
                                   replace=rest_start + plugin_start + plugin_words[-1].start)
                return empty
            names = cli._vst_names()
            plugin_prefix = " ".join(tokens).rstrip()
            if fragment[-1:].isspace() and plugin_prefix in names:
                return choices(("--name",), quoted=True, wanted="", replace=len(before))
            values = [*names, "clear"] if mode == "fx" else names
            return choices(values, quoted=True, wanted=plugin_prefix,
                           replace=rest_start + plugin_start)
        if target == "master":
            return empty
        if mode == "vcv" and len(args) == 2:
            return choices(asset_tokens(cli._vcv_patch_names()))
        if mode == "wav":
            if len(args) == 2:
                return choices(asset_tokens(cli._sample_pack_names()))
            if len(args) == 3:
                return choices(asset_tokens(cli._sample_names(args[2])))
        return empty
    if command in ("info", "get", "set"):
        if not args:
            return choices((*_SLOTS, "master"))
        target = args[0]
        if target not in (*_SLOTS, "master"):
            return empty
        effects = _effects(cli, target)
        indexes = _indexes(effects)
        if target == "master" and len(args) == 1:
            if command in ("info", "set"):
                return choices(indexes)
            target_choices = indexes
        elif target != "master" and len(args) == 1:
            target_choices = ["fx"] if effects else []
        elif target != "master" and args[1] == "fx" and len(args) == 2:
            return choices(indexes)
        else:
            target_choices = []
        if command == "info":
            return choices(target_choices)
        try:
            _, _, plugin, remaining = cli._parameter_target(
                args, require_master_index=command == "set")
        except (ValueError, IndexError):
            return choices(target_choices)
        parameters = {name: parameter for name, parameter in plugin.parameters.items()
                      if not parameter.is_midi_cc_helper}
        if not remaining:
            names = list(parameters)
            if len(args) == 1:
                if target == "master":
                    # These strings select an effect index before a parameter.
                    names = [name for name in names if not name.lstrip("+-").isdigit()]
                else:
                    # Quoting cannot escape the instrument-target fx keyword.
                    names = [name for name in names if name != "fx"]
            return choices([*target_choices, *names])
        if command == "set" and len(remaining) == 1:
            parameter = parameters.get(remaining[0])
            if parameter is not None:
                if parameter.kind == "bool":
                    return choices(("true", "false"))
                if parameter.kind == "enum":
                    return choices(parameter.valid_values)
        return empty
    return empty
