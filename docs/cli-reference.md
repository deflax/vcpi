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
| `--keys-midi` | unset | Novation 25 LE MIDI port index to open on startup |
| `--mix-midi` | unset | MIDI Mix port index to open on startup |
| `--mix-midi-out` | unset | MIDI Mix output port index (LED feedback) |
| `--output` | unset | Preferred output audio device index or name (not auto-started) |
| `--session` | `~/.config/vcpi/session.json` | Session file path |
| `--no-restore` | off | Skip session restore at startup |

When running `serve`, vcpi does not start audio automatically. Start audio
manually from the client with `audio_start [device]`.

Only one server instance per user is allowed. If a server is already running
(e.g. via systemd `payload.service`), `./vcsrv` will refuse to start and
print the existing PID. Connect to the running instance with `./vcli` instead.
The PID file is stored at `~/.config/vcpi/vcpi.pid`.

Logging level is controlled by environment variable `LOG_LEVEL`.
Core default is `WARNING`. `./vcsrv` defaults it to `DEBUG` if unset.
The Raspberry Pi `payload.service` also sets `LOG_LEVEL=DEBUG`.

Cardinal/VCV helpers:

- `load vcv` looks for patch files in `patches/` by default.
- `load vcv` does not auto-route channels; use `route <ch> <slot>` explicitly.
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
autocomplete command names.

### Plugin Commands

| Command | Description |
|---|---|
| `load <slot> <path> [name]` | Load VST instrument into slot |
| `load vcv <slot> <patch_name> [name]` | Load Cardinal into explicit slot from `patches/<patch_name>.vcv` |
| `load fx <path> [slot\|master] [name]` | Load effect into slot insert chain or master bus |
| `unload <slot>` | Unload/clear instrument from slot |
| `unload fx <slot\|master> <fx_index>` | Remove effect by index |
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
| `graph` | Show ASCII route graph |

### Audio Commands

| Command | Description |
|---|---|
| `audio_start [device]` | Start audio engine |
| `audio_stop` | Stop audio engine |
| `audio_devices` | List available output devices |

### MIDI Commands

| Command | Description |
|---|---|
| `midi_ports_in` | List MIDI input ports |
| `midi_ports_out` | List MIDI output ports |
| `midi_seq [port_index]` | Open BeatStep Pro input (no arg opens virtual input `vcpi-Seq`) |
| `midi_keys <port_index>` | Open Novation 25 LE keyboard input |
| `midi_mix <port_index>` | Open Akai MIDI Mix input |
| `midi_mix_out <port_index>` | Open Akai MIDI Mix output (LED feedback) |
| `note <slot> <note> [vel] [dur_ms]` | Send test note to slot |

Index discovery:

```text
vcpi> midi_ports_in
  [0] Arturia BeatStep Pro MIDI 1
  [1] Novation 25 LE
  [2] MIDI Mix MIDI 1

vcpi> midi_ports_out
  [0] MIDI Mix MIDI 1
```

Use the numeric value in `[]` from `midi_ports_in` with `midi_seq`,
`midi_keys`, and `midi_mix`. Use indexes from `midi_ports_out` with
`midi_mix_out`. Indexes may change after reboot or replug.

Important: `route <ch> <slot>` only maps MIDI channels internally. It does
not open hardware ports; `midi_seq`, `midi_keys`, and `midi_mix` do.

`midi_seq` (BeatStep Pro) and `midi_keys` (Novation 25 LE) both use the same
channel routing table from `route <ch> <slot>`.

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

Saved/restored session state now also includes selected connection targets for:

- audio output device
- BeatStep Pro input (`midi_seq`)
- Novation input (`midi_keys`)
- MIDI Mix input/output (`midi_mix`, `midi_mix_out`)

On startup restore, vcpi attempts to reconnect these targets automatically.

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
- BeatStep Pro events (`[SEQ MIDI] ...`)
- MIDI Mix events (`[MIDI Mix] ...`)
