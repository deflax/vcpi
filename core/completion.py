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
from core.models import NUM_TRACKS
from core.sequencer import NUM_SEQ_BANKS

if TYPE_CHECKING:
    from core.cli import HostCLI


_TRACKS = tuple(str(i) for i in range(1, NUM_TRACKS + 1))
_CHANNELS = tuple(str(i) for i in range(1, 17))
_BANKS = tuple(str(i) for i in range(1, NUM_SEQ_BANKS + 1))


@dataclass(frozen=True)
class _Word:
    value: str
    start: int


def _words(text: str) -> list[_Word] | None:
    """Keep raw replacement offsets while tolerating an unfinished last quote."""
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
        return words
    if not words or text[-1:].isspace():
        words.append(_Word("", len(text)))
    return words


def _effects(cli: HostCLI, target: str) -> list:
    if target == "master":
        return cli.host.engine.master_effects
    if target not in _TRACKS:
        return []
    tracks = cli.host.engine.tracks
    index = int(target) - 1
    track = tracks[index] if index < len(tracks) else None
    return track.effects if track is not None else []


def _indexes(items) -> list[str]:
    return [str(i) for i in range(1, len(items) + 1)]


def _port_choices(direction: str, prefix: str) -> list[str]:
    try:
        ports = (midi.list_midi_input_ports() if direction == "input"
                 else midi.list_midi_output_ports())
    except Exception:
        # Hardware backends can be unavailable even when rtmidi is installed.
        # Keep unrelated grammar choices usable without opening any ports.
        return []
    indexes = [str(i) for i in range(len(ports))]
    return indexes if prefix.isdecimal() else [*indexes, *ports]


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
    words = _words(rest)
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

    def choices(values, *, quoted=True, wanted=prefix, replace=start):
        values = [str(value) for value in values
                  if not any(ord(c) < 32 or ord(c) == 127 for c in str(value))]
        matches = cli._filter_prefix(sorted(set(values)), wanted)
        return {"start_position": replace - len(before),
                "matches": [shlex.quote(value) if quoted else value for value in matches]}


    if command in ("save", "load"):
        return choices(cli._session_names()) if not args else empty
    if command == "help":
        topics = [topic.split() for topic in cli._help_topics]
        return choices(parts[len(args)] for parts in topics
                       if len(parts) > len(args) and parts[:len(args)] == args)
    if command in ("mute", "solo", "note", "gain"):
        if not args:
            return choices((*_TRACKS, "master") if command == "gain" else _TRACKS)
        if command in ("mute", "solo") and len(args) == 1 and args[0] in _TRACKS:
            return choices(("on", "off", "toggle"))
        if command == "note" and len(args) >= 2 and args[0] in _TRACKS:
            flags = ("--velocity", "--duration-ms")
            options = args[2:]
            if len(options) % 2 or any(flag not in flags for flag in options[::2]):
                return empty
            if len(options[::2]) != len(set(options[::2])):
                return empty
            return choices(flag for flag in flags if flag not in options[::2])
        return empty
    if command == "debug":
        return choices(("on", "off")) if not args else empty
    if command == "ps":
        valid = ("--all", "--cpu", "--mem")
        if (any(arg not in valid for arg in args) or len(args) != len(set(args))
                or ("--cpu" in args and "--mem" in args)):
            return empty
        return choices(flag for flag in valid if flag not in args
                       and not (flag in ("--cpu", "--mem") and any(sort in args for sort in ("--cpu", "--mem"))))
    if command == "ableton":
        return choices(("link", "cut")) if not args else empty
    if command == "show":
        if not args:
            return choices(("all", "midi", "controls", "audio", *_TRACKS, "master"))
        target = args[0]
        if target not in (*_TRACKS, "master"):
            return empty
        effects = _effects(cli, target)
        if len(args) == 1:
            return choices(("fx",) if effects else ())
        if len(args) == 2 and args[1] == "fx":
            return choices(_indexes(effects))
        return empty
    if command == "audio":
        if not args:
            return choices(("start", "stop", "devices", "diagnose"))
        if args == ["start"]:
            devices = [device for device in cli.host.engine.audio_devices
                       if device["max_output_channels"] > 0]
            values = [str(device["index"]) for device in devices]
            if not prefix.isdecimal():
                values.extend(device["name"] for device in devices)
            return choices(values)
        return empty
    if command == "midi":
        if not args:
            return choices(("ports", "input", "link", "cut"))
        if args == ["ports"]:
            return choices(("input", "output"))
        if args == ["input"]:
            return choices(["close", *_port_choices("input", prefix)])
        if args == ["input", "close"]:
            return choices(_indexes(cli.host.midi_inputs))
        if len(args) == 1 and args[0] in ("link", "cut"):
            return choices(_CHANNELS)
        if len(args) == 2 and args[0] == "link" and args[1] in _CHANNELS:
            return choices(_TRACKS)
        return empty
    if command == "midimix":
        if not args:
            return choices(("input", "output"))
        if len(args) == 1 and args[0] in ("input", "output"):
            return choices(_port_choices(args[0], prefix))
        return empty
    if command == "seq":
        if not args:
            return choices((*_BANKS, "link", "cut", "clear"))
        if len(args) == 1 and args[0] in ("link", "clear", "cut"):
            return choices(_TRACKS if args[0] == "cut" else _BANKS)
        if len(args) == 2 and args[0] == "link" and args[1] in _BANKS:
            return choices(_TRACKS)
        return empty
    if command == "track":
        if not args:
            return choices((*_TRACKS, "master"))
        target = args[0]
        if target not in (*_TRACKS, "master"):
            return empty
        if len(args) == 1:
            return choices(("fx",) if target == "master" else cli.TRACK_TYPES)
        mode = args[1].lower()
        if mode == "fx" and len(args) == 3 and args[2].lower() == "clear":
            return choices(_indexes(_effects(cli, target)))
        if mode == "fx" and len(args) > 2 and args[2].lower() == "clear":
            return empty
        if mode in ("vst", "fx", "pd") and (target != "master" or mode == "fx"):
            # Plugin and PD patch parsers join shell tokens, so replace the
            # complete name fragment, including already typed words.
            plugin_start = words[2].start if len(words) > 2 else len(rest)
            fragment = rest[plugin_start:]
            plugin_words = _words(fragment)
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
            names = cli._pd_patch_names() if mode == "pd" else cli._vst_names()
            plugin_prefix = " ".join(tokens).rstrip()
            if fragment[-1:].isspace() and plugin_prefix in names:
                return choices(("--name",), quoted=True, wanted="", replace=len(before))
            values = [*names, "clear"] if mode == "fx" else names
            return choices(values, quoted=True, wanted=plugin_prefix,
                           replace=rest_start + plugin_start)
        if target == "master":
            return empty
        if mode == "deck":
            if len(args) == 2:
                return choices(("status", "seek", "cue", "remove"))
            if args[2:] == ["cue"]:
                return choices(("set",))
            if args[2:] == ["remove"]:
                track = cli.host.engine.tracks[int(target) - 1]
                if track is not None and track.source_type == "deck":
                    return choices(str(index) for index in range(1, len(track.deck["tracks"]) + 1))
                return empty
            if len(args) == 3 and args[2] not in ("status", "seek", "cue", "remove"):
                return choices(("--name",))
            return empty
        if mode == "vcv":
            if len(args) == 2:
                return choices(cli._vcv_patch_names())
            if len(args) == 3:
                return choices(("--name",))
        if mode == "wav":
            if len(args) == 2:
                return choices(cli._sample_pack_names())
            if len(args) == 3:
                return choices(cli._sample_names(args[2]))
            if len(args) == 4:
                return choices(("--name",))
        return empty
    if command in ("get", "set"):
        if not args:
            return choices((*_TRACKS, "master"))
        target = args[0]
        if target not in (*_TRACKS, "master"):
            return empty
        effects = _effects(cli, target)
        indexes = _indexes(effects)
        if target == "master" and len(args) == 1:
            if command == "set":
                return choices(indexes)
            target_choices = indexes
        elif target != "master" and len(args) == 1:
            target_choices = ["fx"] if effects else []
        elif target != "master" and args[1] == "fx" and len(args) == 2:
            return choices(indexes)
        else:
            target_choices = []
        try:
            track_index, fx_index, plugin, remaining = cli._parameter_target(
                args, require_master_index=command == "set")
        except (ValueError, IndexError):
            return choices(target_choices)
        if not remaining:
            names = [name for name, _ in plugin.parameter_items()]
            if command == "set":
                names.extend(name for name, _ in plugin.parameter_items("settings"))
            if len(args) == 1:
                if target == "master":
                    # These strings select an effect index before a parameter.
                    names = [name for name in names if not name.lstrip("+-").isdigit()]
                else:
                    # Quoting cannot escape the instrument-target fx keyword.
                    names = [name for name in names if name != "fx"]
            if command == "get":
                flags = ("--controls", "--settings", "--midi", "--unused", "--all")
                names = [name for name in names if name not in flags]
                names.extend(flags)
            return choices([*target_choices, *names])
        if command == "set" and len(remaining) == 1:
            parameter = plugin.parameters.get(remaining[0])
            if parameter is not None and plugin.parameter_group(remaining[0]) == "unused":
                return empty
            if parameter is not None:
                if (remaining[0] == "track_select" and track_index is not None
                        and fx_index is None):
                    track = cli.host.engine.tracks[track_index]
                    if track.source_type == "deck":
                        return choices(str(index) for index in
                                       range(1, len(track.deck.get("tracks", [])) + 1))
                if parameter.kind == "bool":
                    return choices(("true", "false"))
                if parameter.kind == "enum":
                    return choices(parameter.valid_values)
        return empty
    return empty
