"""Command help metadata, introspection, and rendering."""

# Purpose, usage lines, arguments/options, and behavior. The same topic keys
# drive help completion, so nested help cannot drift into an unrelated topic.
HELP_TOPICS = {
    "help": ("Browse command help", "help\nhelp <command> [subcommand...]",
             "command/subcommand: a topic name; Tab lists valid topics.",
             "No arguments lists commands. Examples: help audio start; help track deck; help midi input close."),
    "track": ("Manage instruments and effects",
              "track <1-16> <deck|vcv|pd|clear> ...\ntrack <1-16|master> fx ...",
              "track: 1-based track number. --name <label>: optional display label for every source and effect.",
              "Quote paths and labels containing spaces. Use help track <type> for source-specific usage."),
    "track deck": ("Edit or control a deck playlist",
                   "track <1-16> deck <audio-file|directory> [--bpm-hint <bpm>] [--no-sync-filter] [--name <label>]\ntrack <1-16> deck reanalyze\ntrack <1-16> deck seek <seconds>\ntrack <1-16> deck cue [set]\ntrack <1-16> deck phase reset",
                   "audio-file|directory: WAV, AIFF, MP3 or FLAC file, or directory (quote spaces). Replaces the whole playlist and resets song memory; directories use filename order without scanning subdirectories. Sync-ready filtering is on by default: initial load skips entries without reliable beat grids, and controls skip pending/unavailable entries without renumbering. --no-sync-filter: allow manual/unreliable audio; saved with the deck. --bpm-hint: expected median tempo, finite 60–200 BPM, applied to every loaded song; guides analysis without forcing an exact BPM. --name: optional display label. Options may appear in any order. seconds: absolute source time.",
                   "Replacement retains the mixer, routing and inserts. Use track <1-16> clear to remove the whole deck. Each song remembers position, play/pause, cue, pitch, sync and phase_offset across selection and session restore. Unvisited songs start paused at zero. Detected beats follow global tempo and phase by default; momentary nudge is not saved."),
    "track deck reanalyze": ("Discard cached beats and analyze the current song again",
                            "track <1-16> deck reanalyze",
                            "track: 1-based loaded deck track. No additional arguments.",
                            "Pauses at the current position, removes this song's disk cache and bypasses its in-memory result. Analysis runs asynchronously; inspect show <track> until ready. Remains paused and retains cue, pitch, nudge, sync, phase_offset, playlist and effects. A fresh analysis can still reject the song."),
    "track deck seek": ("Seek a deck", "track <1-16> deck seek <seconds>",
                        "seconds: absolute source position in seconds.",
                        "Requests a seek while retaining play state. Use show <track> to inspect applied transport."),
    "track deck cue": ("Recall or store a deck cue", "track <1-16> deck cue\ntrack <1-16> deck cue set",
                       "set: store current source position; omitted: recall cue.",
                       "Recalling a cue pauses playback. Transport requests are asynchronous."),
    "track deck cue set": ("Store a deck cue", "track <1-16> deck cue set",
                           "track: 1-based loaded deck track.",
                           "Stores the current position. Related: help track deck cue."),
    "track deck phase": ("Correct a synced deck's phase",
                         "track <1-16> deck phase reset",
                         "phase_offset: -0.5..+0.5 beats; positive advances the deck, negative delays it.",
                         "Read with get 1 phase_offset or set with set 1 phase_offset <beats>. Synced nudge moves this offset while playing; releasing retains it without changing the shared BPM."),
    "track deck phase reset": ("Reset a deck's phase correction",
                               "track <1-16> deck phase reset",
                               "track: 1-based loaded deck track. No additional arguments.",
                               "Sets phase_offset to zero without seeking, pausing or changing BPM. Active sync smoothly realigns to the uncorrected grid."),
    "track vcv": ("Load a VCV patch", "track <1-16> vcv <patch[.vcv]> [--name <label>]",
                  "patch: patch name/path under the patches directory; quote spaces.\n--name: optional display label, otherwise the patch name.",
                  "Loads the patch using Cardinal. Related: midi link; get; set."),
    "track pd": ("Load a Pure Data patch", "track <1-16> pd <patch[.pd]> [--name <label>]",
                 "patch: patch name/path. --name: optional display label, otherwise the patch name.",
                 "Uses plugdata. Unquoted multiword patch names are supported; quote display labels. Related: midi link; get; set."),
    "track fx": ("Load or remove effects",
                 "track <1-16|master> fx <path|plugin name> [--name <label>]\ntrack <1-16|master> fx clear <fx_index>",
                 "target: track 1-16 or master. fx_index: 1-based chain position.\n--name: optional display label, otherwise plugin name.",
                 "Unquoted multiword VST names are supported. Effects run in chain order. Related: get; set; show."),
    "track fx clear": ("Remove an effect", "track <1-16|master> fx clear <fx_index>",
                       "fx_index: 1-based position in the target effect chain.",
                       "Removes only the selected effect. Related: show <target> fx <index>."),
    "track clear": ("Clear a track", "track <1-16> clear",
                    "track: 1-based track number.",
                    "Removes the instrument and its track state. Master supports only fx."),
    "get": ("Read cached plugin parameters",
            "get <1-16> [name|listing_flag]\nget <1-16> fx <fx_index> [name|listing_flag]\nget master [fx_index] [name|listing_flag]",
            "fx_index: 1-based; master defaults to 1. name: exact parameter name.\nListing flags: --controls (default), --settings, --midi, --unused, --all.",
            "Use one name or one flag, not both. Reports units, ranges and choices from cached metadata. For decks, get reads controls including seek position; set <track> seek <seconds> moves it while preserving play/pause. Use show <track> for operational info and --details for diagnostics and playlist paths. Examples: get 1 --all; get master 1 cutoff."),
    "set": ("Set a plugin parameter",
            "set <1-16> <name> <value>\nset <1-16> fx <fx_index> <name> <value>\nset master <fx_index> <name> <value>",
            "fx_index: 1-based and required for master. name: exact parameter name.\nvalue: native parameter units; booleans true/false; quote enum values containing spaces.",
            "Reports applied value. Unused slots cannot be set. Deck seek sets position in source seconds while preserving play/pause; track <track> deck seek <seconds> is equivalent. Deck index reports request acceptance, not completed loading. Example: set 1 index 2."),
    "focus": ("Read or select the shared focused track", "focus [1-16]",
              "track: 1-based track number; omitted reports the current focus. Empty tracks are selectable.",
              "Application-owned focus starts at track 1 and is shared with controllers. Selection does not change routing, playback or transport. Saved and restored with sessions."),
    "gain": ("Read or set mixer gain", "gain <1-16|master> [value]",
             "value: linear amplitude 0.0-1.0; omitted reads current gain.",
             "Reports actual gain after setting. Examples: gain 1; gain master 0.8."),
    "mute": ("Set or toggle track mute", "mute <1-16> [on|off|toggle]",
             "mode: on/off sets explicitly; toggle or omitted inverts current state.",
             "Reports actual state and refreshes controller LEDs. Example: mute 1 on."),
    "solo": ("Set or toggle track solo", "solo <1-16> [on|off|toggle]",
             "mode: on/off sets explicitly; toggle or omitted inverts current state.",
             "Reports actual state and refreshes controller LEDs. Example: solo 2 off."),
    "show": ("Inspect cached routing and controls",
             "show [all|midi|controls|audio|1-16|master] [--details]\nshow controls [midimix|beatstep] [--details]\nshow <1-16|master> fx <fx_index> [--details]",
             "Bare show prints a compact overview at the prompt.\nshow <track> includes deck transport, detected BPM, requested/active sync, errors and the ordered playlist with per-entry BPMs. The applied entry is marked current; a different requested entry is marked separately. Playlist analysis displays pending analysis, analyzing or unavailable until a reliable BPM is known. BPMs describe source audio, not shared/global tempo.\n--details adds metadata, diagnostics and inactive mappings. For decks it includes full diagnostics, grid confidence/offsets and playlist paths. fx_index: explicit 1-based effect position.",
             "Uses cached state without waiting for the worker; never queries live plugins or verifies hardware templates. Unreliable deck beat grids remain unsynced. Related: get (deck control values); status."),
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
             "port: zero-based hardware index or quoted name. number: 1-based active input number.\nchannel: 1-16. track: 1-16.",
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
                  "channel: 1-16. track: 1-16; both 1-based.",
                  "Replaces the channel route. Example: midi link 1 2. Related: midi cut; show midi."),
    "midi cut": ("Remove a MIDI route", "midi cut <channel>", "channel: 1-16, 1-based.",
                 "Removes the channel's track route without closing input ports."),
    "midimix": ("Connect MIDI Mix controls and LEDs", "midimix input <port>\nmidimix output <port>",
                "port: zero-based hardware port index or quoted name.",
                "Input handles fixed tracks 1-8 (no banking) and master; output provides LED feedback. Use CLI or routed generic MIDI for tracks 9-16. Related: midi ports."),
    "midimix input": ("Open MIDI Mix control input", "midimix input <port>",
                      "port: zero-based hardware input index or quoted name.",
                      'Example: midimix input "MIDI Mix". Related: midi ports input; midimix output.'),
    "midimix output": ("Open MIDI Mix LED feedback output", "midimix output <port>",
                       "port: zero-based hardware output index or quoted name.",
                       'Example: midimix output "MIDI Mix". Related: midi ports output; midimix input.'),
    "beatstep": ("Connect BeatStep Pro",
                 "beatstep\nbeatstep input [port]\nbeatstep close",
                 "port: zero-based hardware input index or quoted name; omitted identifies one BeatStep Pro.",
                 "Routes SEQ1/SEQ2/DRUM to tracks 3/4/5; CONTROL follows the shared focused track selected by step buttons or focus. Auto-discovered at startup. close disables reconnection; input enables it. No hardware template writes."),
    "beatstep input": ("Open BeatStep Pro", "beatstep input [port]",
                       "port: zero-based input index or quoted name; omitted discovers one device.",
                       "Dedicated input handles controls, notes, clock and transport. Do not also open the same port as generic MIDI input."),
    "beatstep close": ("Disconnect BeatStep Pro", "beatstep close", "No arguments.",
                       "Releases held controls and disables automatic reconnection. Clock continues internally without restarting a stopped transport."),
    "clock": ("Inspect or select timing authority", "clock\nclock source auto|internal|link|beatstep",
              "auto: BeatStep Pro clock, then Link peers, then internal timing.",
              "Shows selected/active source, sync status and whether musical position is confirmed. Source selection is saved; live position is not."),
    "clock source": ("Select timing authority", "clock source auto|internal|link|beatstep",
                     "source: timing authority; auto uses available external timing.",
                     "Connecting USB alone does not reset transport. Missing external clock holds over internally."),
    "transport": ("Control the shared sequencer transport",
                  "transport [start|stop|continue|align]",
                  "start: reset and run. stop: pause. continue: resume. align: establish the current downbeat.",
                  "Controls built-in sequences, not deck play/pause. Four beats per bar. Live transport position is not restored from sessions."),
    "note": ("Send a timed MIDI test note", "note <1-16> <midi_note> [--velocity <value>] [--duration-ms <value>]",
             "midi_note: integer 0-127. --velocity: integer 0-127, default 100.\n--duration-ms: positive finite milliseconds, default 300.",
             "Options may appear in either order, once each. Example: note 1 60 --duration-ms 500 --velocity 90."),
    "seq": ("Edit and route sequence banks",
            "seq\nseq <bank> [notes...]\nseq link <bank> <track>\nseq cut <track>\nseq clear <bank>",
            "bank: 1-16. track: 1-16. notes: note names with optional accidentals/octaves.",
            "Omitted notes reads a bank; no arguments lists banks. Notes are evenly spaced over a bar. Examples: seq 1 d c b a; seq 3 C#5 Bb4."),
    "seq link": ("Attach a sequence bank to a track", "seq link <bank> <track>",
                 "bank: 1-16. track: 1-16.", "Queues the pattern's first step for the next shared bar; follows global transport. Related: seq; clock; transport."),
    "seq cut": ("Detach sequences from a track", "seq cut <track>", "track: 1-16.",
                "Retains sequence bank contents. Related: seq link."),
    "seq clear": ("Clear a sequence bank", "seq clear <bank>", "bank: 1-16.",
                  "Removes the bank's notes and routing. Related: seq."),
    "tempo": ("Read or set tempo", "tempo [bpm]", "bpm: positive finite beats per minute; omitted reads current tempo.",
              "Updates the shared timeline tempo; external clock authority may constrain changes. Example: tempo 128. Related: clock; ableton."),
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
        ("Mixer", ("gain", "solo", "mute", "focus")),
        ("Audio devices & output", ("audio",)),
        ("MIDI ports & routing", ("midi", "midimix", "beatstep", "note")),
        ("Sequencing, tempo & Link", ("tempo", "clock", "transport", "ableton", "seq")),
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

