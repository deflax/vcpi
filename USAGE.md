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
| `./vcpid` | Starts headless server mode; creates `.venv` and synchronizes dependencies |
| `./vcpi` | Connects interactive client mode; creates `.venv` and synchronizes dependencies |
| `python main.py serve` | Starts headless server with Unix socket control |
| `python main.py cli` | Connects to a running headless server |

Both launchers install `requirements.txt` when the environment is new, when
upgrading an older environment without a dependency snapshot, or when the
requirements file changes. Successful installs are recorded in
`$VENV_DIR/.vcpi-requirements.txt` (`VENV_DIR` defaults to the repo `.venv`).
Unchanged requirements skip installation; failed installs stop startup and
are retried on the next launch. Direct Python startup does not install dependencies.

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
(e.g. via systemd `payload.service`), `./vcpid` will refuse to start and
print the existing PID. Connect to the running instance with `./vcpi` instead.
The PID file is stored at `~/.config/vcpi/vcpi.pid`.

Logging level is controlled by environment variable `LOG_LEVEL`.
Core default is `WARNING`. `./vcpid` defaults it to `DEBUG` if unset.
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
LOG_LEVEL=INFO ./vcpid
LOG_LEVEL=WARNING python main.py serve
```

Server/client specific:

| Mode | Flag | Default | Description |
|---|---|---|---|
| `serve` | `--sock` | auto (see above) | Unix socket to bind |
| `cli` | `--sock` | auto (see above) | Unix socket to connect to |

## Interactive Commands

Type `help` for commands grouped by function: plugins, mixer, audio, MIDI,
sequencer & sync, sessions, status & diagnostics, and help & lifecycle.
Commands are alphabetical within each group. Use `help <command>` for
detailed syntax and usage.

The interactive client uses `prompt_toolkit` for the `vcpi>` command line
and Rich for terminal presentation; it is not a full-screen interface.
`help`, `params`, `status`, and `info` use tables. `flow` retains its diagram
layout with color, and `knobs` highlights filled slider bars. Other responses,
including `help <command>`, retain their text.

- Up/Down recall commands from the current client session.
- `Ctrl-C` cancels the current input without disconnecting.
- `Ctrl-D` on an empty prompt disconnects; `quit` and `exit` also disconnect.
- Redirected input or output uses the plain-text client, without Rich formatting.
- For direct Python startup, install dependencies with `.venv/bin/pip install -r requirements.txt`.

When connected with `./vcpi` (or `python main.py cli`), press `Tab` to
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
| `slot <slot> vst <path\|vst_name> [--name <label>]` | Load VST instrument into slot |
| `slot <slot> wav <pack> <sample> [name]` | Load `sampler/samples/<pack>/<sample>.wav` as one-shot sampler into slot |
| `slot <slot> vcv <patch_name> [name]` | Load Cardinal into slot from `patches/<patch_name>.vcv` |
| `slot <slot\|master> fx <path\|vst_name> [--name <label>]` | Load effect into slot insert chain or master bus |
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

VST names and paths may contain spaces: `slot 1 vst Acid V` loads the plugin
named `Acid V` and uses that name as the slot label. Quotes are also supported.
To override the display label, use `slot 1 vst Acid V --name Bass`.
The same syntax applies to `slot <slot|master> fx`; trailing words without
`--name` are part of the plugin name, not a custom label.

`set` waits for the owning process to apply the change between render blocks
and prints the actual applied value, including plugin clamping. Boolean values
may use `true`/`false`; quote enum values containing spaces, for example
`set 1 transpose_semitones "24 semitones"`. `params`, `info`, and `knobs`
read cached snapshots and never call live plugin getters. Slot metadata is
captured at loading, chain edits, and explicit session-state capture, sequentially
with native rendering in that slot's process. Master metadata capture requires
the output stream stopped. A parameter setter returns only its actual value,
raw/display delta, and acknowledgment—not a fresh catalog of all parameters.
Plugin-side changes between full captures can make diagnostics temporarily stale.

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

The daemon spawns an audio-output process and a persistent native process per
loaded slot. Command parsing/display work, MIDI ports, Link, sequencing, and
session-file I/O remain in Python. Each slot's C++ render thread owns its
instrument and insert effects; the C++ PortAudio callback owns mixing and
master FX. Neither rendering path calls Python or acquires the GIL. The callback
never waits for slot rendering, command responses, display work, or filesystem
operations. Python API entry points constructing `VcpiCore` must use the usual
`if __name__ == "__main__":` spawn guard.

`audio devices` displays the cached device catalog, refreshed at audio-owner
startup and while stopped during `audio start`/`audio stop`. After replugging
devices, stop audio before using the catalog's current indexes.

Running `audio start` again stops and closes the previous output stream before
opening the new one, including when changing devices. If opening or starting
the replacement fails, audio remains stopped.

All slot audio uses preallocated shared-memory stereo buffers with sequence
tags and nonblocking ownership checks. Each callback consumes the previous
slot block and requests the next; this adds **one block** to note-to-sound
latency: **512 samples / 11.610 ms** at the defaults, in addition to backend and
plugin latency. The three handoff cells do not add three blocks of latency.
An unavailable/late block silences only that slot for that block and increments
its missing-block counter; old blocks are never replayed. Muted and solo-excluded
slots still render so MIDI note-offs and plugin tails advance. Gain/mix, sampler,
and event storage are preallocated. Processes increase memory/scheduling cost;
native rendering removes interpreter contention but does not guarantee hard
real-time behavior from the OS, device backend, or third-party plugins.

Native output controls use a bounded 128-entry FIFO. Each slot has a fixed
4096-message MIDI FIFO with a maximum encoded message size of 1024 bytes.
MIDI order and note-offs are preserved even when late audio jobs are skipped.
Heavy traffic can increase rendering/control latency. Global ingress saturation
explicitly stops the output owner; slot MIDI overflow or oversized messages
explicitly fail only that slot. Events for a retired generation cannot affect
its replacement.

Instrument/WAV/Cardinal loads initialize and warm a new slot process while
existing slots continue playing. A ready replacement switches at a callback
boundary; only then is the old process retired. Failed loading leaves the old
source playing. Removal stops only that slot. Insert edits and parameter
inspection/setters use native plugin ownership and may briefly interrupt that
slot, not the output stream or other slots. Failed chain construction/warmup
retains the previous chain. Master-chain edits and native master metadata capture
still pause the output stream. Restoration deliberately stays paused until
complete. If reopening fails, audio remains stopped; failed native teardown is
terminal when callback quiescence cannot be established.

For persistent underruns, restart the daemon with `./vcpid --buf 1024` (or
`2048`), reconnect, and issue `audio start`. Larger buffers provide more
scheduling headroom at the cost of note-to-sound latency.

There is no built-in real-time-priority flag. Native rendering is not itself
OS real-time scheduling; elevated scheduling remains a separate platform-specific
configuration. Build the extension on each platform as described in `README.md`.

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
Without Ableton Link, playback fires once per position on a shared timing grid,
including banks with different pattern lengths. Late wake-ups skip missed
positions rather than retriggering notes or emitting a catch-up burst.

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
Loading a session replaces the current master-effects chain rather than
appending another copy of the saved effects.

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

Parameter snapshots preserve numeric, boolean, and enum/string values. Saving
waits for prior controls/MIDI and captures each slot's native state between its
render jobs. Without master effects it leaves the output stream running; native
capture can delay the inspected slot. With master effects it also pauses the
output stream for fresh master state. JSON is written in the control process.
Restoration stays paused until plugin/control changes are complete. Parameters
are applied in ordered batches of at most 64 values, preserving numeric,
boolean, and enum types. Individual incompatible parameters are reported while
other entries continue; a terminal native-owner failure aborts that plugin's
restore. Read-only diagnostics do not perform native capture. A failed slot or
output owner makes saving fail rather than overwrite the last good session.

On startup restore, vcpi attempts to reconnect audio and MIDI targets
automatically.

An interrupted startup retires native owners without saving partially restored
state; the original session file remains unchanged. Native owners ignore
foreground Ctrl-C, leaving the control process to cancel pending work and
perform teardown. Canceled control channels cannot accept a late reply as a
subsequent operation. Slot-parent supervision waits for confirmed parent death
before unlinking orphaned shared semaphore resources.

### Visualization Commands

The `flow`, `info`, and `knobs` commands render ASCII diagrams using cached
metadata from the native JUCE VST3 host. Numeric parameters retain display
values and unit-qualified names (kHz displays use Hz values). Text choices and
boolean switches are inferred at load even when a plugin omits VST3 step counts.
`raw_value` remains normalized.

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

`status` reads shared output-callback health and cached metadata; it does not
contact native owners or query PortAudio while rendering. `Audio owner`
identifies the output/mixer PID; `Audio failure` takes precedence over stale
running flags. `Render` distinguishes maximum slot-process capacity from the
eligible slot count in the last callback. `Slot buffering` states added latency.
Each `Slot N worker` row shows its PID, failure, and cumulative missing blocks;
these cached worker-health fields refresh approximately every 100 ms.

`Callback time` shows mixer/master callback last/peak execution time and the
block deadline, **not native instrument/insert rendering time**. A fast mixer
with missing slot blocks is still audible trouble. `Deadline misses` counts
output callbacks exceeding that deadline, not every OS scheduling delay.
`Underruns` counts backend-reported output underflows; zero does not imply zero
slot dropouts. `Render errors` counts output processing errors. Output counters
are cumulative across stream restarts; a replacement slot starts new worker
counters. The control tick copies native callback counters into shared health
fields; the audio callback does not log or call Python.

If a running output callback stops completing for two seconds (or eight block
periods, whichever is longer), supervision stops the output owner. Native output
control acknowledgments have a five-second deadline. A timeout permanently
fails/silences the owner; queued work cannot later produce an audible success.

A slot render stalled for two seconds is terminated locally. Slot initialization
and insert lifecycle waits are bounded at 90 seconds; setters/native snapshot
requests at five seconds. Lifecycle work is exempt from the render-stall timer.
The output callback never waits for those deadlines. Slot failure leaves other
slots playing; reload that slot to recover. No automatic restart hides lost
plugin state. Children exit when their owning output process dies.

Output-owner death/channel failure produces explicit stopped/error state while
cached diagnostics and client disconnect remain usable. Restart the daemon
after a terminal output-owner failure.

`shutdown` saves before teardown, stops MIDI/sequencing and joins pending
note-off timers, disables Link, then shuts down the output owner and slot children.
Aborted startup uses the Python API `host.shutdown(save=False)` instead, avoiding
a session capture/write while still releasing resources. Plugin references and
cyclic parameter caches are released on their native owner's main thread.
Cleanup still runs after save failure. Deliberately interrupted RPCs request
cooperative output-owner cleanup with a bounded ten-second grace period;
unresponsive children are then terminated/killed with bounded waits. Signal
handlers unwind pending RPCs instead of re-entering their locks.

## Example Workflows

Headless server + client shell:

```bash
# Terminal 1
./vcpid

# Terminal 2
./vcpi
```

Server logs now include:

- CLI command execution (`[CLI] ...`)
- MIDI input events (`[MIDI IN] ...`)
- MIDI Mix events (`[MIDI Mix] ...`)
