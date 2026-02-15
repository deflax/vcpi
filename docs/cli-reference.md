# LinkVST CLI Reference

This reference covers command-line startup flags and interactive `linkvst>`
commands.

Source of truth:

- `linkvst/main.py`
- `linkvst/cli.py`

## Conventions

- Slots are **1-8**.
- MIDI channels are **1-16**.
- `fx_index` values are **1-based** in CLI commands.
- `master` means the global master effects bus.

## Startup Modes

| Command | What it does |
|---|---|
| `./start.sh` | Starts local interactive mode; bootstraps `.venv` on first run |
| `python vst_host.py` | Starts local interactive mode |
| `python vst_host.py serve` | Starts headless server with Unix socket control |
| `python -m linkvst cli` | Connects to a running headless server |

Default socket path (server and client):

```text
/run/linkvst/linkvst.sock
```

## Host Startup Flags

These flags apply to local mode (`python vst_host.py`) and server mode
(`python vst_host.py serve`).

| Flag | Default | Description |
|---|---|---|
| `--sr` | `44100` | Audio sample rate |
| `--buf` | `512` | Audio buffer size |
| `--bpm` | `120.0` | Initial tempo |
| `--link` | off | Enable Ableton Link on startup |
| `--seq-midi` | unset | BeatStep Pro MIDI port index to open on startup |
| `--mix-midi` | unset | MIDI Mix port index to open on startup |
| `--output` | unset | Output audio device index or name |
| `--session` | `~/.config/linkvst/session.json` | Session file path |
| `--no-restore` | off | Skip session restore at startup |

Server/client specific:

| Mode | Flag | Default | Description |
|---|---|---|---|
| `serve` | `--sock` | `/run/linkvst/linkvst.sock` | Unix socket to bind |
| `cli` | `--sock` | `/run/linkvst/linkvst.sock` | Unix socket to connect to |

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
| `midi_seq [port_index]` | Open BeatStep Pro input (no arg opens virtual input `LinkVST-Seq`) |
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
| `quit` / `exit` / `Ctrl-D` | Exit local CLI, or disconnect client in server mode |

## Example Workflows

Local interactive mode:

```text
./start.sh
linkvst> deps
linkvst> midi_ports
linkvst> midi_seq 0
linkvst> midi_mix 1
linkvst> load 1 /path/to/Synth.vst3 Lead
linkvst> route 1 1
linkvst> audio_start
linkvst> link 120
linkvst> status
```

Headless server + client shell:

```bash
./start.sh serve --sock /run/linkvst/linkvst.sock
python -m linkvst cli --sock /run/linkvst/linkvst.sock
```
