# vcpi Usage Reference

This reference covers command-line startup flags and interactive `vcpi>`
commands.

Source of truth:

- `core/main.py`
- `core/cli.py`

## Conventions

- Slots are **1-8**.
- MIDI channels are **1-16**.
- `fx_index` values are **1-based** in CLI commands.
- `master` means the global master effects bus.

## Startup Modes

| Command | What it does |
|---|---|
| `./vcsrv` | Starts headless server mode; bootstraps `.venv` on first run |
| `./vcli` | Connects interactive client mode; bootstraps `.venv` on first run |
| `python main.py serve` | Starts headless server with Unix socket control |
| `python main.py cli` | Connects to a running headless server |

Default socket path (server and client):

```text
$XDG_RUNTIME_DIR/vcpi/vcpi.sock
# fallback: /tmp/vcpi-<uid>/vcpi.sock
# root fallback: /run/vcpi/vcpi.sock
```

## Host Startup Flags

These flags apply when starting the host server (`python main.py serve`).

| Flag | Default | Description |
|---|---|---|
| `--sr` | `44100` | Audio sample rate |
| `--buf` | `512` | Audio buffer size |
| `--bpm` | `120.0` | Initial tempo |
| `--link` | off | Enable Ableton Link on startup |
| `--midi-in` | unset | MIDI input port index(es) to open on startup (repeatable) |
| `--mix-midi` | unset | MIDI Mix port index to open on startup |
| `--mix-midi-out` | unset | MIDI Mix output port index (LED feedback) |
| `--output` | unset | Preferred output audio device index or name (not auto-started) |
| `--session` | `~/.config/vcpi/session.json` | Session file path |
| `--no-restore` | off | Skip session restore at startup |

When running `serve`, vcpi does not start audio automatically. Start audio
manually from the client with `audio start [device]`.

Only one server instance per user is allowed. If a server is already running
(e.g. via systemd `payload.service`), `./vcsrv` will refuse to start and
print the existing PID. Connect to the running instance with `./vcli` instead.
The PID file is stored at `~/.config/vcpi/vcpi.pid`.

Logging level is controlled by environment variable `LOG_LEVEL`.
Core default is `WARNING`. `./vcsrv` defaults it to `DEBUG` if unset.
The Raspberry Pi `payload.service` also sets `LOG_LEVEL=DEBUG`.

VST3 plugin search and name resolution:

- `load vst` and `load fx` accept a plugin **name** (e.g. `Dexed`) or a full
  path (e.g. `/usr/lib/vst3/Dexed.vst3`). Names are resolved by scanning
  known search directories.
- Search order: `vst3/` in the repo root, `~/.vst3`,
  `/usr/lib/vst3`, `/usr/local/lib/vst3`, and macOS standard paths.
- Name matching: exact stem match first, then case-insensitive, then unique
  prefix match. Ambiguous names produce an error listing the candidates.
- Tab completion for `load vst` and `load fx` lists all detected plugin names.
- Run `./vst3/fetch-vsts-amd64` on x86_64/amd64 systems.
- Run `./vst3/fetch-vsts-aarch64` on Raspberry Pi / Linux aarch64 systems.
- Override with `VST3_PATH` or `VST_PATH` environment variables.

Cardinal/VCV helpers:

- `load vcv` looks for patch files in `patches/` by default.
- `load vcv` does not auto-route channels; use `midi link <ch> <slot>` explicitly.
- Override patch directory with `VCPI_PATCHES_DIR`.
- Override Cardinal plugin path with `CARDINAL_VST3_PATH`.

Examples:

```bash
LOG_LEVEL=INFO ./vcsrv
LOG_LEVEL=WARNING python main.py serve
```

Server/client specific:

| Mode | Flag | Default | Description |
|---|---|---|---|
| `serve` | `--sock` | auto (see above) | Unix socket to bind |
| `cli` | `--sock` | auto (see above) | Unix socket to connect to |

## Interactive Commands

When connected with `./vcli` (or `python main.py cli`), press `Tab` to
autocomplete command names. `load` also has context-aware argument completion:

- `load` -> `vst`, `wav`, `vcv`, `fx`
- `load wav` -> sample pack names and sample names
- `load vcv` -> patch names from `patches/`
- `load vst` / `load fx` -> detected VST names
- `info` / `knobs` -> slot numbers, `master`, `fx`

### Plugin Commands

| Command | Description |
|---|---|
| `load vst <slot> <path\|vst_name> [name]` | Load VST instrument into slot |
| `load wav <slot> <pack> <sample> [name]` | Load `sampler/samples/<pack>/<sample>.wav` as one-shot sampler into slot |
| `load vcv <slot> <patch_name> [name]` | Load Cardinal into slot from `patches/<patch_name>.vcv` |
| `load fx <slot\|master> <path\|vst_name> [name]` | Load effect into slot insert chain or master bus |
| `unload <slot>` | Unload/clear instrument from slot |
| `unload fx <slot\|master> <fx_index>` | Remove effect by index |
| `params <slot>` | Show instrument parameters |
| `params master <fx_index>` | Show master FX parameters (defaults to first if omitted) |
| `set <slot> <name> <value>` | Set instrument parameter |
| `set master <fx_index> <name> <value>` | Set master FX parameter |
| `info <slot>` | Show plugin metadata (vendor, category, version, latency, param count) |
| `info <slot> fx <fx_index>` | Show metadata for a slot effect |
| `info master [fx_index]` | Show metadata for a master bus effect |
| `knobs <slot>` | Show ASCII parameter sliders with values, units, and ranges |
| `knobs <slot> fx <fx_index>` | Show parameter sliders for a slot effect |
| `knobs master [fx_index]` | Show parameter sliders for a master bus effect |

### Mixer Commands

| Command | Description |
|---|---|
| `gain <slot> <value>` | Set slot gain (0.0-1.0) |
| `gain master [value]` | Get or set master gain |
| `mute <slot>` | Toggle slot mute |
| `solo <slot>` | Toggle slot solo |
| `flow` | Show full signal-flow diagram (all slots, FX chains, master bus) |

### Audio Commands

All audio operations are subcommands of `audio`:

| Command | Description |
|---|---|
| `audio start [device]` | Start audio engine |
| `audio stop` | Stop audio engine |
| `audio devices` | List available output devices |

### MIDI Commands

All MIDI operations are subcommands of `midi`:

| Command | Description |
|---|---|
| `midi ports input` | List MIDI input ports |
| `midi ports output` | List MIDI output ports |
| `midi input <port>` | Open any MIDI input port |
| `midi input close <index>` | Close a MIDI input by its position in the open list |
| `midi link <ch> <slot>` | Route MIDI channel to slot |
| `midi cut <ch>` | Remove MIDI channel route |
| `midimix input <port>` | Open Akai MIDI Mix input |
| `midimix output <port>` | Open Akai MIDI Mix output (LED feedback) |
| `note <slot> <note> [vel] [dur_ms]` | Send test note to slot |

Index discovery:

```text
vcpi> midi ports input
  [0] Arturia BeatStep Pro MIDI 1
  [1] Novation 25 LE
  [2] MIDI Mix MIDI 1

vcpi> midi ports output
  [0] MIDI Mix MIDI 1
```

Use the numeric value in `[]` from `midi ports input` with `midi input`
and `midimix input`. Use indexes from `midi ports output` with `midimix output`.
Indexes may change after reboot or replug.

You can open multiple MIDI inputs simultaneously. All MIDI inputs share the
same channel routing table (`midi link <ch> <slot>`).

Important: `midi link <ch> <slot>` only maps MIDI channels internally. It does
not open hardware ports; `midi input` and `midimix input` do.

WAV sampler behavior:

- `load wav` plays the file when MIDI `note_on` events reach that slot.
- `load wav 2 909 bassdrum` resolves to `sampler/samples/909/bassdrum.wav`.
- Built-in packs: `808`, `909`, `piano`, `organ`, `strings`, `synth-pads`, `synth-leads`.
- The `.wav` extension is optional in `<sample>`.
- Notes are pitch-shifted around middle C (MIDI note 60).
- It is one-shot playback (note-off does not cut the sample).

Melodic examples:

```text
vcpi> load wav 1 piano c4-soft
vcpi> load wav 2 organ c4-drawbar
vcpi> load wav 3 strings c4-ensemble
vcpi> load wav 4 synth-pads c4-warm
vcpi> load wav 5 synth-leads c4-mono-saw
```

### Sequencer Commands

vcpi has a built-in step sequencer with up to 16 sequence banks. Notes in
a bank loop over one bar at the current tempo. Notes are evenly spaced:
1 note plays once per bar, 4 notes play as quarter notes, etc.

| Command | Description |
|---|---|
| `seq` | Show all sequence banks |
| `seq <bank>` | Show a single bank |
| `seq <bank> <note> [note ...]` | Set notes in a bank (e.g. `seq 1 d c b a`) |
| `seq clear <bank>` | Clear a bank |
| `seq link <bank> <slot>` | Attach sequence bank to a slot (starts playback) |
| `seq cut <slot>` | Remove all sequence links from a slot |

Note names are case-insensitive. Sharps (`C#`), flats (`Bb`), and octave
suffixes (`C5`, `F#3`) are supported. Default octave is 4 (middle C).

Examples:

```text
vcpi> seq 1 d c b a        # bank 1: D4 C4 B4 A4, plays as 4 quarter notes
vcpi> seq 2 c              # bank 2: just C4, plays once per bar
vcpi> seq 3 C#5 Bb4 G4     # bank 3: 3 notes per bar
vcpi> seq link 1 5          # play bank 1 through slot 5
vcpi> seq link 2 3          # play bank 2 through slot 3
vcpi> seq cut 5             # stop sequence on slot 5
vcpi> seq clear 1           # remove bank 1
```

The sequencer follows the current BPM (set via `tempo` or Ableton Link).

### Ableton Link Commands

All Ableton Link operations are subcommands of `ableton`:

| Command | Description |
|---|---|
| `ableton link [bpm]` | Enable Ableton Link (optionally set BPM) |
| `ableton cut` | Disable Ableton Link |
| `tempo [bpm]` | Get or set current BPM |

### Session Commands

| Command | Description |
|---|---|
| `save [path]` | Save current session to JSON |
| `restore [path]` | Restore session from JSON |

Saved/restored session state includes:

- Per-slot instruments, effects, parameters, gain, mute/solo
- Master effects and master gain
- MIDI channel routing
- BPM and Ableton Link state
- Sequencer banks and links
- Audio output device and MIDI connections

On startup restore, vcpi attempts to reconnect audio and MIDI targets
automatically.

### Visualization Commands

The `flow`, `info`, and `knobs` commands render ASCII diagrams using metadata
exposed by the pedalboard VST3 host library.

**`flow`** shows the full signal chain across all 8 slots:

```text
vcpi> flow
+------------------------------------------------+
|                vcpi Signal Flow                |
+------------------------------------------------+
|   [ S1] ch01,ch02    -> Dexed -> DragonflyHall |
|          gain [########--] 0.75                |
|   [S2] (empty)                                 |
|   [xS3] ch05         -> Surge -> TAL-Dub       |
|          gain [########--] 0.75 M              |
|   ...                                          |
|   Master FX: Limiter                           |
|   Master   : [########--] 0.85                 |
+------------------------------------------------+
```

- `x` prefix on the slot number means the slot is inaudible (muted, or
  another slot is soloed).
- `M` / `S` flags indicate mute and solo state.

**`info`** shows plugin metadata:

```text
vcpi> info 1
+-----------------------------------------------+
|                 Slot 1: Dexed                 |
|                                               |
| Name          : Dexed                         |
| Vendor        : Digital Suburban              |
| Category      : Instrument|Synth              |
| Version       : 0.9.6                         |
| Type          : Instrument                    |
| Path          : /home/pi/vcpi/vst3/Dexed.vst3 |
| Latency       : 0 samples                     |
|                                               |
| Parameters    : 24                            |
|   automatable : 24                            |
|   boolean     : 2                             |
|   discrete    : 5                             |
+-----------------------------------------------+
```

**`knobs`** shows every parameter as an ASCII slider bar:

```text
vcpi> knobs 1
+--------------------------------------------------------------+
|                        Slot 1: Dexed                         |
+--------------------------------------------------------------+
|   cutoff_hz  [#-------------------]  880.0 Hz  (20 .. 2e+04) |
|   resonance  [######--------------]  0.3       (0 .. 1)      |
|   attack_ms  [--------------------]  50.0 ms   (0 .. 5000)   |
+--------------------------------------------------------------+
```

- Float parameters show a slider bar, value with units, and the valid range.
- Boolean parameters show an on/off slider.
- Enum/string parameters show the current value and available options.

All three commands support slot instruments, per-slot effects, and master bus
effects using the same targeting syntax:

```text
info 1            # slot 1 instrument
info 1 fx 1       # slot 1, first effect
info master 1     # first master bus effect
knobs 1           # slot 1 instrument
knobs 1 fx 2      # slot 1, second effect
knobs master      # first master bus effect (index defaults to 1)
```

### Status and Exit

| Command | Description |
|---|---|
| `status` | Print combined system status |
| `deps` | Check optional dependency availability |
| `shutdown` | Shut down the vcpi daemon process |
| `quit` / `exit` / `Ctrl-D` | Disconnect this client session |

## Example Workflows

Headless server + client shell:

```bash
# Terminal 1
./vcsrv

# Terminal 2
./vcli
```

Server logs now include:

- CLI command execution (`[CLI] ...`)
- MIDI input events (`[MIDI IN] ...`)
- MIDI Mix events (`[MIDI Mix] ...`)
