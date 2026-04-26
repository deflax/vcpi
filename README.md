# vcpi

vcpi is a Python VST3 host with Ableton Link tempo sync, designed for
hardware-centric live setups (for example Arturia BeatStep Pro + Akai MIDI Mix).

## Quick Start (from repo root)

Fastest path:

```bash
# Terminal 1
./vcsrv

# Terminal 2
./vweb

# Browser
open http://127.0.0.1:8765
```

Manual setup:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Terminal 1: start server
python main.py serve

# Terminal 2: connect interactive client
python main.py cli

# Or start the local browser console and typed API
python main.py web
```

vcpi runs with a separate server (`serve`) and multiple clients. Use `cli`
for the terminal client or `web` for the local browser command console. Phase 2
adds typed JSON endpoints for status, slots, audio transport, and slot mixer
controls while keeping the command console available.

In server mode, vcpi does not start audio automatically. Start audio manually
from `vcli` with `audio start [device]`.

If you are on Linux, you may also need:

```bash
sudo apt install libasound2-dev libjack-dev libportaudio2
```

## Repository Layout

```text
.
  core/               # Main package
    host.py           # vcpi core orchestration
    engine.py         # Real-time audio engine (sounddevice callback)
    cli.py            # Interactive command-line interface
    server.py         # Unix socket server for headless mode
    client.py         # CLI client (connects to server)
    link.py           # Ableton Link wrapper
    sequencer.py      # Internal step sequencer
    session.py        # Session save/restore
  sampler/            # WAV sampler package
    plugin.py         # WavSamplerPlugin (plugin-like API)
    wav.py            # WAV file I/O and resampling
    samples/          # Built-in WAV sample packs (808, 909, piano, etc.)
  controllers/        # Hardware controller modules (generic MIDI input, MIDI Mix)
  graph/              # ASCII visualization renderers (signal flow, plugin info, knobs)
  vst3/               # Open-license VST3 plugins (run arch-specific fetch scripts)
  patches/            # VCV/Cardinal .vcv patch files
  sessions/           # User-saved session files (save/load commands)
  main.py             # Top-level Python entry point
  vcsrv               # Server launcher (creates .venv on first run)
  vcli                # Client launcher (creates .venv on first run)
  vweb                # Local browser console launcher (creates .venv on first run)
  requirements.txt    # Python dependencies
  USAGE.md            # Full CLI and startup flag reference
  rpi-build/          # Raspberry Pi image build/provisioning tools only
```

## Common Commands

```bash
# Start headless daemon with Unix socket API
./vcsrv

# Connect CLI client to a running daemon
./vcli

# Start local browser console and typed API
./vweb

# Direct python equivalents
python main.py serve
python main.py cli
python main.py web --host 127.0.0.1 --port 8765

# Raspberry Pi/systemd installs may need the service socket explicitly
./vweb --sock /run/vcpi/vcpi.sock

# Logging defaults:
# - python main.py serve -> WARNING
# - ./vcsrv -> DEBUG
LOG_LEVEL=INFO ./vcsrv
```

The web console binds to `127.0.0.1` by default, uses a per-process CSRF token
for state-changing requests, and refuses non-loopback hosts unless you pass
`--allow-remote`. Only expose it on a trusted network; both the command console
and typed API control the running daemon.

Full CLI and startup flag reference: `USAGE.md`

Inside `./vcli`, press `Tab` to autocomplete command names. The `slot`
command has context-aware completion: `slot <n> vst` and `slot <n> fx`
complete detected VST3 plugin names, `slot <n> wav` completes pack and
sample names, and `slot <n> vcv` completes patch names. You do not need
to type full paths.

Inside the CLI, a typical first run is:

```text
deps
midi ports input
audio devices
slot 1 vst Dexed
midi input <index>
midimix input <index>
midi ports output
midimix output <index>
midi link <ch> <slot>
flow
audio start
tempo 120
status
info 1
knobs 1
```

### Finding MIDI port indexes

Use these commands to discover indexes used by `midi input`,
`midimix input`, and `midimix output`:

```text
vcpi> midi ports input
  [0] Arturia BeatStep Pro MIDI 1
  [1] Novation 25 LE
  [2] MIDI Mix MIDI 1

vcpi> midi ports output
  [0] MIDI Mix MIDI 1
```

Use the number in brackets:

```text
vcpi> midi input 0
vcpi> midi input 1
vcpi> midimix input 2
vcpi> midimix output 0
```

You can open any number of MIDI inputs. Use `midi input close <index>` to
close one.

Port indexes can change after reboot/replug, so always re-check with
`midi ports input` and `midi ports output`.

## Controller Setup

### MIDI inputs (any keyboard, sequencer, etc.)

All MIDI input devices are handled identically. Open any device with
`midi input <port_index>` and route its MIDI channels to slots:

```text
vcpi> midi ports input
vcpi> midi input 0       # e.g. BeatStep Pro
vcpi> midi input 1       # e.g. keyboard
vcpi> midi link 1 1      # BSP ch 1 -> slot 1
vcpi> midi link 2 2      # BSP ch 2 -> slot 2
vcpi> midi link 10 3     # BSP ch 10 -> slot 3
vcpi> midi link 5 1      # keyboard ch 5 -> slot 1
```

- There are no device-specific commands; every MIDI input is a peer.
- Routing is channel-based: `midi link <ch> <slot>` maps a MIDI channel to a slot.
- Each device sends on its own channel(s); configure channels on the hardware.
- Use `midi input close <index>` to close an open input.

### Akai MIDI Mix (hardware mixer)

- MIDI Mix uses its own dedicated input port (separate from note routing).
- Connect control input with `midimix input <port_index>` (from `midi ports input`).
- Optional LED feedback is via MIDI output with `midimix output <port_index>` (from `midi ports output`).
- Factory mapping is used by default:
  - Channel faders 1-8 -> slot gain
  - Master fader -> master gain
  - 3 knobs per strip -> first 3 parameters of that slot's instrument plugin
  - MUTE buttons -> mute toggle
  - REC ARM buttons -> solo toggle

Strip-to-slot mapping is fixed: strip 1 controls slot 1, ... strip 8 controls slot 8.

MIDI Mix quick mapping (factory defaults):

```text
Faders (slots 1-8):   CC 19,23,27,31,49,53,57,61
Master fader:         CC 62
Knob 1 (per strip):   CC 16,20,24,28,46,50,54,58
Knob 2 (per strip):   CC 17,21,25,29,47,51,55,59
Knob 3 (per strip):   CC 18,22,26,30,48,52,56,60
MUTE buttons:         Notes 1,4,7,10,13,16,19,22
SOLO (REC ARM):       Notes 3,6,9,12,15,18,21,24
```

## Example Commands

Cardinal + VCV patch quick load:

```text
# place patch files under ./patches, e.g. ./patches/ambient.vcv
vcpi> slot 1 vcv ambient
vcpi> midi link 1 1
```

`slot <n> vcv` loads a fresh Cardinal instance into the slot you specify.
Routing remains explicit via `midi link <ch> <slot>`.

Fetch bundled open-license VST3 plugins and load one:

```bash
./vst3/fetch-vsts-amd64    # desktop/laptop Linux amd64
# or
./vst3/fetch-vsts-aarch64  # Raspberry Pi / Linux aarch64
```

Bundled plugins currently include Dexed, Surge XT, Odin 2, OB-Xf, Geonkick,
JC-303, Firefly Synth 2 (+FX), and Dragonfly Reverb.

```text
vcpi> slot 1 vst Dexed         # Tab completes installed VST names
vcpi> audio start
vcpi> note 1 60 100 500
```

Full paths also work:

```text
vcpi> slot 1 vst /path/to/Synth.vst3 Lead
```

Load a WAV sample into a slot and trigger it from MIDI routing:

```text
vcpi> slot 2 wav 909 bassdrum
vcpi> midi link 10 2
vcpi> midi input 0
```

`slot <n> wav` resolves to `sampler/samples/<pack>/<sample>.wav` (for example,
`sampler/samples/909/bassdrum.wav`) and plays one-shot sample voices on note-on.
You can include or omit the `.wav` extension in `<sample>`.

Built-in packs include drums (`808`, `909`) plus melodic/synth packs:

- `piano`
- `organ`
- `strings`
- `synth-pads`
- `synth-leads`

Examples:

```text
vcpi> slot 1 wav piano c4-soft
vcpi> slot 2 wav organ c4-drawbar
vcpi> slot 3 wav strings c4-ensemble
vcpi> slot 4 wav synth-pads c4-warm
vcpi> slot 5 wav synth-leads c4-mono-saw
```

Internal sequencer (bar-aligned with Ableton Link when enabled):

```text
vcpi> seq 1 d c b a          # define 4-note pattern in bank 1
vcpi> seq 2 c                # single note in bank 2
vcpi> seq link 1 5           # play bank 1 through slot 5
vcpi> seq link 2 3           # play bank 2 through slot 3
vcpi> seq cut 5              # stop sequence on slot 5
vcpi> seq                    # show all banks
```

Typical multi-instrument setup:

```text
vcpi> slot 1 vst Dexed Lead
vcpi> slot 2 vst OB-Xf Bass
vcpi> slot 3 vst Geonkick Drums

vcpi> midi link 1 1
vcpi> midi link 2 2
vcpi> midi link 10 3

vcpi> midi input 0
vcpi> midi input 1
vcpi> midimix input 2
vcpi> slot 1 fx DragonflyRoomReverb Reverb
vcpi> audio start
vcpi> ableton link 120
vcpi> status

vcpi> flow              # signal flow: all slots, FX chains, master bus
vcpi> info 1            # plugin metadata: vendor, category, version, etc.
vcpi> info 1 fx 1       # info for slot 1's first effect
vcpi> knobs 1           # ASCII slider view of all parameters
vcpi> knobs master 1    # knobs for first master effect
```

Headless server + remote CLI:

```bash
# Terminal 1
./vcsrv

# Terminal 2
./vcli
```

From `vcli`, use `shutdown` to terminate the daemon process (for example, to
let systemd restart it).

Browser console and typed API:

```bash
# Terminal 1
./vcsrv

# Terminal 2
./vweb

# Browser
open http://127.0.0.1:8765
```

`vweb` binds to `127.0.0.1:8765` by default and talks to the local vcpi
daemon over its Unix socket. Pass `--allow-shutdown` only if you want the web
console to expose daemon shutdown.

Phase 2 keeps the free-form command console at `/api/command` and adds typed
JSON endpoints for controls that a browser mixer can call without building CLI
strings. The typed endpoints are still local/protected. They use the same
loopback bind default, the same `--allow-remote` guard, and the same per-process
CSRF token for state-changing requests.

Expected typed endpoints:

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/status` | Return structured daemon status, including audio running state, sample rate, buffer size, tempo, Link state, and selected output name when known |
| `GET` | `/api/slots` | Return all 8 slots with name, source type, route channels, gain, mute, solo, and loaded effect names |
| `POST` | `/api/audio/start` | Start audio, optionally with JSON `{"device": "name or index"}` |
| `POST` | `/api/audio/stop` | Stop audio |
| `POST` | `/api/slots/<slot>/gain` | Set slot gain with JSON `{"gain": 0.75}` where `<slot>` is 1-8 |
| `POST` | `/api/slots/<slot>/mute` | Set or toggle slot mute with JSON `{"muted": true}` or `{"toggle": true}` |
| `POST` | `/api/slots/<slot>/solo` | Set or toggle slot solo with JSON `{"solo": true}` or `{"toggle": true}` |

Example:

```bash
TOKEN=$(python3 - <<'TOKENPY'
import re, urllib.request
html = urllib.request.urlopen('http://127.0.0.1:8765/').read().decode()
print(re.search(r'name="vcpi-csrf-token" content="([^"]+)"', html).group(1))
TOKENPY
)

curl http://127.0.0.1:8765/api/status
curl http://127.0.0.1:8765/api/slots
curl -X POST http://127.0.0.1:8765/api/slots/1/gain \
  -H "Content-Type: application/json" \
  -H "X-VCPI-CSRF: $TOKEN" \
  -d '{"gain": 0.65}'
```

## Raspberry Pi Builds

All Raspberry Pi image build files are in `rpi-build/`.

- Full instructions: `rpi-build/README.md`
- Build script: `rpi-build/prepare-image.sh`
- First-boot provisioning script: `rpi-build/setup.sh`
- Systemd units: `rpi-build/services/`

Run image build from repo root:

```bash
sudo ./rpi-build/prepare-image.sh <raspios-image-url>
```

Or from inside `rpi-build/`:

```bash
cd rpi-build
sudo ./prepare-image.sh <raspios-image-url>
```

## License

vcpi is licensed under the GNU General Public License v3.0 (GPL-3.0).
See the [LICENSE](LICENSE) file for the full text.

Several Python dependencies (`pedalboard`, `aalink`) and all bundled VST3
plugins are also GPL-3.0 licensed.
