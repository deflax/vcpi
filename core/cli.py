"""Interactive command-line interface for vcpi.

All track numbers and MIDI channels are presented 1-based to the user
(tracks 1-8, MIDI channels 1-16) and converted to 0-based internally.
"""

from __future__ import annotations

import cmd
import logging
import math
import os
import shlex
from pathlib import Path

from core.completion import complete_line
from core.deps import HAS_LINK, HAS_RTMIDI, HAS_MIDO
from core.host import VcpiCore
from core.midi import list_midi_input_ports, list_midi_output_ports
from core.models import NUM_TRACKS
from core.logging_setup import set_log_level
from core.link import validate_bpm
from core.sequencer import NUM_SEQ_BANKS, midi_to_note_name
from core.processes import render_processes
from graph.signal_flow import build_flow_snapshot, render_flow_snapshot, show_view
from graph.status import format_output_device, format_realtime, render_status

# Purpose, usage lines, arguments/options, and behavior. The same topic keys
# drive help completion, so nested help cannot drift into an unrelated topic.
_HELP_TOPICS = {
    "help": ("Browse command help", "help\nhelp <command> [subcommand...]",
             "command/subcommand: a topic name; Tab lists valid topics.",
             "No arguments lists commands. Examples: help audio start; help track wav; help midi input close."),
    "track": ("Manage instruments and effects",
              "track <1-8> <vst|wav|deck|vcv|pd|clear> ...\ntrack <1-8|master> fx ...",
              "track: 1-based track number. --name <label>: optional display label for every source and effect.",
              "Quote paths and labels containing spaces. Use help track <type> for source-specific usage."),
    "track vst": ("Load a VST instrument", "track <1-8> vst <path|plugin name> [--name <label>]",
                  "path/plugin name: VST3 bundle or discovered name. --name: display label; defaults to plugin name.",
                  "Unquoted multiword plugin names are supported. Quote display labels. Example: track 1 vst Vital --name Bass."),
    "track wav": ("Load a WAV sample", "track <1-8> wav <pack> <sample> [--name <label>]",
                  "pack: directory under sampler/samples. sample: filename, optionally without .wav.\n--name: display label; defaults to sample name.",
                  'Quote multiword pack/sample names. Example: track 1 wav "Drum Kit" "Deep Kick" --name Kick. Related: midi link; note.'),
    "track deck": ("Edit or control a deck playlist",
                   "track <1-8> deck <audio-file> [--name <label>]\ntrack <1-8> deck remove <index>\ntrack <1-8> deck status\ntrack <1-8> deck seek <seconds>\ntrack <1-8> deck cue [set]",
                   "audio-file: WAV, AIFF, MP3 or FLAC path (quote spaces). Creates a paused deck or appends to its playlist. --name: optional display label. seconds: absolute source time.",
                   "Appending retains playback and effects. Playlists are saved in the session. New selections load paused at zero. Detected beats follow global/Link tempo and phase by default. Use set 1 sync false for manual pitch."),
    "track deck remove": ("Remove a playlist entry", "track <1-8> deck remove <index>",
                          "index: 1-based entry shown by deck status.",
                          "Removing the active entry selects the next entry, or the previous if last, paused at zero. Use track <1-8> clear to remove the final entry."),
    "track deck status": ("Read cached deck transport", "track <1-8> deck status",
                          "track: 1-based loaded deck track.",
                          "Shows selection, transport, detected bpm, beat_offset (seconds), beat_confidence, sync and sync_error. Unreliable beat grids remain unsynced. Reads cached state without waiting for the worker."),
    "track deck seek": ("Seek a deck", "track <1-8> deck seek <seconds>",
                        "seconds: absolute source position in seconds.",
                        "Requests a seek while retaining play state. Use track <track> deck status to inspect applied transport."),
    "track deck cue": ("Recall or store a deck cue", "track <1-8> deck cue\ntrack <1-8> deck cue set",
                       "set: store current source position; omitted: recall cue.",
                       "Recalling a cue pauses playback. Transport requests are asynchronous."),
    "track deck cue set": ("Store a deck cue", "track <1-8> deck cue set",
                           "track: 1-based loaded deck track.",
                           "Stores the current position. Related: help track deck cue."),
    "track vcv": ("Load a VCV patch", "track <1-8> vcv <patch[.vcv]> [--name <label>]",
                  "patch: patch name/path under the patches directory; quote spaces.\n--name: optional display label, otherwise the patch name.",
                  "Loads the patch using Cardinal. Related: midi link; get; set."),
    "track pd": ("Load a Pure Data patch", "track <1-8> pd <patch[.pd]> [--name <label>]",
                 "patch: patch name/path. --name: optional display label, otherwise the patch name.",
                 "Uses plugdata. Unquoted multiword patch names are supported; quote display labels. Related: midi link; get; set."),
    "track fx": ("Load or remove effects",
                 "track <1-8|master> fx <path|plugin name> [--name <label>]\ntrack <1-8|master> fx clear <fx_index>",
                 "target: track 1-8 or master. fx_index: 1-based chain position.\n--name: optional display label, otherwise plugin name.",
                 "Unquoted multiword VST names are supported. Effects run in chain order. Related: get; set; show."),
    "track fx clear": ("Remove an effect", "track <1-8|master> fx clear <fx_index>",
                       "fx_index: 1-based position in the target effect chain.",
                       "Removes only the selected effect. Related: show <target> fx <index>."),
    "track clear": ("Clear a track", "track <1-8> clear",
                    "track: 1-based track number.",
                    "Removes the instrument and its track state. Master supports only fx."),
    "get": ("Read cached plugin parameters",
            "get <1-8> [name|listing_flag]\nget <1-8> fx <fx_index> [name|listing_flag]\nget master [fx_index] [name|listing_flag]",
            "fx_index: 1-based; master defaults to 1. name: exact parameter name.\nListing flags: --controls (default), --settings, --midi, --unused, --all.",
            "Use one name or one flag, not both. Reports units, ranges and choices from cached metadata. Examples: get 1 --all; get master 1 cutoff."),
    "set": ("Set a plugin parameter",
            "set <1-8> <name> <value>\nset <1-8> fx <fx_index> <name> <value>\nset master <fx_index> <name> <value>",
            "fx_index: 1-based and required for master. name: exact parameter name.\nvalue: native parameter units; booleans true/false; quote enum values containing spaces.",
            "Reports applied value. Unused slots cannot be set. Deck track_select reports request acceptance, not completed loading. Example: set 1 cutoff 1000."),
    "gain": ("Read or set mixer gain", "gain <1-8|master> [value]",
             "value: linear amplitude 0.0-1.0; omitted reads current gain.",
             "Reports actual gain after setting. Examples: gain 1; gain master 0.8."),
    "mute": ("Set or toggle track mute", "mute <1-8> [on|off|toggle]",
             "mode: on/off sets explicitly; toggle or omitted inverts current state.",
             "Reports actual state and refreshes controller LEDs. Example: mute 1 on."),
    "solo": ("Set or toggle track solo", "solo <1-8> [on|off|toggle]",
             "mode: on/off sets explicitly; toggle or omitted inverts current state.",
             "Reports actual state and refreshes controller LEDs. Example: solo 2 off."),
    "show": ("Inspect signal routing and plugin metadata",
             "show [all|midi|controls|audio|1-8|master]\nshow <1-8|master> fx <fx_index>",
             "view: omitted opens the client's read-only explorer; plain output uses overview.\nfx_index: 1-based effect chain position. all: every cached table.",
             "Uses cached state; does not query live plugins. Related: get; status."),
    "audio": ("Manage audio output",
              "audio start [device]\naudio stop\naudio devices\naudio diagnose",
              "device: zero-based hardware index or quoted full output name; omitted uses preferred/default device.",
              "Loading a source does not start audio. Use help audio <subcommand> for details."),
    "audio start": ("Start audio output", "audio start [device]",
                    "device: zero-based hardware index or full name (quote spaces).\nDefault: preferred device, otherwise system default.",
                    'Opens output and reports the actual device/backend. Example: audio start "USB Audio". Related: audio devices; audio stop.'),
    "audio stop": ("Stop audio output", "audio stop", "No arguments or options.",
                   "Stops rendering without clearing loaded tracks. Related: audio start."),
    "audio devices": ("List audio devices", "audio devices", "No arguments or options.",
                      "Shows zero-based indexes, full names, channels, backend and selected device. Related: audio start."),
    "audio diagnose": ("Inspect audio and MIDI diagnostics", "audio diagnose", "No arguments or options.",
                       "Reports delivery counters and lifetime peak high-water marks, not current levels. Related: status."),
    "midi": ("Manage MIDI ports and routing",
             "midi ports input|output\nmidi input <port>\nmidi input close <number>\nmidi link <channel> <track>\nmidi cut <channel>",
             "port: zero-based hardware index or quoted name. number: 1-based active input number.\nchannel: 1-16. track: 1-8.",
             "Opening an input does not create routes. Related: help midi input; help midi link."),
    "midi ports": ("List MIDI hardware ports", "midi ports input\nmidi ports output",
                   "direction: input or output.", "Hardware port indexes start at zero, unlike active-input numbers. Related: midi input."),
    "midi ports input": ("List MIDI input ports", "midi ports input", "No additional arguments or options.",
                         "Lists zero-based hardware input indexes and names. Related: midi input."),
    "midi ports output": ("List MIDI output ports", "midi ports output", "No additional arguments or options.",
                          "Lists zero-based hardware output indexes and names. Related: midimix output."),
    "midi input": ("Open or close MIDI inputs", "midi input <port>\nmidi input close <number>",
                   "port: zero-based hardware index or quoted name. number: 1-based active-input number.",
                   'Opening adds an active input; closing uses its active number, not hardware index. Example: midi input "USB Keyboard". Related: status; midi link.'),
    "midi input close": ("Close an active MIDI input", "midi input close <number>",
                         "number: 1-based position among currently open inputs, shown by status.",
                         "Not a zero-based hardware port index. Example: midi input close 1. Remaining active inputs are renumbered."),
    "midi link": ("Route a MIDI channel to a track", "midi link <channel> <track>",
                  "channel: 1-16. track: 1-8; both 1-based.",
                  "Replaces the channel route. Example: midi link 1 2. Related: midi cut; show midi."),
    "midi cut": ("Remove a MIDI route", "midi cut <channel>", "channel: 1-16, 1-based.",
                 "Removes the channel's track route without closing input ports."),
    "midimix": ("Connect MIDI Mix controls and LEDs", "midimix input <port>\nmidimix output <port>",
                "port: zero-based hardware port index or quoted name.",
                "Input handles mixer controls; output provides LED feedback. Related: midi ports."),
    "midimix input": ("Open MIDI Mix control input", "midimix input <port>",
                      "port: zero-based hardware input index or quoted name.",
                      'Example: midimix input "MIDI Mix". Related: midi ports input; midimix output.'),
    "midimix output": ("Open MIDI Mix LED feedback output", "midimix output <port>",
                       "port: zero-based hardware output index or quoted name.",
                       'Example: midimix output "MIDI Mix". Related: midi ports output; midimix input.'),
    "note": ("Send a timed MIDI test note", "note <1-8> <midi_note> [--velocity <value>] [--duration-ms <value>]",
             "midi_note: integer 0-127. --velocity: integer 0-127, default 100.\n--duration-ms: positive finite milliseconds, default 300.",
             "Options may appear in either order, once each. Example: note 1 60 --duration-ms 500 --velocity 90."),
    "seq": ("Edit and route sequence banks",
            "seq\nseq <bank> [notes...]\nseq link <bank> <track>\nseq cut <track>\nseq clear <bank>",
            "bank: 1-16. track: 1-8. notes: note names with optional accidentals/octaves.",
            "Omitted notes reads a bank; no arguments lists banks. Notes are evenly spaced over a bar. Examples: seq 1 d c b a; seq 3 C#5 Bb4."),
    "seq link": ("Attach a sequence bank to a track", "seq link <bank> <track>",
                 "bank: 1-16. track: 1-8.", "Uses current tempo and Link timing. Related: seq; tempo."),
    "seq cut": ("Detach sequences from a track", "seq cut <track>", "track: 1-8.",
                "Retains sequence bank contents. Related: seq link."),
    "seq clear": ("Clear a sequence bank", "seq clear <bank>", "bank: 1-16.",
                  "Removes the bank's notes and routing. Related: seq."),
    "tempo": ("Read or set tempo", "tempo [bpm]", "bpm: positive finite beats per minute; omitted reads current tempo.",
              "Updates local/Link tempo without an arbitrary maximum. Example: tempo 128. Related: ableton."),
    "ableton": ("Control Ableton Link", "ableton link [bpm]\nableton cut",
                "bpm: positive finite beats per minute; omitted preserves current tempo.",
                "Link synchronizes tempo and phase with peers. Related: tempo; seq."),
    "ableton link": ("Enable Ableton Link", "ableton link [bpm]",
                     "bpm: positive finite beats per minute; omitted preserves current tempo.",
                     "Reports actual enabled tempo. Example: ableton link 128. Related: ableton cut."),
    "ableton cut": ("Disable Ableton Link", "ableton cut", "No additional arguments or options.",
                    "Disconnects Link; local tempo remains available. Related: tempo."),
    "save": ("Save a named session", "save [name]",
             "name: session name/path, optionally ending in .json; quote spaces.\nDefault: previously loaded named session.",
             "Relative names resolve under sessions/. The automatic session is written on shutdown. Example: save \"Live Set\". Related: load."),
    "load": ("Restore a named session", "load <name>",
             "name: session name/path, optionally ending in .json; quote spaces.",
             "Relative names resolve under sessions/. Restores saved audio state and updates the automatic session; named files change only on save. Related: save."),
    "status": ("Show overall host status", "status", "No arguments or options.",
               "Includes current session, audio, MIDI inputs and Link state. Related: show; audio diagnose."),
    "ps": ("Inspect processes and threads", "ps [--all] [--cpu|--mem]",
           "--all: include idle generic threads. --cpu: descending CPU. --mem: descending process RSS.\nDefault: process order; hidden idle generic threads.",
           "Samples about 200 ms. CPU 100% is one core; memory is MiB. Includes daemon descendants, not CLI client. Sort flags are mutually exclusive."),
    "debug": ("Read or set debug logging", "debug [on|off]",
              "mode: omitted reads logging state; on enables DEBUG; off restores INFO.",
              "Applies to daemon and live/new workers until exit. Native output is hidden unless enabled. Startup: LOG_LEVEL=DEBUG ./vcpid."),
    "deps": ("Report dependency availability", "deps", "No arguments or options.",
             "Reports native audio, aalink, python-rtmidi and mido availability."),
    "about": ("Show project information", "about", "No arguments or options.",
              "Displays the logo, project URL and license."),
    "quit": ("Exit the current CLI session", "quit", "No arguments or options.",
             "A socket client disconnects without stopping the daemon. Standalone CLI shuts down its host. Alias: exit. Related: shutdown."),
    "exit": ("Exit the current CLI session", "exit", "No arguments or options.",
             "Alias for quit. Socket clients leave the daemon running. Related: shutdown."),
    "shutdown": ("Shut down the host and server", "shutdown", "No arguments or options.",
                 "Stops the daemon, which may be restarted by its service manager. Unlike quit, affects every client."),
}


VCPI_ASCII_LOGO = r"""
                                 ███
                                ░░░
 █████ █████  ██████  ████████  ████
░░███ ░░███  ███░░███░░███░░███░░███
 ░███  ░███ ░███ ░░░  ░███ ░███ ░███
 ░░███ ███  ░███  ███ ░███ ░███ ░███
  ░░█████   ░░██████  ░███████  █████
   ░░░░░     ░░░░░░   ░███░░░  ░░░░░
                      ░███
                      █████
                     ░░░░░
""".strip("\n")


def _track_to_internal(user_track: int) -> int:
    """Convert 1-based user track to 0-based index, with validation."""
    if not 1 <= user_track <= NUM_TRACKS:
        raise ValueError(f"track must be 1-{NUM_TRACKS}")
    return user_track - 1


def _ch_to_internal(user_ch: int) -> int:
    """Convert 1-based user MIDI channel to 0-based, with validation."""
    if not 1 <= user_ch <= 16:
        raise ValueError("MIDI channel must be 1-16")
    return user_ch - 1


class HostCLI(cmd.Cmd):
    intro = (
        "\n"
        f"{VCPI_ASCII_LOGO}\n"
        "Type 'help' for available commands.\n"
        "Tracks are numbered 1-8.  MIDI channels are numbered 1-16."
    )
    prompt = "vcpi> "
    TRACK_TYPES = ("vst", "wav", "deck", "vcv", "pd", "fx", "clear")

    def __init__(self, host: VcpiCore, stdout=None, owns_host: bool = True):
        super().__init__(stdout=stdout)
        self.host = host
        # When True, quit/exit will call host.shutdown().
        # Set to False when running behind the socket server (the server
        # manages the host lifecycle).
        self._owns_host = owns_host
        # Set by `shutdown` so the socket server can terminate the daemon.
        self._shutdown_requested = False

    # -- helper for redirectable output --------------------------------------

    def _print(self, *args, **kwargs):
        """Print to self.stdout so output is captured in server mode."""
        kwargs.setdefault("file", self.stdout)
        print(*args, **kwargs)

    _help_topics = _HELP_TOPICS

    def _command_entries(self) -> list[tuple[str, str]]:
        """Return sorted (command, summary) rows for help output."""
        entries: list[tuple[str, str]] = []

        for attr in dir(self.__class__):
            if not attr.startswith("do_"):
                continue

            command = attr[3:]
            if not command or command == "EOF" or command.startswith("_"):
                continue

            handler = getattr(self, attr, None)
            if not callable(handler):
                continue

            if command == "exit":
                summary = "Alias for quit"
            else:
                summary = self._help_topics[command][0]
                if command == "quit":
                    summary += " (alias: exit)"

            entries.append((command, summary))

        entries.sort(key=lambda item: item[0])
        return entries

    def do_help(self, arg):
        """Show help: help [command]"""
        tokens = self._tokens(arg, "help [command [subcommand...]]")
        if tokens is None:
            return
        topic = " ".join(tokens)
        if topic:
            entry = self._help_topics.get(topic)
            if entry is None:
                self._print(f"No help for '{topic}'")
                return
            purpose, usage, arguments, behavior = entry
            self._print(purpose + ".")
            for heading, text in (("Usage", usage), ("Arguments / Options", arguments),
                                  ("Behavior", behavior)):
                self._print(f"\n{heading}:")
                for line in text.splitlines():
                    self._print(f"  {line}")
            children = sorted(key for key in self._help_topics
                              if key.startswith(topic + " ") and key.count(" ") == topic.count(" ") + 1)
            if children:
                self._print("\nRelated help: " + "; ".join("help " + key for key in children))
            return

        entries = [
            (name, summary) for name, summary in self._command_entries()
            if name != "exit"
        ]
        if not entries:
            self._print("No commands available.")
            return

        width = max(len(name) for name, _ in entries)
        self._print("Available commands:")
        groups = (
            ("Save & load sessions", ("save", "load")),
            ("Status, signal & logging", ("status", "ps", "deps", "debug")),
            ("Help, exit & shutdown", ("help", "about", "quit", "shutdown")),
            None,
            ("Overview", ("show",)),
            ("Instruments & effects", ("track", "get", "set")),
            ("Mixer", ("gain", "solo", "mute")),
            ("Audio devices & output", ("audio",)),
            ("MIDI ports & routing", ("midi", "midimix", "note")),
            ("Sequencing, tempo & Link", ("tempo", "ableton", "seq")),
        )
        remaining = dict(entries)
        for group in groups:
            if group is None:
                self._print("\n" + "-" * 60)
                continue
            title, commands = group
            rows = [(name, remaining[name]) for name in commands if name in remaining]
            if not rows:
                continue
            self._print(f"\n{title}:")
            for name, summary in rows:
                self._print(f"  {name:<{width}}  {summary}")
                remaining.pop(name)
        if remaining:
            self._print("\nOther commands:")
            for name, summary in remaining.items():
                self._print(f"  {name:<{width}}  {summary}")
        self._print()
        self._print("Tip: use 'help <command>' for detailed usage.")

    # -- plugins -------------------------------------------------------------

    def _track_usage(self) -> str:
        return (self._help_topics["track"][1]
                + "\nUse 'help track <type>' for source-specific options and examples.")

    @staticmethod
    def _source_args(tokens, count=None):
        """Separate a single explicit label from source arguments."""
        name = None
        if "--name" in tokens:
            marker = tokens.index("--name")
            if len(tokens) != marker + 2 or not tokens[-1] or tokens[-1].startswith("--"):
                raise ValueError("--name requires one label; quote labels containing spaces")
            name = tokens[-1]
            tokens = tokens[:marker]
        if any(token.startswith("--") for token in tokens):
            raise ValueError("unknown source option")
        if not tokens or (count is not None and len(tokens) != count):
            raise ValueError("incorrect source arguments; labels require --name")
        return tokens, name

    @staticmethod
    def _parse_vst_args(text: str) -> tuple[str, str | None]:
        tokens, name = HostCLI._source_args(shlex.split(text))
        return " ".join(tokens), name

    def _tokens(self, arg, usage):
        try:
            return shlex.split(arg)
        except ValueError as error:
            self._print(f"Error: {error}")
            self._print(f"Usage: {usage}")
            return None

    @staticmethod
    def _repo_root() -> Path:
        return Path(__file__).resolve().parent.parent

    def _samples_root(self) -> Path:
        cwd_samples = Path.cwd() / "sampler" / "samples"
        if cwd_samples.exists() and cwd_samples.is_dir():
            return cwd_samples
        return self._repo_root() / "sampler" / "samples"

    def _patches_root(self) -> Path:
        patches_dir = self.host.patches_dir
        if not patches_dir.is_absolute():
            patches_dir = Path.cwd() / patches_dir
        return patches_dir

    def _vst_search_dirs(self) -> list[Path]:
        env_tokens: list[str] = []
        for key in ("VST3_PATH", "VST_PATH"):
            raw = os.environ.get(key)
            if raw:
                env_tokens.extend(part for part in raw.split(os.pathsep) if part)

        candidates = [
            *env_tokens,
            str(Path.cwd()),
            str(self._repo_root()),
            "~/.vst3",
            "/usr/lib/vst3",
            "/usr/local/lib/vst3",
            "~/Library/Audio/Plug-Ins/VST3",
            "/Library/Audio/Plug-Ins/VST3",
        ]

        found: list[Path] = []
        seen: set[Path] = set()
        for token in candidates:
            path = Path(token).expanduser()
            try:
                resolved = path.resolve()
            except OSError:
                resolved = path
            if resolved in seen:
                continue
            seen.add(resolved)
            if resolved.exists() and resolved.is_dir():
                found.append(resolved)
        return found

    def _iter_vst_plugin_paths(self) -> list[Path]:
        plugins: dict[str, Path] = {}
        for base in self._vst_search_dirs():
            try:
                entries = list(base.iterdir())
            except OSError:
                continue
            for entry in entries:
                if entry.suffix.lower() != ".vst3":
                    continue
                plugins.setdefault(str(entry), entry)
        return sorted(plugins.values(), key=lambda p: p.name.lower())

    def _resolve_vst_token(self, token: str) -> str:
        token = token.strip()
        if not token:
            raise ValueError("VST path/name is required")

        path = Path(token).expanduser()
        if path.exists():
            return str(path)

        if token.startswith(("/", "./", "../", "~")):
            return str(path)

        key = token.lower()
        exact: list[Path] = []
        prefix: list[Path] = []

        for candidate in self._iter_vst_plugin_paths():
            stem = candidate.stem.lower()
            name = candidate.name.lower()

            if key in {stem, name} or (not key.endswith(".vst3") and f"{key}.vst3" == name):
                exact.append(candidate)
                continue

            if stem.startswith(key) or name.startswith(key):
                prefix.append(candidate)

        if len(exact) == 1:
            return str(exact[0])
        if len(exact) > 1:
            preview = ", ".join(sorted({p.stem for p in exact})[:5])
            raise ValueError(f"ambiguous VST name '{token}' (matches: {preview})")

        if len(prefix) == 1:
            return str(prefix[0])
        if len(prefix) > 1:
            preview = ", ".join(sorted({p.stem for p in prefix})[:5])
            raise ValueError(f"ambiguous VST name '{token}' (matches: {preview})")

        return token

    @staticmethod
    def _filter_prefix(values: list[str], prefix: str) -> list[str]:
        wanted = prefix.lower()
        return sorted(v for v in values if v.lower().startswith(wanted))

    def _sample_pack_names(self) -> list[str]:
        root = self._samples_root()
        if not root.exists() or not root.is_dir():
            return []
        packs: list[str] = []
        for entry in root.iterdir():
            if entry.is_dir() and not entry.name.startswith("."):
                packs.append(entry.name)
        return sorted(packs)

    def _sample_names(self, pack_name: str) -> list[str]:
        pack = Path(pack_name.strip().strip("/"))
        if pack.is_absolute() or ".." in pack.parts:
            return []

        pack_dir = self._samples_root() / pack
        if not pack_dir.exists() or not pack_dir.is_dir():
            return []

        samples: list[str] = []
        for wav in pack_dir.glob("*.wav"):
            samples.append(wav.stem)
        return sorted(samples)

    def _vcv_patch_names(self) -> list[str]:
        root = self._patches_root()
        if not root.exists() or not root.is_dir():
            return []

        patches: list[str] = []
        for patch_file in root.rglob("*.vcv"):
            try:
                rel = patch_file.relative_to(root).as_posix()
            except ValueError:
                continue
            if rel.lower().endswith(".vcv"):
                rel = rel[:-4]
            patches.append(rel)
        return sorted(patches)

    def _pd_patch_names(self) -> list[str]:
        root = self._patches_root()
        if not root.is_dir():
            return []
        return sorted(
            patch.relative_to(root).with_suffix("").as_posix()
            for patch in root.rglob("*.pd") if patch.is_file()
        )

    def _vst_names(self) -> list[str]:
        return sorted({path.stem for path in self._iter_vst_plugin_paths()})

    def cmdloop(self, intro=None):
        """Let readline replace a whole completion span, including plugin words."""
        if not self.use_rawinput or not self.completekey:
            return super().cmdloop(intro)
        try:
            import readline
        except ImportError:
            return super().cmdloop(intro)
        delimiters = readline.get_completer_delims()
        try:
            readline.set_completer_delims("\n")
            return super().cmdloop(intro)
        finally:
            readline.set_completer_delims(delimiters)

    def complete(self, text, state):
        """Bridge cmd.Cmd's readline API to the shared daemon grammar."""
        if state == 0:
            import readline

            before = readline.get_line_buffer()[:readline.get_endidx()]
            result = complete_line(self, before)
            start = len(before) + result["start_position"]
            preserved = before[readline.get_begidx():start]
            self.completion_matches = [
                preserved + match + " " for match in result["matches"]
            ]
        try:
            return self.completion_matches[state]
        except IndexError:
            return None

    def do_track(self, arg):
        """Load or clear track sources and effects, or control a deck."""
        text = arg.strip()
        if not text:
            self._print(f"Usage: {self._track_usage()}")
            return

        parts = self._tokens(text, self._track_usage())
        if parts is None:
            return
        target = parts[0]

        # Determine track index (or None for master).
        if target == "master":
            track_num = None
        else:
            try:
                track_num = int(target)
                _track_to_internal(track_num)  # validate
            except ValueError as e:
                self._print(f"Error: {e}")
                self._print(f"Usage: {self._track_usage()}")
                return

        if len(parts) < 2:
            self._print(f"Usage: {self._track_usage()}")
            return

        mode = parts[1].lower()

        # -- track <num> clear ------------------------------------------------
        if mode == "clear" and track_num is not None:
            if len(parts) != 2:
                self._print("Usage: track <1-8> clear")
                return
            idx = _track_to_internal(track_num)
            try:
                removed = self.host.remove_instrument(idx)
                self.host.refresh_mixer_leds([idx])
                self._print(f"  track {track_num} cleared ({removed.name})")
            except Exception as e:
                self._print(f"Error: {e}")
            return

        # -- track <num|master> fx ... ----------------------------------------
        if mode == "fx":
            if len(parts) < 3:
                self._print(f"Usage: track <1-8|master> fx <path|vst_name> [--name <label>] | track <1-8|master> fx clear <fx_index>")
                return

            track_idx = None if track_num is None else _track_to_internal(track_num)

            # track <num|master> fx clear <fx_index>
            if parts[2].lower() == "clear":
                if len(parts) != 4:
                    self._print(f"Usage: track <1-8|master> fx clear <fx_index>")
                    return
                try:
                    fx_idx = int(parts[3]) - 1
                except ValueError:
                    self._print("Error: fx_index must be a positive integer")
                    return
                if fx_idx < 0:
                    self._print("Error: fx_index must be >= 1")
                    return
                try:
                    self.host.remove_effect(track_idx, fx_idx)
                    self._print("  Removed.")
                except Exception as e:
                    self._print(f"Error: {e}")
                return

            # track <num|master> fx <path|vst_name> [--name <label>]
            try:
                path_token, name = self._parse_vst_args(text.split(maxsplit=2)[2])
                path = self._resolve_vst_token(path_token)
            except ValueError as e:
                self._print(f"Error: {e}")
                return
            try:
                self.host.load_effect(path, track_idx, name)
            except Exception as e:
                self._print(f"Error: {e}")
            return

        # Remaining modes require a numeric track.
        if track_num is None:
            self._print("Error: master only supports 'fx' subcommand")
            self._print(f"Usage: {self._track_usage()}")
            return

        idx = _track_to_internal(track_num)

        if mode == "deck":
            try:
                tokens = parts[2:]
                if not tokens:
                    raise ValueError("expected audio path, remove <index>, status, seek <seconds>, or cue [set]")
                action = tokens[0]
                if action in ("status", "seek", "cue", "remove"):
                    track = self.host.engine.tracks[idx]
                    if track is None or track.source_type != "deck":
                        raise ValueError(f"Track {track_num} is not a deck")
                    if tokens == ["status"]:
                        self._print_deck_status(track)
                    elif action == "seek" and len(tokens) == 2:
                        self.host.engine.deck_command(idx, "seek", float(tokens[1]))
                        self._print("  seek accepted")
                    elif tokens in (["cue"], ["cue", "set"]):
                        command = "cue_set" if len(tokens) == 2 else "cue"
                        self.host.engine.deck_command(idx, command)
                        self._print(f"  {command} accepted")
                    elif action == "remove" and len(tokens) == 2:
                        track = self.host.remove_deck_entry(idx, int(tokens[1]))
                        self._print_deck_status(track)
                    else:
                        raise ValueError("expected status, remove <index>, seek <seconds>, or cue [set]")
                else:
                    sources, name = self._source_args(tokens, 1)
                    track = self.host.load_deck(
                        idx, str(Path(sources[0]).expanduser()), name)
                    self._print(f"  track {track_num} = {track.name}")
                    self._print_deck_status(track)
            except Exception as error:
                self._print(f"Error: {error}")
            return

        # -- track <num> vst <path|name> [--name <label>] ----------------------
        if mode == "vst":
            rest = text.split(maxsplit=2)
            if len(rest) < 3:
                self._print(f"Usage: track <1-8> vst <path|vst_name> [--name <label>]")
                return
            try:
                path_token, name = self._parse_vst_args(rest[2])
                path = self._resolve_vst_token(path_token)
                track = self.host.load_instrument(idx, path, name)
                self._print(f"  track {track_num} = {track.name}")
                self._print(f"  vst      : {track.path}")
            except Exception as e:
                self._print(f"Error: {e}")
            return

        # -- track <num> vcv <patch> [--name <label>] -------------------------
        if mode == "vcv":
            try:
                sources, name = self._source_args(parts[2:], 1)
                patch_name = sources[0]
                track, patch_result, cardinal_path, patch_path = self.host.load_vcv_patch(
                    idx, patch_name, name=name,
                )
            except Exception as e:
                self._print(f"Error: {e}")
                return
            self._print(f"  track {track_num} = {track.name}")
            self._print(f"  patch    : {patch_path}")
            self._print(f"  cardinal : {cardinal_path}")
            self._print(f"  result   : {patch_result}")
            self._print(f"  route with: midi link <channel 1-16> {track_num}")
            return

        # -- track <num> pd <patch> [--name <label>] --------------------------
        if mode == "pd":
            rest = text.split(maxsplit=2)
            if len(rest) < 3:
                self._print("Usage: track <1-8> pd <patch_name[.pd]> [--name <label>]")
                return
            try:
                patch_name, name = self._parse_vst_args(rest[2])
                track, patch_result, plugdata_path, patch_path = self.host.load_pd_patch(
                    idx, patch_name, name=name,
                )
            except Exception as e:
                self._print(f"Error: {e}")
                return
            self._print(f"  track {track_num} = {track.name}")
            self._print(f"  patch    : {patch_path}")
            self._print(f"  plugdata : {plugdata_path}")
            self._print(f"  result   : {patch_result}")
            self._print(f"  route with: midi link <channel 1-16> {track_num}")
            return

        # -- track <num> wav <pack> <sample> [--name <label>] -------------------
        if mode == "wav":
            try:
                sources, display_name = self._source_args(parts[2:], 2)
            except ValueError as error:
                self._print(f"Error: {error}")
                self._print("Usage: track <1-8> wav <pack> <sample> [--name <label>]")
                return
            pack_name = sources[0].strip().strip("/")
            sample_name = sources[1].strip()

            if not pack_name:
                self._print("Error: pack name is required")
                return
            if not sample_name:
                self._print("Error: sample name is required")
                return

            pack_path = Path(pack_name)
            if pack_path.is_absolute() or ".." in pack_path.parts:
                self._print("Error: invalid pack name")
                return

            wav_file = sample_name if sample_name.lower().endswith(".wav") else f"{sample_name}.wav"
            sample_path = Path(wav_file)
            if sample_path.is_absolute() or ".." in sample_path.parts:
                self._print("Error: invalid sample name")
                return

            wav_path = Path("sampler") / "samples" / pack_path / sample_path

            try:
                track = self.host.load_wav(idx, str(wav_path), display_name)
            except Exception as e:
                self._print(f"Error: {e}")
                return

            self._print(f"  track {track_num} = {track.name}")
            self._print(f"  wav      : {track.path}")
            self._print(f"  route with: midi link <channel 1-16> {track_num}")
            return

        self._print(f"Error: unknown subcommand '{mode}'")
        self._print(f"Usage: {self._track_usage()}")

    def _print_deck_status(self, track):
        """Render only the daemon snapshot, never synchronizing with a worker."""
        status = track.deck
        self._print(f"  playlist_id = {status.get('bank_id', '(not cached)')}")
        for key in ("track_id", "track_title",
                    "requested_track_id", "request_id", "state", "play",
                    "position", "duration", "cue", "pitch", "nudge", "bpm",
                    "beat_offset", "beat_confidence", "sync", "sync_active",
                    "sync_error", "playback_speed", "beat_phase_error", "error"):
            self._print(f"  {key} = {status.get(key, '(not cached)')}")
        for ordinal, entry in enumerate(status.get("tracks", []), 1):
            self._print(f"  {ordinal}: {entry.get('title') or entry['id']} — {entry['path']}")

    def _parameter_target(self, parts, *, require_master_index=False):
        """Resolve a cached plugin and leave parameter/value arguments unconsumed."""
        if not parts:
            raise ValueError("missing plugin target")
        fx_idx = None
        if parts[0] == "master":
            idx = None
            consumed = 1
            fx_idx = 0
            if len(parts) > 1 and (require_master_index or parts[1].lstrip("+-").isdigit()):
                try:
                    fx_idx = int(parts[1]) - 1
                except ValueError:
                    raise ValueError("fx_index must be a positive integer") from None
                consumed = 2
            elif require_master_index:
                raise ValueError("missing master fx_index")
            effects = self.host.engine.master_effects
            if not effects:
                raise ValueError("No master effects loaded")
            if not 0 <= fx_idx < len(effects):
                raise ValueError("fx_index out of range")
            plugin = effects[fx_idx]
        else:
            idx = _track_to_internal(int(parts[0]))
            track = self.host.engine.tracks[idx]
            if track is None:
                raise ValueError(f"Track {parts[0]} is empty")
            consumed = 1
            plugin = track.plugin
            if len(parts) > 1 and parts[1] == "fx":
                if len(parts) < 3:
                    raise ValueError("missing track fx_index")
                try:
                    fx_idx = int(parts[2]) - 1
                except ValueError:
                    raise ValueError("fx_index must be a positive integer") from None
                if not 0 <= fx_idx < len(track.effects):
                    raise ValueError("fx_index out of range")
                plugin = track.effects[fx_idx]
                consumed = 3
        if plugin is None:
            raise ValueError("No plugin loaded")
        return idx, fx_idx, plugin, parts[consumed:]

    def do_get(self, arg):
        """Read cached plugin parameters, including units, ranges, and choices.

        get <track 1-8> [name | <listing flag>]
        get <track 1-8> fx <fx_index> [name | <listing flag>]
        get master [fx_index] [name | <listing flag>]

        Listing flags: --controls (default), --settings, --midi, --unused, --all.
        Use one flag or one exact parameter name, not both.
        Master defaults to its first effect. Reads use cached metadata only.
        Examples: get 1; get 1 --all; get 1 fx 2; get master 1 cutoff
        """
        try:
            idx, fx_idx, plugin, remaining = self._parameter_target(shlex.split(arg))
            if idx is not None and fx_idx is None and not remaining:
                track = self.host.engine.tracks[idx]
                if track.source_type == "deck":
                    self._print_deck_status(track)
            groups = {
                "controls": "Controls:",
                "settings": "Plugin settings:",
                "midi": "MIDI parameters:",
                "unused": "Unused parameters:",
            }
            group = "controls"
            if len(remaining) > 1:
                raise ValueError("expected one parameter name or one listing flag, not both")
            if remaining and remaining[0] in (
                    "--controls", "--settings", "--midi", "--unused", "--all"):
                group = remaining[0][2:]
                remaining = []
            if remaining:
                name = remaining[0]
                if name not in plugin.parameters:
                    raise ValueError(f"unknown parameter '{name}'")
                sections = [(None, [(name, plugin.parameters[name])])]
            elif group == "all":
                sections = [(heading, plugin.parameter_items(key))
                            for key, heading in groups.items()]
            else:
                sections = [(None, plugin.parameter_items(group))]
            for heading, parameters in sections:
                if heading:
                    self._print(heading)
                found = False
                for name, parameter in parameters:
                    found = True
                    value = plugin.values.get(name, parameter.string_value or "???")
                    units = f" {parameter.units}" if parameter.units else ""
                    details = ""
                    if parameter.kind == "enum" and parameter.valid_values:
                        details = f"  (choices {', '.join(parameter.valid_values)})"
                    elif parameter.kind in ("float", "int") and len(parameter.range) >= 2:
                        low, high = parameter.range[:2]
                        if low is not None and high is not None:
                            details = f"  (range {low:.4g} .. {high:.4g})"
                    self._print(f"  {name} = {value}{units}{details}")
                if not found:
                    self._print("  No parameters")
        except ValueError as error:
            self._print(f"Error: {error}")
            self._print("Usage: get <track> [fx <index>] [name | <listing flag>] | "
                        "get master [index] [name | <listing flag>]")
            self._print("Listing flags: --controls (default), --settings, --midi, --unused, --all")

    def do_set(self, arg):
        """Set a plugin parameter and show its actual applied value.

        set <track 1-8> <name> <value>
        set <track 1-8> fx <fx_index> <name> <value>
        set master <fx_index> <name> <value>

        Boolean values accept true/false; quote enum values containing spaces.
        Unused plugin slots cannot be set; explicit MIDI parameter names are allowed.
        Examples: set 1 cutoff 1000; set 1 fx 2 bypass true
                  set master 1 mode "Quiet Mode"
        """
        try:
            idx, fx_idx, plugin, remaining = self._parameter_target(
                shlex.split(arg), require_master_index=True)
            if len(remaining) != 2:
                raise ValueError("expected a parameter name and one value")
            pname, pval = remaining
            parameter = plugin.parameters.get(pname)
            if parameter is None:
                raise ValueError(f"unknown parameter '{pname}'")
            if plugin.parameter_group(pname) == "unused":
                raise ValueError(f"parameter '{pname}' is an unused plugin slot and cannot be set")
            if parameter.kind != "enum":
                try:
                    pval = float(pval)
                except ValueError:
                    if pval.lower() in ("true", "false"):
                        pval = pval.lower() == "true"
        except ValueError as error:
            self._print(f"Error: {error}")
            self._print("Usage: set <track> [fx <index>] <name> <value> | set master <index> <name> <value>")
            return
        try:
            actual = self.host.engine.set_parameter(idx, pname, pval, fx_index=fx_idx)
            if (idx is not None and fx_idx is None and pname == "track_select"
                    and self.host.engine.tracks[idx].source_type == "deck"):
                self._print(f"  track_select request accepted = {actual}; use deck status for applied selection")
            else:
                self._print(f"  {pname} = {actual}")
        except Exception as error:
            self._print(f"Error: {error}")

    # -- gain / mute / solo --------------------------------------------------


    def do_gain(self, arg):
        """Read or set track/master gain."""
        parts = self._tokens(arg, "gain <track 1-8|master> [value]")
        if parts is None:
            return
        if not 1 <= len(parts) <= 2:
            self._print("Usage: gain <track 1-8|master> [value]")
            return
        try:
            idx = None if parts[0] == "master" else _track_to_internal(int(parts[0]))
            track = None if idx is None else self.host.engine.tracks[idx]
            if idx is not None and track is None:
                raise ValueError(f"track {parts[0]} is empty")
            if len(parts) == 2:
                gain = float(parts[1])
                if not 0.0 <= gain <= 1.0:
                    raise ValueError("gain must be between 0.0 and 1.0")
                if idx is None:
                    self.host.engine.set_master_gain(gain)
                else:
                    track = self.host.engine.set_track_state(idx, gain=gain)
            gain = self.host.engine.master_gain if idx is None else track.gain
            self._print(f"  {parts[0]} gain = {gain:.2f}")
        except Exception as error:
            self._print(f"Error: {error}")

    def _switch_track(self, arg, command, field):
        usage = f"{command} <track 1-8> [on|off|toggle]"
        parts = self._tokens(arg, usage)
        if parts is None:
            return
        if not 1 <= len(parts) <= 2 or (len(parts) == 2 and parts[1] not in ("on", "off", "toggle")):
            self._print(f"Usage: {usage}")
            return
        try:
            idx = _track_to_internal(int(parts[0]))
            track = self.host.engine.tracks[idx]
            if track is None:
                raise ValueError(f"track {parts[0]} is empty")
            mode = parts[1] if len(parts) == 2 else "toggle"
            value = not getattr(track, field) if mode == "toggle" else mode == "on"
            track = self.host.engine.set_track_state(idx, **{field: value})
            self.host.refresh_mixer_leds([idx])
            self._print(f"  {track.name}: {command} {'on' if getattr(track, field) else 'off'}")
        except Exception as error:
            self._print(f"Error: {error}")

    def do_mute(self, arg):
        """Set or toggle track mute."""
        self._switch_track(arg, "mute", "muted")

    def do_solo(self, arg):
        """Set or toggle track solo."""
        self._switch_track(arg, "solo", "solo")

    # -- routing / MIDI ------------------------------------------------------

    def do_show(self, arg):
        """Inspect routing and plugin metadata: show [all|midi|controls|audio|1-8|master]

        show <track> includes instrument metadata and the track's routing/mixer state.
        show <track> fx <index> or show master fx <index> inspects one effect.
        With no argument, open the interactive client's read-only tabbed explorer.
        Plain output uses the compact overview; all prints every cached table.
        """
        try:
            view = show_view(shlex.split(arg))
            snapshot = build_flow_snapshot(self.host)
            self._print(render_flow_snapshot(snapshot, None if view == "all" else view))
        except ValueError as error:
            self._print(f"Error: {error}")


    # -- audio ---------------------------------------------------------------

    def do_audio(self, arg):
        """Audio commands: audio start [device] | audio stop | audio devices | audio diagnose

        Subcommands:
          audio start [device]  -- start audio engine
          audio stop            -- stop audio engine
          audio devices         -- list available output devices
          audio diagnose        -- inspect MIDI delivery and lifetime signal peaks
        """
        parts = self._tokens(arg, "audio start [device] | audio stop | audio devices | audio diagnose")
        if parts is None:
            return
        if not parts:
            self._print("Usage: audio start [device] | audio stop | audio devices | audio diagnose")
            return

        sub = parts[0]
        if (sub == "start" and len(parts) > 2) or (sub != "start" and len(parts) != 1):
            self._print("Usage: audio start [device] | audio stop | audio devices | audio diagnose")
            return

        if sub == "start":
            dev = " ".join(parts[1:]) if len(parts) > 1 else None
            if dev and dev.isdigit():
                dev = int(dev)
            try:
                self.host.start_audio(dev)
                engine = self.host.engine
                device = format_output_device(engine.audio_output_name, engine.audio_output_index)
                self._print(f"  Audio started: {device} | backend={engine.backend_label}")
            except Exception as e:
                self._print(f"Error: {e}")

        elif sub == "stop":
            self.host.stop_audio()

        elif sub == "devices":
            devices = self.host.engine.audio_devices
            if not devices:
                self._print("  No audio devices available.")
            for device in devices:
                engine = self.host.engine
                selected = device["index"] == engine.audio_output_index
                marker = (" [active]" if engine.running else " [last opened]") if selected else ""
                self._print(
                    f"  [{device['index']}] {device['name']} "
                    f"({device['max_input_channels']} in, "
                    f"{device['max_output_channels']} out; {device['hostapi_name']}){marker}"
                )

        elif sub == "diagnose":
            try:
                diagnosis = self.host.engine.request("diagnose")
            except Exception as e:
                self._print(f"Error: {e}")
                return
            output = diagnosis["output"]
            self._print(
                f"  Output: {format_output_device(output['output_name'], output['output_index'])} "
                f"| backend={output['backend_label']} "
                f"| running={output['running']} "
                f"| master gain={output['master_gain']:.3f}"
            )
            self._print(f"  Output realtime: {format_realtime(output['realtime'])}")
            self._print(
                f"  Callbacks={output['callback_count']} "
                f"| render errors={output['render_errors']} "
                f"| underruns={output['underruns']} "
                f"| deadline misses={output['deadline_misses']}"
            )
            self._print("  Peaks are absolute-sample high-water marks, not current levels.")
            self._print(
                f"  Output peak since audio-owner creation "
                f"(final device samples): {output['output_peak']:.6g}"
            )
            if not diagnosis["tracks"]:
                self._print("  No loaded tracks.")
            for track in diagnosis["tracks"]:
                self._print(
                    f"  Track {track['index']}: {track['name']} "
                    f"| gain={track['gain']:.3f} "
                    f"| muted={track['muted']} | solo={track['solo']}"
                )
                self._print(
                    f"    MIDI sent={track['midi_sent']} "
                    f"consumed={track['midi_consumed']} pending={track['midi_pending']} "
                    f"| missing blocks={track['misses']}"
                )
                self._print(
                    f"    Audio peak since track creation "
                    f"(before mixer gain/mute/solo): {track['audio_peak']:.6g}"
                )
                self._print(f"    Worker failure: {track['worker_failure'] or 'none'}")
                self._print(f"    Realtime: {format_realtime(track['realtime'])}")

        else:
            self._print(f"Unknown audio subcommand: {sub}")

    # -- MIDI (unified) ------------------------------------------------------

    def do_midi(self, arg):
        """MIDI commands: midi ports input | midi ports output | midi input <port> | midi input close <index> | midi link <ch> <track> | midi cut <ch>

        Subcommands:
          midi ports input       -- list MIDI input ports
          midi ports output      -- list MIDI output ports
          midi input <port>      -- open a MIDI input port
          midi input close <idx> -- close a MIDI input by index
          midi link <ch> <track>  -- route MIDI channel to track
          midi cut <ch>          -- remove MIDI channel route
        """
        parts = self._tokens(arg, "midi ports input|output | midi input <port> | midi input close <number> | midi link <ch> <track> | midi cut <ch>")
        if parts is None:
            return
        if not parts:
            self._print(
                "Usage: midi ports input | midi ports output | "
                "midi input <port> | midi input close <index> | "
                "midi link <ch> <track> | midi cut <ch>"
            )
            return

        sub = parts[0].lower()

        # --- midi ports input / midi ports output ---------------------------
        if sub == "ports":
            if len(parts) != 2:
                self._print("Usage: midi ports input | midi ports output")
                return
            direction = parts[1].lower()
            if direction == "input":
                ports = list_midi_input_ports()
                if not ports:
                    self._print("  No MIDI input ports found.")
                    return
                for i, name in enumerate(ports):
                    self._print(f"  [{i}] {name}")
            elif direction == "output":
                ports = list_midi_output_ports()
                if not ports:
                    self._print("  No MIDI output ports found.")
                    return
                for i, name in enumerate(ports):
                    self._print(f"  [{i}] {name}")
            else:
                self._print("Usage: midi ports input | midi ports output")
            return

        # --- midi input <port> / midi input close <index> -------------------
        if sub == "input":
            if len(parts) < 2:
                self._print("Usage: midi input <port> | midi input close <index>")
                return
            if parts[1].lower() == "close":
                if len(parts) != 3:
                    self._print("Usage: midi input close <index>")
                    return
                try:
                    idx = int(parts[2]) - 1
                    if not 0 <= idx < len(self.host.midi_inputs):
                        raise ValueError("active input number must be 1 through the number of open inputs")
                    self.host.close_midi_input(idx)
                    self._print("  Closed.")
                except Exception as e:
                    self._print(f"Error: {e}")
            else:
                if len(parts) != 2:
                    self._print("Usage: midi input <port index|name>; quote names containing spaces")
                    return
                try:
                    ctrl = self.host.open_midi_input(parts[1])
                    self._print(f"  Opened: {ctrl.port_name}")
                except Exception as e:
                    self._print(f"Error: {e}")
            return

        # --- midi link <ch> <track> ------------------------------------------
        if sub == "link":
            if len(parts) != 3:
                self._print("Usage: midi link <channel 1-16> <track 1-8>")
                return
            try:
                ch = _ch_to_internal(int(parts[1]))
                idx = _track_to_internal(int(parts[2]))
            except ValueError as e:
                self._print(f"Error: {e}")
                return
            self.host.route(ch, idx)
            self._print(f"  ch {parts[1]} -> track {parts[2]}")
            return

        # --- midi cut <ch> --------------------------------------------------
        if sub == "cut":
            if len(parts) != 2:
                self._print("Usage: midi cut <channel 1-16>")
                return
            try:
                ch = _ch_to_internal(int(parts[1]))
            except ValueError as e:
                self._print(f"Error: {e}")
                return
            self.host.unroute(ch)
            self._print(f"  ch {parts[1]} unlinked")
            return

        self._print(
            "Unknown midi subcommand. Use: ports, input, link, cut"
        )

    def do_midimix(self, arg):
        """Open MIDI Mix control input or LED feedback output."""
        parts = self._tokens(arg, "midimix input|output <port index|name>")
        if parts is None:
            return
        if len(parts) != 2 or parts[0].lower() not in ("input", "output"):
            self._print("Usage: midimix input|output <port index|name>")
            return
        try:
            port = int(parts[1]) if parts[1].isdigit() else parts[1]
            if parts[0].lower() == "input":
                self.host.open_mixer_midi(port)
            else:
                self.host.open_mixer_midi_out(port)
        except Exception as error:
            self._print(f"Error: {error}")

    def do_note(self, arg):
        """Send a timed MIDI test note."""
        usage = "note <track 1-8> <midi_note> [--velocity <0..127>] [--duration-ms <milliseconds>]"
        parts = self._tokens(arg, usage)
        if parts is None:
            return
        try:
            if len(parts) < 2:
                raise ValueError("track and MIDI note are required")
            idx = _track_to_internal(int(parts[0]))
            note = int(parts[1])
            options = {}
            for offset in range(2, len(parts), 2):
                flag = parts[offset]
                if flag not in ("--velocity", "--duration-ms") or flag in options or offset + 1 == len(parts):
                    raise ValueError("expected each note option once with one value")
                options[flag] = parts[offset + 1]
            velocity = int(options.get("--velocity", "100"))
            duration = float(options.get("--duration-ms", "300"))
            if not 0 <= note <= 127 or not 0 <= velocity <= 127:
                raise ValueError("MIDI note and velocity must be integers from 0 to 127")
            if not math.isfinite(duration) or duration <= 0:
                raise ValueError("duration-ms must be a positive finite number")
            self.host.send_note(idx, note, velocity, duration / 1000.0)
        except (ValueError, OverflowError) as error:
            self._print(f"Error: {error}")
            self._print(f"Usage: {usage}")

    # -- sequencer -----------------------------------------------------------

    def do_seq(self, arg):
        """Sequencer: seq | seq <bank> <notes...> | seq link <bank> <track> | seq cut <track> | seq clear <bank>

        Examples:
          seq                  -- show all sequence banks
          seq 1 d c b a        -- set bank 1 to D C B A (4 notes per bar)
          seq 1 c              -- set bank 1 to just C (1 note per bar)
          seq 3 C#5 Bb4        -- sharps/flats and octave suffixes work
          seq clear 1          -- clear bank 1
          seq link 1 5         -- attach sequence bank 1 to track 5
          seq cut 5            -- remove sequences from track 5
        """
        parts = self._tokens(arg, "seq [bank [notes...]] | seq link <bank> <track> | seq cut <track> | seq clear <bank>")
        if parts is None:
            return

        # No args: display all banks
        if not parts:
            seq = self.host.sequencer
            any_bank = False
            for bi, bank in enumerate(seq.banks):
                if bank is None:
                    continue
                any_bank = True
                notes_str = " ".join(midi_to_note_name(n) for n in bank.notes)
                if bank.linked_track is not None:
                    link_str = f" -> track {bank.linked_track + 1}"
                else:
                    link_str = ""
                self._print(f"  seq {bi + 1}: {notes_str}{link_str}")
            if not any_bank:
                self._print("  No sequences defined. Use: seq <bank> <note> [note ...]")
            return

        # --- seq link <bank> <track> -----------------------------------------
        if parts[0].lower() == "link":
            if len(parts) != 3:
                self._print("Usage: seq link <bank 1-16> <track 1-8>")
                return
            try:
                bi = int(parts[1]) - 1
                if not 0 <= bi < NUM_SEQ_BANKS:
                    raise ValueError(f"bank must be 1-{NUM_SEQ_BANKS}")
            except ValueError as e:
                self._print(f"Error: {e}")
                return
            try:
                si = _track_to_internal(int(parts[2]))
            except ValueError as e:
                self._print(f"Error: {e}")
                return
            try:
                self.host.sequencer.link(bi, si)
                self._print(f"  seq {bi + 1} -> track {si + 1}")
            except ValueError as e:
                self._print(f"Error: {e}")
            return

        # --- seq cut <track> -------------------------------------------------
        if parts[0].lower() == "cut":
            if len(parts) != 2:
                self._print("Usage: seq cut <track 1-8>")
                return
            try:
                si = _track_to_internal(int(parts[1]))
            except ValueError as e:
                self._print(f"Error: {e}")
                return
            self.host.sequencer.detach_track(si)
            self._print(f"  track {si + 1}: sequences detached")
            return

        # --- seq clear <bank> -----------------------------------------------
        if parts[0].lower() == "clear":
            if len(parts) != 2:
                self._print(f"Usage: seq clear <bank 1-{NUM_SEQ_BANKS}>")
                return
            try:
                bank_num = int(parts[1])
                bi = bank_num - 1
                if not 0 <= bi < NUM_SEQ_BANKS:
                    raise ValueError
            except ValueError:
                self._print(f"Error: bank must be 1-{NUM_SEQ_BANKS}")
                return
            self.host.sequencer.clear_bank(bi)
            self._print(f"  seq {bank_num}: cleared")
            return

        # --- remaining forms need a bank number as first arg ----------------
        try:
            bank_num = int(parts[0])
            bi = bank_num - 1
            if not 0 <= bi < NUM_SEQ_BANKS:
                raise ValueError
        except ValueError:
            self._print(f"Error: bank must be 1-{NUM_SEQ_BANKS}")
            return

        # seq <bank> -- show single bank
        if len(parts) == 1:
            bank = self.host.sequencer.banks[bi]
            if bank is None:
                self._print(f"  seq {bank_num}: (empty)")
            else:
                notes_str = " ".join(midi_to_note_name(n) for n in bank.notes)
                if bank.linked_track is not None:
                    link_str = f" -> track {bank.linked_track + 1}"
                else:
                    link_str = ""
                self._print(f"  seq {bank_num}: {notes_str}{link_str}")
            return

        # seq <bank> <note> [note ...]
        note_names = parts[1:]
        try:
            bank = self.host.sequencer.set_bank(bi, note_names)
        except ValueError as e:
            self._print(f"Error: {e}")
            return

        notes_str = " ".join(midi_to_note_name(n) for n in bank.notes)
        n = len(bank.notes)
        if n == 1:
            timing = "1 note/bar"
        else:
            timing = f"{n} notes/bar (1/{n} spacing)"
        self._print(f"  seq {bank_num}: {notes_str}  ({timing})")

    # -- link ----------------------------------------------------------------

    def do_ableton(self, arg):
        """Ableton Link commands: ableton link [bpm] | ableton cut

        Subcommands:
          ableton link [bpm]  -- enable Ableton Link (optionally set BPM)
          ableton cut         -- disable Ableton Link
        """
        parts = self._tokens(arg, "ableton link [bpm] | ableton cut")
        if parts is None:
            return
        if not parts:
            self._print("Usage: ableton link [bpm] | ableton cut")
            return

        sub = parts[0]
        if (sub == "link" and len(parts) > 2) or (sub != "link" and len(parts) != 1):
            self._print("Usage: ableton link [bpm] | ableton cut")
            return

        if sub == "link":
            if len(parts) > 1:
                try:
                    bpm = validate_bpm(parts[1])
                except ValueError as error:
                    self._print(f"Error: {error}")
                    return
            else:
                bpm = None
            try:
                self.host.start_link(bpm)
                self._print(f"  Link enabled at {self.host.link.bpm:.1f} BPM")
            except Exception as e:
                self._print(f"Error: {e}")

        elif sub == "cut":
            self.host.stop_link()
            self._print("  Link disabled")

        else:
            self._print(f"Unknown ableton subcommand: {sub}")

    def do_tempo(self, arg):
        """Get/set tempo: tempo [bpm]"""
        parts = self._tokens(arg, "tempo [bpm]")
        if parts is None:
            return
        if len(parts) > 1:
            self._print("Usage: tempo [bpm]")
            return
        if parts:
            try:
                self.host.link.bpm = validate_bpm(parts[0])
            except ValueError as error:
                self._print(f"Error: {error}")
                return
        self._print(f"  {self.host.link.bpm:.1f} BPM")

    def do_debug(self, arg):
        """Show or toggle daemon debug logging: debug [on|off]

        debug on enables DEBUG logging in the daemon and live audio workers.
        debug off returns logging to INFO. New workers inherit the setting.
        Changes last until the daemon exits; logs stay in its terminal/service log.
        Raw native/plugin output is discarded unless debug is on, then shown with
        [daemon], [audio], or [track N] prefixes. No per-process log files are kept.
        For startup diagnostics, launch with LOG_LEVEL=DEBUG ./vcpid.
        """
        mode = arg.strip().lower()
        if mode not in ("", "on", "off"):
            self._print("Usage: debug [on|off]")
            return
        if mode:
            level = "DEBUG" if mode == "on" else "INFO"
            try:
                self.host.engine.request("set_log_level", level=level)
            except Exception as error:
                self._print(f"Error: {error}")
                return
            set_log_level(level)
        level = logging.getLogger().getEffectiveLevel()
        self._print(f"  Debug logging: {'on' if level <= logging.DEBUG else 'off'} "
                    f"(log level: {logging.getLevelName(level)})")


    # -- session -------------------------------------------------------------

    def _sessions_root(self) -> Path:
        """Return the sessions directory (CWD or repo root)."""
        cwd_sessions = Path.cwd() / "sessions"
        if cwd_sessions.exists() and cwd_sessions.is_dir():
            return cwd_sessions
        return self._repo_root() / "sessions"

    def _resolve_session_path(self, name: str) -> Path:
        """Resolve a session name to a full path in the sessions directory."""
        if name.lower().endswith(".json"):
            name = name[:-5]
        return self._sessions_root() / f"{name}.json"

    def _session_names(self) -> list[str]:
        """List available session names for tab completion."""
        root = self._sessions_root()
        if not root.exists() or not root.is_dir():
            return []
        return sorted(
            f.stem for f in root.glob("*.json")
            if f.is_file() and not f.name.startswith(".")
        )


    def do_save(self, arg):
        """Save session: save [<name>]

        Saves the current session to sessions/<name>.json.
        If no name is given, saves to the previously loaded session.
        The automatic session at ~/.config/vcpi/session.json is only
        written on shutdown.
        """
        parts = self._tokens(arg, "save [name]")
        if parts is None:
            return
        if len(parts) > 1:
            self._print("Usage: save [name]; quote names containing spaces")
            return
        name = parts[0] if parts else ""
        if not name:
            if self.host.loaded_session_path is None:
                self._print("Usage: save <name>  (no session loaded)")
                return
            path = self.host.loaded_session_path
        else:
            path = self._resolve_session_path(name)
        try:
            self.host.save_session(str(path))
            self._print(f"  Saved to {path}")
        except Exception as e:
            self._print(f"Error: {e}")

    def do_load(self, arg):
        """Load session: load <name>

        Loads from sessions/<name>.json and sets it as the active
        session (shown in ``status``).  The auto-save file at
        ~/.config/vcpi/session.json is updated so that shutdown
        preserves the loaded state, but the named session file is
        only written when you explicitly run ``save``.
        """
        parts = self._tokens(arg, "load <name>")
        if parts is None:
            return
        if len(parts) != 1 or not parts[0]:
            self._print("Usage: load <name>; quote names containing spaces")
            return
        name = parts[0]
        path = self._resolve_session_path(name)
        if not path.exists():
            self._print(f"Error: session not found: {path}")
            return
        try:
            self.host.restore_session(str(path))
            self.host.refresh_mixer_leds()
            # Track the loaded session for save-without-args and status.
            self.host.loaded_session_name = name
            self.host.loaded_session_path = path
            # Update the auto-save session so shutdown preserves this state.
            self.host.save_session()
            self._print(f"  Loaded from {path}")
        except Exception as e:
            self._print(f"Error: {e}")

    # -- status --------------------------------------------------------------

    def do_about(self, arg):
        """Show vcpi logo and project info: about"""
        if arg.strip():
            self._print("Usage: about")
            return

        self._print(VCPI_ASCII_LOGO)
        self._print()
        self._print("vcpi - Native VST3 host with Ableton Link tempo sync")
        self._print("License: AGPL-3.0 (see LICENSE); no warranty.")
        self._print("Redistribution is permitted under the license.")
        self._print("Source: https://github.com/deflax/vcpi")

    def do_status(self, arg):
        """Overall status: status"""
        if arg.strip():
            self._print("Usage: status")
            return
        self._print(render_status(self.host))

    def do_ps(self, arg):
        """Show processes and threads: ps [--all] [--cpu | --mem]

        Includes the daemon and all descendants (audio, tracks, helpers), not
        the separate CLI client. Samples CPU over ~200 ms; 100% is one core.
        Process rows show total CPU, resident memory (MiB), state and thread
        count. Thread rows show native TIDs and CPU, not separate memory.
        Idle generically named threads are hidden; ps --all shows every thread.
        Main threads and known native render threads remain visible.
        Sort descending by CPU (including threads) or process RSS.
        """
        options = self._tokens(arg, "ps [--all] [--cpu | --mem]")
        if options is None:
            return
        if (any(option not in ("--all", "--cpu", "--mem") for option in options)
                or len(options) != len(set(options))
                or ("--cpu" in options and "--mem" in options)):
            self._print("Usage: ps [--all] [--cpu | --mem]")
            return
        sort_by = "cpu" if "--cpu" in options else "mem" if "--mem" in options else None
        try:
            self._print(render_processes(self.host, show_all="--all" in options,
                                         sort_by=sort_by))
        except (OSError, RuntimeError) as exc:
            self._print(f"Error collecting process resources: {exc}")

    def do_deps(self, arg):
        """Check dependencies."""
        if arg.strip():
            self._print("Usage: deps")
            return
        native = self.host.engine.native_dependencies
        for name, ok in [("native audio (JUCE/PortAudio)", native["native"]),
                         ("aalink", HAS_LINK), ("python-rtmidi", HAS_RTMIDI),
                         ("mido", HAS_MIDO)]:
            self._print(f"  {name}: {'OK' if ok else 'MISSING'}")

    def do_quit(self, arg):
        """Exit the current CLI session."""
        if arg.strip():
            self._print("Usage: quit")
            return
        if self._owns_host:
            self.host.shutdown()
        return True

    def do_shutdown(self, arg):
        """Shutdown host/server process (for systemd restart)."""
        if arg.strip():
            self._print("Usage: shutdown")
            return

        self._shutdown_requested = True
        self._print("  Shutting down vcpi...")

        if self._owns_host:
            self.host.shutdown()

        return True

    do_exit = do_quit
    do_EOF = do_quit
