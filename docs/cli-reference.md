# vcpi CLI Reference

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
| `--seq-midi` | unset | BeatStep Pro MIDI port index to open on startup |
| `--mix-midi` | unset | MIDI Mix port index to open on startup |
| `--output` | unset | Output audio device index or name |
| `--session` | `~/.config/vcpi/session.json` | Session file path |
| `--no-restore` | off | Skip session restore at startup |

When running `serve`, vcpi automatically attempts to start audio at boot
using `--output` if provided. If startup fails, the daemon keeps running and
logs the error.

Logging level is controlled by environment variable `LOG_LEVEL`.
Core default is `WARNING`. `./vcsrv` defaults it to `DEBUG` if unset.
The Raspberry Pi `payload.service` also sets `LOG_LEVEL=DEBUG`.

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

### Plugin Commands

| Command | Description |
|---|---|
| `load <slot> <path> [name]` | Load VST instrument into slot |
| `load_fx <path> [slot\|master] [name]` | Load effect into slot insert chain or master bus |
| `remove_fx <slot\|master> <fx_index>` | Remove effect by index |
| `slots` | Show slot status, routing, gain, and loaded FX |
| `params <slot>` | Show instrument parameters |
| `params master <fx_index>` | Show master FX parameters (defaults to first if omitted) |
| `set <slot> <name> <value>` | Set instrument parameter |
| `set master <fx_index> <name> <value>` | Set master FX parameter |

### Mixer Commands

| Command | Description |
|---|---|
| `gain <slot> <value>` | Set slot gain |
| `mute <slot>` | Toggle slot mute |
| `solo <slot>` | Toggle slot solo |
| `master [value]` | Get or set master gain |

### Routing Commands

| Command | Description |
|---|---|
| `route <ch> <slot>` | Route MIDI channel to slot |
| `unroute <ch>` | Remove MIDI channel route |
| `routing` | Show active channel routes |

### Audio Commands

| Command | Description |
|---|---|
| `audio_start [device]` | Start audio engine |
| `audio_stop` | Stop audio engine |
| `devices` | List available output devices |

### MIDI Commands

| Command | Description |
|---|---|
| `midi_ports` | List MIDI input ports |
| `midi_seq [port_index]` | Open BeatStep Pro input (no arg opens virtual input `vcpi-Seq`) |
| `midi_mix <port_index>` | Open Akai MIDI Mix input |
| `note <slot> <note> [vel] [dur_ms]` | Send test note to slot |

### Link Commands

| Command | Description |
|---|---|
| `link [bpm]` | Enable Link (optionally set BPM) |
| `unlink` | Disable Link |
| `tempo [bpm]` | Get or set current BPM |

### Session Commands

| Command | Description |
|---|---|
| `save [path]` | Save current session to JSON |
| `restore [path]` | Restore session from JSON |

### Status and Exit

| Command | Description |
|---|---|
| `status` | Print combined system status |
| `deps` | Check optional dependency availability |
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
- BeatStep Pro events (`[SEQ MIDI] ...`)
- MIDI Mix events (`[MIDI Mix] ...`)
