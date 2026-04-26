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
| `./vweb` | Starts the local-only browser command console and typed API; bootstraps `.venv` on first run |
| `python main.py serve` | Starts headless server with Unix socket control |
| `python main.py cli` | Connects to a running headless server |
| `python main.py web` | Starts the browser console and typed API on `127.0.0.1:8765` |

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

- `slot <n> vst` and `slot <n> fx` accept a plugin **name** (e.g. `Dexed`) or a
  full path (e.g. `/usr/lib/vst3/Dexed.vst3`). Names are resolved by scanning
  known search directories.
- Search order: `vst3/` in the repo root, `~/.vst3`,
  `/usr/lib/vst3`, `/usr/local/lib/vst3`, and macOS standard paths.
- Name matching: exact stem match first, then case-insensitive, then unique
  prefix match. Ambiguous names produce an error listing the candidates.
- Tab completion for `slot <n> vst` and `slot <n> fx` lists all detected plugin names.
- Run `./vst3/fetch-vsts-amd64` on x86_64/amd64 systems.
- Run `./vst3/fetch-vsts-aarch64` on Raspberry Pi / Linux aarch64 systems.
- Override with `VST3_PATH` or `VST_PATH` environment variables.

Cardinal/VCV helpers:

- `slot <n> vcv` looks for patch files in `patches/` by default.
- `slot <n> vcv` does not auto-route channels; use `midi link <ch> <slot>` explicitly.
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

## Web Client Flags

The browser console and typed API are local-only by default and connect to the
running daemon over its Unix socket.

| Flag | Default | Description |
|---|---|---|
| `--host` | `127.0.0.1` | HTTP bind host for the browser console |
| `--port` | `8765` | HTTP bind port for the browser console |
| `--sock` | auto (see above) | Unix socket to connect to |
| `--allow-shutdown` | off | Allow the browser UI to request daemon shutdown |
| `--daemon-timeout` | `60.0` | Seconds to wait for daemon command responses |
| `--allow-remote` | off | Allow binding to non-loopback hosts such as `0.0.0.0` |

Example:

```bash
./vcsrv
./vweb
# open http://127.0.0.1:8765

# If connecting to a Raspberry Pi/systemd service socket:
./vweb --sock /run/vcpi/vcpi.sock
```

Phase 2 keeps the command console for compatibility and adds typed JSON
endpoints for the browser mixer. State-changing requests require the CSRF token
embedded in the served page, and cross-origin posts are rejected. The server
refuses non-loopback binds unless `--allow-remote` is set. Only use remote
binding on a trusted network because both the command console and typed API can
control the running daemon.

The browser dashboard automatically refreshes `/api/status`, `/api/slots`,
`/api/sessions`, `/api/audio/devices`, and `/api/flow` on a conservative
client-side interval. It slows while the tab is hidden, skips polling during
typed control updates, and keeps the Refresh button available for an immediate
manual read. Loaded slot cards include an Unload action that calls the typed
slot clear endpoint and an Info action that opens a read-only details panel from
`/api/slots/<slot>/info`. The signal-flow panel displays the current ASCII mixer
diagram returned by the read-only flow endpoint. The session field can suggest
saved sessions from the picker, while manual safe-name entry remains supported.
The audio-device picker uses the read-only device list and sends the selected
value to `/api/audio/start` as `{"device": "name or index"}`. Tempo and Link
browser controls call typed POST routes for BPM, Link start, and Link stop
actions.

### Typed HTTP API

The typed API is a small local control surface for browser UI code. It avoids
stringly typed CLI commands for common mixer actions, but it does not relax the
safety model. The web process remains a client of the daemon socket. Daemon
work still runs on the daemon main thread, preserving native audio and plugin
requirements.

| Method | Path | Body | Description |
|---|---|---|---|
| `GET` | `/api/status` | none | Structured status: audio running state, sample rate, buffer size, tempo, Link state, selected output name when known |
| `GET` | `/api/slots` | none | All 8 slots with slot number, loaded name, source type, routed MIDI channels, gain, mute, solo, and effect count |
| `GET` | `/api/slots/<slot>/info` | none | Read-only slot diagnostics and plugin metadata for `<slot>` 1-8, returned as `{"ok": true, "slot": {...}, "instrument": {...}, "effects": [...], "rendered": "..."}`. Empty slots return `instrument: null`, an empty effects list, and a `message`. |
| `GET` | `/api/sessions` | none | Saved safe session names found directly under `sessions/`, sorted by name, with the loaded session marked |
| `GET` | `/api/audio/devices` | none | Output-capable audio devices for the browser picker, returned as `{"ok": true, "available": true, "current": "Built-in Output", "default_device": 1, "devices": [{"id": 1, "name": "Built-in Output", "output_channels": 2, "default": true, "selected": true}]}` |
| `GET` | `/api/flow` | none | Current ASCII signal-flow diagram for the browser diagnostics panel, returned as `{"ok": true, "flow": "..."}` |
| `POST` | `/api/session/save` | optional `{"name": "demo"}` | Save the current daemon state. Without a name, saves to the loaded session path. |
| `POST` | `/api/session/load` | `{"name": "demo"}` | Load a named session, refresh mixer state, update autosave, and return refreshed slots |
| `POST` | `/api/audio/start` | optional `{"device": "name or index"}` | Start the audio engine. The browser picker sends the selected device value here. |
| `POST` | `/api/audio/stop` | `{}` | Stop the audio engine |
| `POST` | `/api/tempo` | `{"bpm": 128}` | Set tempo, where BPM is 20.0 to 300.0 |
| `POST` | `/api/link/start` | optional `{"bpm": 128}` | Enable Ableton Link, optionally setting BPM first. BPM is 20.0 to 300.0. |
| `POST` | `/api/link/stop` | `{}` | Disable Ableton Link |
| `POST` | `/api/master/gain` | `{"gain": 0.75}` | Set master gain, where gain is 0.0-1.0 |
| `POST` | `/api/slots/<slot>/gain` | `{"gain": 0.75}` | Set slot gain, where `<slot>` is 1-8 and gain is 0.0-1.0 |
| `POST` | `/api/slots/<slot>/mute` | `{"muted": true}` or `{"toggle": true}` | Set or toggle slot mute. Omit the body or send `{"toggle": true}` to toggle. |
| `POST` | `/api/slots/<slot>/solo` | `{"solo": true}` or `{"toggle": true}` | Set or toggle slot solo. Omit the body or send `{"toggle": true}` to toggle. |
| `POST` | `/api/slots/<slot>/clear` | `{}` | Unload an already-loaded slot and return updated slot/status data. Existing MIDI routing behavior follows the same core clear semantics as `slot <n> clear`. |
| `POST` | `/api/slots/<slot>/unload` | `{}` | Alias for `/api/slots/<slot>/clear` |

Session names may be plain names such as `demo` or include the `.json` suffix.
They must start with a letter or number and use only letters, numbers, dots,
underscore, or hyphen. The web and daemon
resolve names to `sessions/<name>.json`. `GET /api/sessions` lists only safe,
top-level JSON session files in `sessions/` for the browser picker. Arbitrary
`path` payloads, absolute paths, nested paths, dotfiles, and spaces are still
not supported.

Tempo and Link BPM payloads must be JSON numbers from 20.0 to 300.0. Strings,
booleans, and values outside that range are rejected.

`GET /api/audio/devices`, `GET /api/flow`, and `GET /api/slots/<slot>/info` are
read-only and do not need a CSRF token. The audio-device endpoint lists only
devices with output channels. If `sounddevice` is unavailable or device querying
fails in the daemon, the endpoint returns `{"ok": true, "available": false,
"devices": []}` so the browser can keep the system-default option available.
`GET /api/flow` returns the same current ASCII signal-flow diagram shown by the
CLI `flow` command. `GET /api/slots/<slot>/info` is diagnostics only for the
browser Info panel and does not edit parameters, load plugins, or change slot
state. Starting audio still uses a state-changing POST and must include the CSRF
token, even when the device value came from the picker.

Read-only requests can be called directly:

```bash
curl http://127.0.0.1:8765/api/status
curl http://127.0.0.1:8765/api/slots
curl http://127.0.0.1:8765/api/sessions
curl http://127.0.0.1:8765/api/audio/devices
curl http://127.0.0.1:8765/api/flow
curl http://127.0.0.1:8765/api/slots/1/info
```

For `POST` requests, read the CSRF token from `/` and send it as
`X-VCPI-CSRF`:

```bash
TOKEN=$(python3 - <<'TOKENPY'
import re, urllib.request
html = urllib.request.urlopen('http://127.0.0.1:8765/').read().decode()
print(re.search(r'name="vcpi-csrf-token" content="([^"]+)"', html).group(1))
TOKENPY
)

curl -X POST http://127.0.0.1:8765/api/audio/start \
  -H "Content-Type: application/json" \
  -H "X-VCPI-CSRF: $TOKEN" \
  -d '{"device": "Built-in Output"}'

curl -X POST http://127.0.0.1:8765/api/slots/1/mute \
  -H "Content-Type: application/json" \
  -H "X-VCPI-CSRF: $TOKEN" \
  -d '{"toggle": true}'

curl -X POST http://127.0.0.1:8765/api/tempo \
  -H "Content-Type: application/json" \
  -H "X-VCPI-CSRF: $TOKEN" \
  -d '{"bpm": 128}'

curl -X POST http://127.0.0.1:8765/api/link/start \
  -H "Content-Type: application/json" \
  -H "X-VCPI-CSRF: $TOKEN" \
  -d '{"bpm": 128}'

curl -X POST http://127.0.0.1:8765/api/link/stop \
  -H "Content-Type: application/json" \
  -H "X-VCPI-CSRF: $TOKEN" \
  -d '{}'

curl -X POST http://127.0.0.1:8765/api/session/save \
  -H "Content-Type: application/json" \
  -H "X-VCPI-CSRF: $TOKEN" \
  -d '{"name": "demo"}'

curl -X POST http://127.0.0.1:8765/api/session/load \
  -H "Content-Type: application/json" \
  -H "X-VCPI-CSRF: $TOKEN" \
  -d '{"name": "demo"}'
```

The free-form command console remains available at `/api/command` for commands
that do not yet have typed endpoints.

## Interactive Commands

When connected with `./vcli` (or `python main.py cli`), press `Tab` to
autocomplete command names. `slot` has context-aware argument completion:

- `slot` -> slot numbers `1`-`8`, `master`
- `slot <n>` -> `vst`, `wav`, `vcv`, `fx`, `clear`
- `slot <n> wav` -> sample pack names and sample names
- `slot <n> vcv` -> patch names from `patches/`
- `slot <n> vst` / `slot <n> fx` -> detected VST names
- `info` / `knobs` -> slot numbers, `master`, `fx`

### Plugin Commands

| Command | Description |
|---|---|
| `slot <slot> vst <path\|vst_name> [name]` | Load VST instrument into slot |
| `slot <slot> wav <pack> <sample> [name]` | Load `sampler/samples/<pack>/<sample>.wav` as one-shot sampler into slot |
| `slot <slot> vcv <patch_name> [name]` | Load Cardinal into slot from `patches/<patch_name>.vcv` |
| `slot <slot\|master> fx <path\|vst_name> [name]` | Load effect into slot insert chain or master bus |
| `slot <slot> clear` | Clear instrument from slot |
| `slot <slot\|master> fx clear <fx_index>` | Remove effect by index |
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

- `slot <n> wav` plays the file when MIDI `note_on` events reach that slot.
- `slot 2 wav 909 bassdrum` resolves to `sampler/samples/909/bassdrum.wav`.
- Built-in packs: `808`, `909`, `piano`, `organ`, `strings`, `synth-pads`, `synth-leads`.
- The `.wav` extension is optional in `<sample>`.
- Notes are pitch-shifted around middle C (MIDI note 60).
- It is one-shot playback (note-off does not cut the sample).

Melodic examples:

```text
vcpi> slot 1 wav piano c4-soft
vcpi> slot 2 wav organ c4-drawbar
vcpi> slot 3 wav strings c4-ensemble
vcpi> slot 4 wav synth-pads c4-warm
vcpi> slot 5 wav synth-leads c4-mono-saw
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
When Ableton Link is enabled, the sequencer phase-locks to the shared
Link beat grid -- bar boundaries align with Ableton Live and any other
Link peers on the network. Without Link the sequencer still follows
tempo but free-runs from the moment playback starts.

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
| `save <name>` | Save current session to `sessions/<name>.json` |
| `load <name>` | Load session from `sessions/<name>.json` |

Sessions are saved to the `sessions/` directory in the repo root. Tab
completion lists available session names.

`save` always requires a name. The automatic session at
`~/.config/vcpi/session.json` is only written on shutdown (not by the
`save` command). `load` restores the named session and also updates the
auto-save session so that shutdown preserves the loaded state.

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
| `about` | Print the vcpi logo and basic project info |
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
