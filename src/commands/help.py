"""Command help metadata, introspection, and rendering."""

# Purpose, usage lines, arguments/options, and behavior. The same topic keys
# drive help completion, so nested help cannot drift into an unrelated topic.
HELP_TOPICS = {
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
                  "pack: directory under assets/samples. sample: filename, optionally without .wav.\n--name: display label; defaults to sample name.",
                  'Quote multiword pack/sample names. Example: track 1 wav "Drum Kit" "Deep Kick" --name Kick. Related: midi link; note.'),
    "track deck": ("Edit or control a deck playlist",
                   "track <1-8> deck <audio-file|directory> [--name <label>]\ntrack <1-8> deck remove <index>\ntrack <1-8> deck status\ntrack <1-8> deck seek <seconds>\ntrack <1-8> deck cue [set]",
                   "audio-file|directory: WAV, AIFF, MP3 or FLAC file, or directory (quote spaces). Directories add supported files in filename order, without scanning subdirectories. Already-listed resolved paths are skipped. Creates a paused deck or appends to its playlist. --name: optional display label. seconds: absolute source time.",
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


def command_entries(cli) -> list[tuple[str, str]]:
    """Return sorted (command, summary) rows for help output."""
    entries: list[tuple[str, str]] = []

    for attr in dir(cli.__class__):
        if not attr.startswith("do_"):
            continue

        command = attr[3:]
        if not command or command == "EOF" or command.startswith("_"):
            continue

        handler = getattr(cli, attr, None)
        if not callable(handler):
            continue

        if command == "exit":
            summary = "Alias for quit"
        else:
            summary = HELP_TOPICS[command][0]
            if command == "quit":
                summary += " (alias: exit)"

        entries.append((command, summary))

    entries.sort(key=lambda item: item[0])
    return entries


def show_help(ctx, arg, entries):
    """Show help: help [command]"""
    tokens = ctx.tokens(arg, "help [command [subcommand...]]")
    if tokens is None:
        return
    topic = " ".join(tokens)
    if topic:
        entry = HELP_TOPICS.get(topic)
        if entry is None:
            ctx.emit(f"No help for '{topic}'")
            return
        purpose, usage, arguments, behavior = entry
        ctx.emit(purpose + ".")
        for heading, text in (("Usage", usage), ("Arguments / Options", arguments),
                              ("Behavior", behavior)):
            ctx.emit(f"\n{heading}:")
            for line in text.splitlines():
                ctx.emit(f"  {line}")
        children = sorted(key for key in HELP_TOPICS
                          if key.startswith(topic + " ") and key.count(" ") == topic.count(" ") + 1)
        if children:
            ctx.emit("\nRelated help: " + "; ".join("help " + key for key in children))
        return

    entries = [
        (name, summary) for name, summary in entries
        if name != "exit"
    ]
    if not entries:
        ctx.emit("No commands available.")
        return

    width = max(len(name) for name, _ in entries)
    ctx.emit("Available commands:")
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
            ctx.emit("\n" + "-" * 60)
            continue
        title, commands = group
        rows = [(name, remaining[name]) for name in commands if name in remaining]
        if not rows:
            continue
        ctx.emit(f"\n{title}:")
        for name, summary in rows:
            ctx.emit(f"  {name:<{width}}  {summary}")
            remaining.pop(name)
    if remaining:
        ctx.emit("\nOther commands:")
        for name, summary in remaining.items():
            ctx.emit(f"  {name:<{width}}  {summary}")
    ctx.emit()
    ctx.emit("Tip: use 'help <command>' for detailed usage.")

