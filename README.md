# vcpi

A Raspberry Pi image that auto-provisions and runs
[LinkVST](#linkvst) -- a Python VST3 host with Ableton Link tempo
synchronisation, designed for use with an **Arturia Beatstep Pro** and an
**Akai MIDI Mix**.

---

## Table of Contents

- [Raspberry Pi Image Setup](#raspberry-pi-image-setup)
  - [Build the Image](#build-the-image)
  - [First Boot](#first-boot)
  - [Debugging](#debugging)
- [LinkVST](#linkvst)
  - [Overview](#overview)
  - [Requirements](#requirements)
  - [Quick Start](#quick-start)
  - [Hardware Setup](#hardware-setup)
  - [CLI Command Reference](#cli-command-reference)
  - [Session Persistence](#session-persistence)
  - [Akai MIDI Mix Mapping](#akai-midi-mix-mapping)
  - [Architecture](#architecture)
  - [Startup Options](#startup-options)
  - [Examples](#examples)
  - [Troubleshooting](#troubleshooting)
  - [License](#license)

---

## Raspberry Pi Image Setup

### Build the Image

1. Create a local `wpa_supplicant.conf` file (it is gitignored) and add your WiFi network name and password:

```conf
country=BG
update_config=1
ctrl_interface=/var/run/wpa_supplicant

network={
 scan_ssid=1
 ssid="YOUR_SSID"
 psk="YOUR_PASSWORD"
}
```

`prepare-image.sh` reads this file and writes a NetworkManager profile into the image for Raspberry Pi OS Bookworm+.

2. Create a local `userconf.txt` file (it is gitignored) with your own password hash:

   ```bash
   cp userconf.example.txt userconf.txt
   HASH=$(openssl passwd -6 'your-strong-password')
   printf 'pi:%s\n' "$HASH" > userconf.txt
   ```

   `prepare-image.sh` will refuse known insecure defaults unless you explicitly set
   `ALLOW_INSECURE_DEFAULTS=1`.

3. Execute `prepare-image.sh` with `sudo` (it needs root for `losetup`, `mount`, and `umount`) where `[image]` is the URL to Raspberry Pi OS.

   ```bash
   sudo ./prepare-image.sh [image]
   ```

   The downloaded archive is cached in `.image-cache/` so repeated runs do not download again.
   Use `--refresh` (or `-r`) to force re-download and overwrite the cached archive:

   ```bash
   sudo ./prepare-image.sh --refresh [image]
   ```

   Optionally set `IMAGE_CACHE_DIR` to use a different cache directory:

   ```bash
   sudo IMAGE_CACHE_DIR=/var/cache/vcpi ./prepare-image.sh [image]
   ```

   Optionally set `USERCONF_PATH` if your credentials file is stored elsewhere:

   ```bash
   sudo USERCONF_PATH=/secure/path/userconf.txt ./prepare-image.sh [image]
   ```

4. Flash the `vcpi.img` to SD card.

### First Boot

5. Boot Rpi on DHCP enabled network. The boot script should run `setup.sh` on first boot and reboot the system when its done.

6. Login with the credentials you configured in `userconf.txt` (default username is `pi`).

7. After reboot, the `vcpi` payload service starts automatically and runs LinkVST.

### Debugging

- Debug the initial setup process with `journalctl -u firstboot`. The `setup.sh` should be automatically renamed to `setup.sh.done` if setup is successful.
- Debug the payload service with `journalctl -u payload`.

---

# LinkVST

A Python VST3 host with Ableton Link tempo synchronisation, designed for use
with an **Arturia Beatstep Pro** (external step sequencer) and an **Akai MIDI
Mix** (hardware mixer).

---

## Overview

LinkVST provides:

- **8 instrument slots** that each hold a VST3 instrument plugin with an
  optional per-slot insert-effects chain
- **A master effects bus** applied to the summed output of all slots
- **Flexible MIDI routing** -- any MIDI channel (1-16) from the Beatstep Pro
  can be routed to any instrument slot
- **Hardware mixer control** via the Akai MIDI Mix (faders, knobs, mute, solo)
- **Ableton Link** tempo synchronisation so the Beatstep Pro and any other
  Link-enabled applications share a common clock
- **Real-time audio** rendered through a low-latency sounddevice callback

```
                        ┌─────────────────────────────────────────┐
Beatstep Pro ──USB──>   │  [channel routing]                      │
  (notes/CC)            │       │                                 │
                        │       v                                 │
                        │  Instrument Slots 1-8  ── per-slot FX   │
                        │       │                                 │
MIDI Mix ──────USB──>   │  gain / mute / solo                     │
  (separate port)       │  knobs -> plugin params                 │
                        │       │                                 │
                        │       v                                 │
                        │  Master Effects Bus                     │
                        │       │                                 │
                        │       v                                 │
                        │  Audio Output                           │
                        └─────────────────────────────────────────┘
                                │
Ableton Link <── tempo sync ────┘
```

The Beatstep Pro and MIDI Mix use **separate MIDI ports**. The BSP sends
notes through the channel router to instruments. The MIDI Mix is a dedicated
mixer controller that directly adjusts slot gain, mute/solo, and plugin
parameters -- it never sends notes to instruments.

---

## Requirements

| Dependency | Purpose | Install |
|---|---|---|
| Python 3.10+ | Runtime | -- |
| [pedalboard](https://github.com/spotify/pedalboard) | VST3 plugin hosting | `pip install pedalboard` |
| [aalink](https://pypi.org/project/aalink/) | Ableton Link sync | `pip install aalink` |
| [python-rtmidi](https://pypi.org/project/python-rtmidi/) | Hardware MIDI I/O | `pip install python-rtmidi` |
| [mido](https://pypi.org/project/mido/) | MIDI message construction | `pip install mido` |
| [numpy](https://numpy.org/) | Audio buffer math | `pip install numpy` |
| [sounddevice](https://python-sounddevice.readthedocs.io/) | Real-time audio output | `pip install sounddevice` |

All dependencies are optional at import time (the host will tell you what is
missing), but all are required for full functionality.

> **Note:** On the Raspberry Pi image, all dependencies are pre-installed in a
> virtualenv at `/home/pi/vcpi/venv` during first boot.

---

## Quick Start

```bash
# 1. Start the host
python vst_host.py
# or
python -m linkvst

# 2. Check dependencies
linkvst> deps

# 3. List your hardware
linkvst> midi_ports
linkvst> devices

# 4. Connect MIDI
linkvst> midi_seq 0          # Beatstep Pro (use the port index from midi_ports)
linkvst> midi_mix 1          # MIDI Mix

# 5. Load instruments into slots
linkvst> load 1 /path/to/Synth.vst3 Lead
linkvst> load 2 /path/to/Bass.vst3 Bass
linkvst> load 3 /path/to/Drums.vst3 Drums

# 6. Route Beatstep Pro MIDI channels to slots
#    (use whatever channels your BSP is configured to send on)
linkvst> route 1 1           # BSP Seq1 channel -> slot 1 (Lead)
linkvst> route 2 2           # BSP Seq2 channel -> slot 2 (Bass)
linkvst> route 10 3          # BSP Drum channel -> slot 3 (Drums)

# 7. Optionally load effects
linkvst> load_fx /path/to/Delay.vst3 1          # insert on slot 1
linkvst> load_fx /path/to/Reverb.vst3 master     # master bus

# 8. Start audio and Link
linkvst> audio_start
linkvst> link 120

# 9. Play!
linkvst> status
```

---

## Hardware Setup

### Arturia Beatstep Pro

The Beatstep Pro acts as the external step sequencer. LinkVST receives its
MIDI notes and CCs and routes them to instrument slots based on MIDI channel.

**The BSP's MIDI channels are fully configurable** in Arturia's MIDI Control
Center. Whatever channels you assign to Seq 1, Seq 2, and the Drum sequencer,
you simply match them with `route` commands in LinkVST. There are no
hard-coded channel assumptions.

**Example** (assuming BSP is configured to Seq1=ch3, Seq2=ch5, Drum=ch10):

```
linkvst> route 3 1       # BSP Seq1 on ch 3 -> slot 1
linkvst> route 5 2       # BSP Seq2 on ch 5 -> slot 2
linkvst> route 10 3      # BSP Drum on ch 10 -> slot 3
```

> **Numbering:** Slots are 1-8 and MIDI channels are 1-16 -- matching the
> numbers printed on your hardware. No 0-indexing.

You can route any channel to any slot, and multiple channels to the same slot
if desired. Use `routing` to see active routes at any time.

The Beatstep Pro connects via USB. Its MIDI port will appear in
`midi_ports` output -- look for a name containing "Arturia BeatStep Pro".

### Akai MIDI Mix

The MIDI Mix acts as a **dedicated hardware mixer** for the 8 instrument
slots. It is **not** part of the MIDI channel routing -- it has its own
separate MIDI port and handler. It only controls mixing (gain, mute, solo)
and plugin parameters. It never sends notes to instruments.

Plug it in via USB and open it with `midi_mix <port_index>`.

It uses **factory default** CC/note assignments (no special configuration
needed). See the [mapping table](#akai-midi-mix-mapping) below for details.

**What each control does:**

| Control | Function |
|---|---|
| Channel faders (1-8) | Slot gain (0.0 - 1.0) |
| Master fader | Master bus gain |
| Knobs (3 per channel) | First 3 parameters of the slot's instrument plugin |
| MUTE buttons | Toggle mute per slot |
| REC ARM buttons | Toggle solo per slot (repurposed) |

**Strip-to-slot mapping is fixed:** MIDI Mix strip 1 = slot 1, strip 2 =
slot 2, ..., strip 8 = slot 8. This is a direct 1:1 hardware mapping -- there
is no configurable routing for the mixer.

**Solo behaviour:** when any slot is soloed, only soloed slots are audible.
When no slots are soloed, all unmuted slots are audible. MUTE and SOLO are
independent -- a muted slot stays silent even if also soloed.

### Ableton Link

[Ableton Link](https://www.ableton.com/en/link/) synchronises tempo across
applications on the same network. Enable it with:

```
linkvst> link 120
```

Any Link-enabled app (Ableton Live, other instances of LinkVST, iOS apps,
etc.) on the same local network will lock to the same tempo. The Beatstep Pro
receives its clock from Link through the computer's USB connection.

You can change tempo at any time:

```
linkvst> tempo 135
```

All connected Link peers will follow the change.

---

## CLI Command Reference

### Plugin Commands

| Command | Description |
|---|---|
| `load <slot> <path> [name]` | Load a VST3 instrument into slot 1-8 |
| `load_fx <path> [slot\|master] [name]` | Load a VST3 effect (insert on slot, or master bus) |
| `remove_fx <slot\|master> <fx_index>` | Remove an effect by index |
| `slots` | Show all 8 slots with status |
| `params <slot>` | List instrument parameters and current values |
| `params master <fx_index>` | List effect parameters on master bus |
| `set <slot> <name> <value>` | Set an instrument parameter |
| `set master <fx_index> <name> <value>` | Set a master effect parameter |

### Mixer Commands

| Command | Description |
|---|---|
| `gain <slot> <0.0-1.0>` | Set slot volume |
| `mute <slot>` | Toggle mute on a slot |
| `solo <slot>` | Toggle solo on a slot |
| `master [0.0-1.0]` | Get or set master bus gain |

### Routing Commands

| Command | Description |
|---|---|
| `route <ch> <slot>` | Route MIDI channel (1-16) to instrument slot (1-8) |
| `unroute <ch>` | Remove a MIDI channel route |
| `routing` | Show all active MIDI channel routes |

### Audio Commands

| Command | Description |
|---|---|
| `audio_start [device]` | Start audio output (device index or name) |
| `audio_stop` | Stop audio output |
| `devices` | List available audio devices |

### MIDI Commands

| Command | Description |
|---|---|
| `midi_ports` | List available MIDI input ports |
| `midi_seq <port>` | Open Beatstep Pro MIDI port |
| `midi_mix <port>` | Open MIDI Mix port |
| `note <slot> <note> [vel] [dur_ms]` | Send a test note to a slot |

### Link Commands

| Command | Description |
|---|---|
| `link [bpm]` | Enable Ableton Link (optionally set initial BPM) |
| `unlink` | Disable Ableton Link |
| `tempo [bpm]` | Get or set tempo |

### Session Commands

| Command | Description |
|---|---|
| `save [path]` | Save session to file (default: `~/.config/linkvst/session.json`) |
| `restore [path]` | Restore session from file |

### Status Commands

| Command | Description |
|---|---|
| `status` | Full overview (audio, MIDI, Link, all slots) |
| `deps` | Check which dependencies are installed |
| `quit` / `exit` | Save session and exit |

---

## Session Persistence

LinkVST automatically saves your session on exit and restores it on the next
startup. The session file is stored at `~/.config/linkvst/session.json` by
default.

### What is saved

- All loaded instruments (plugin paths, names)
- All plugin parameter values (instrument and effect)
- Per-slot insert effects (paths, names, parameters)
- Master bus effects (paths, names, parameters)
- Per-slot gain, mute, and solo state
- Master gain
- MIDI channel routing
- Link BPM

### What is NOT saved

- Audio device selection (depends on hardware state)
- MIDI port connections (Beatstep Pro, MIDI Mix -- ports may change between runs)
- Whether Link was enabled (use `--link` flag or `link` command)

### Session file

The session file is human-readable JSON. You can hand-edit it, copy it to
create presets, or share it with others who have the same plugins installed.

```bash
# Use a custom session file
python vst_host.py --session ~/my-liveset.json

# Skip restoring (start fresh)
python vst_host.py --no-restore

# Manually save/restore during a session
linkvst> save
linkvst> save ~/backup.json
linkvst> restore ~/other-session.json
```

---

## Akai MIDI Mix Mapping

The host uses the **factory default** MIDI assignments. No configuration in
the Akai MIDI Mix Editor is required.

### Faders (CC, channel gain)

| Strip | CC | Slot |
|---|---|---|
| 1 | 19 | 1 |
| 2 | 23 | 2 |
| 3 | 27 | 3 |
| 4 | 31 | 4 |
| 5 | 49 | 5 |
| 6 | 53 | 6 |
| 7 | 57 | 7 |
| 8 | 61 | 8 |
| Master | 62 | master bus |

### Knobs (CC, first 3 instrument parameters)

Each strip has 3 knobs mapped to the first 3 parameters of the instrument
loaded in that slot. The CC value (0-127) is scaled to the parameter's
native range automatically.

| Strip | Knob 1 (High) | Knob 2 (Mid) | Knob 3 (Low) |
|---|---|---|---|
| 1 | CC 16 | CC 17 | CC 18 |
| 2 | CC 20 | CC 21 | CC 22 |
| 3 | CC 24 | CC 25 | CC 26 |
| 4 | CC 28 | CC 29 | CC 30 |
| 5 | CC 46 | CC 47 | CC 48 |
| 6 | CC 50 | CC 51 | CC 52 |
| 7 | CC 54 | CC 55 | CC 56 |
| 8 | CC 58 | CC 59 | CC 60 |

### Buttons (Note On, toggle)

| Strip | MUTE (note) | SOLO / REC ARM (note) |
|---|---|---|
| 1 | 1 | 3 |
| 2 | 4 | 6 |
| 3 | 7 | 9 |
| 4 | 10 | 12 |
| 5 | 13 | 15 |
| 6 | 16 | 18 |
| 7 | 19 | 21 |
| 8 | 22 | 24 |

> The REC ARM buttons are repurposed as SOLO toggles.

---

## Architecture

```
src/
  linkvst/
    __init__.py      Package exports (VSTHost, InstrumentSlot, NUM_SLOTS)
    __main__.py      python -m linkvst support
    main.py          Argument parsing and startup sequence
    deps.py          All optional imports in one place with availability flags
    models.py        InstrumentSlot dataclass and NUM_SLOTS constant
    midi.py          MidiPort class (generic python-rtmidi wrapper)
    link.py          LinkSync class (aalink wrapper)
    midimix.py       MIDI Mix CC/note constants and MidiMixHandler
    engine.py        AudioEngine (sounddevice callback, rendering, mixing)
    host.py          VSTHost (coordinator: plugins, routing, MIDI, Link)
    session.py       Save/restore session state to JSON
    cli.py           HostCLI (interactive cmd.Cmd interface)
  vst_host.py        Thin entry point (calls linkvst.main)
  requirements.txt   pip dependencies
```

### Module dependency graph

```
deps.py            (no internal deps -- imported by everything)
   |
models.py          (no internal deps)
   |
midi.py            <- deps
link.py            <- deps
   |
midimix.py         <- models, (reads engine.slots at runtime)
engine.py          <- deps, models
   |
host.py            <- deps, models, engine, midi, midimix, link
   |
cli.py             <- deps, midi, host
   |
main.py            <- host, cli
```

### Audio signal flow

1. **MIDI in** -- Beatstep Pro sends notes/CC over USB MIDI
2. **Channel routing** -- `_channel_map` directs each MIDI channel to a slot
3. **MIDI queue** -- messages are enqueued thread-safely for the audio callback
4. **Audio callback** (called by sounddevice every `buffer_size` samples):
   - Flush queued MIDI into each slot's instrument plugin
   - Render each instrument (silence in -> audio out)
   - Apply per-slot insert effects
   - Mix into a stereo bus (respecting gain, mute, solo)
   - Apply master effects chain
   - Clip to [-1.0, 1.0] and write to output buffer
5. **Link** -- tempo is shared across the network; the Beatstep Pro syncs via
   the computer's USB connection

---

## Startup Options

```
python vst_host.py [OPTIONS]
python -m linkvst [OPTIONS]
```

| Flag | Default | Description |
|---|---|---|
| `--sr` | 44100 | Sample rate in Hz |
| `--buf` | 512 | Audio buffer size in samples |
| `--bpm` | 120.0 | Initial tempo |
| `--link` | off | Enable Ableton Link on startup |
| `--seq-midi` | -- | Beatstep Pro MIDI port index (auto-open) |
| `--mix-midi` | -- | MIDI Mix port index (auto-open) |
| `--output` | default | Audio output device (index or name) |
| `--session` | `~/.config/linkvst/session.json` | Session file path |
| `--no-restore` | off | Skip restoring previous session on startup |

**Example: start everything in one command:**

```bash
python vst_host.py --link --bpm 128 --seq-midi 0 --mix-midi 1 --buf 256
```

---

## Examples

### Load a synth and play a test note

```
linkvst> load 1 /Library/Audio/Plug-Ins/VST3/Diva.vst3 Diva
  slot 1 = Diva
linkvst> audio_start
[Audio] Started  sr=44100  buf=512  ch=2
linkvst> note 1 60 100 500
```

### Multi-instrument setup with Beatstep Pro

```
# Load instruments
linkvst> load 1 /path/to/Diva.vst3 Lead
linkvst> load 2 /path/to/Monologue.vst3 Bass
linkvst> load 3 /path/to/DrumBrute.vst3 Drums

# Route BSP channels to slots (match your MIDI Control Center config)
# e.g. Seq1=ch1, Seq2=ch2, Drum=ch10
linkvst> route 1 1           # ch 1 -> Lead
linkvst> route 2 2           # ch 2 -> Bass
linkvst> route 10 3          # ch 10 -> Drums

# Effects
linkvst> load_fx /path/to/Valhalla.vst3 master Reverb
linkvst> load_fx /path/to/Delay.vst3 1 PingPong

# Connect hardware and go
linkvst> midi_seq 0
linkvst> midi_mix 1
linkvst> audio_start
linkvst> link 120
linkvst> status
```

### Tweak a plugin parameter

```
linkvst> params 1
  cutoff = 0.5  (range 0.000 .. 1.000)
  resonance = 0.2  (range 0.000 .. 1.000)
  ...

linkvst> set 1 cutoff 0.75
  cutoff = 0.75
```

### Solo a single instrument for sound design

```
linkvst> solo 1
  Lead: SOLO
linkvst> note 1 48 80 1000
linkvst> solo 1
  Lead: unsolo
```

---

## Troubleshooting

### `deps` shows MISSING for a package

Run `pip install -r requirements.txt` again. On Linux, python-rtmidi and
sounddevice may need system libraries:

```bash
sudo apt install libasound2-dev libjack-dev libportaudio2
```

### No MIDI ports found

- Check that the Beatstep Pro / MIDI Mix is plugged in via USB
- On Linux, make sure your user is in the `audio` group:
  ```bash
  sudo usermod -aG audio $USER
  ```
- Try `midi_ports` again after plugging in the device

### Audio glitches / dropouts

- Increase buffer size: `--buf 1024` or `--buf 2048`
- Close other audio applications that might hold the device
- On Linux, consider using JACK for lower latency

### Plugin fails to load

- Make sure the path points to a `.vst3` bundle (not `.dll` or `.component`)
- pedalboard only supports **VST3** (not VST2)
- On macOS, Audio Units (`.component`) are also supported by pedalboard

### Link shows 0 peers

- All Link peers must be on the same local network (same subnet)
- Firewalls may block Link's UDP multicast -- allow UDP on port 20808
- Check that the other application has Link enabled

### MIDI Mix controls are unresponsive

- Make sure the MIDI Mix is set to **factory default** CC assignments
- The host expects the MIDI Mix to send on MIDI channel 1 (the default)
- Use Akai's MIDI Mix Editor to reset to factory defaults if needed

---

## License

This project uses the following libraries, each with their own license:

| Library | License |
|---|---|
| pedalboard | GPLv3 |
| aalink | GPLv3+ |
| python-rtmidi | MIT |
| mido | MIT |
| numpy | BSD |
| sounddevice | MIT |

If you distribute a binary that bundles pedalboard or aalink, the GPLv3
applies to the combined work.
