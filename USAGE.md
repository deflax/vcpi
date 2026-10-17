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

Startup logging level is controlled by environment variable `LOG_LEVEL`.
Core and `./vcpid` defaults are `INFO` if unset, showing activity messages,
warnings, and errors without DEBUG noise. The Raspberry Pi `payload.service`
explicitly sets `LOG_LEVEL=INFO`. An explicit `LOG_LEVEL=WARNING` hides INFO.

From a connected client, use `debug` to inspect the current daemon logging level,
`debug on` to enable DEBUG, and `debug off` to return to INFO without restarting.
The change applies to the daemon, live audio/slot workers, and subsequently
created workers. It lasts until the daemon exits and does not edit service or
shell configuration. Logs remain in the daemon terminal or service journal,
not in the client response. Use `help debug` for command help.

Raw native/plugin stdout and stderr (including ALSA/JACK probing and plugin
print statements) are discarded unless DEBUG is enabled. With `debug on`, they
stream to the daemon terminal with `[daemon]`, `[audio]`, or `[slot N]` prefixes.
There are no vcpi per-process log files or replay buffers. `debug off` stops
streaming and discards subsequent raw output; structured vcpi activity messages,
warnings, and errors continue to appear. Long or unterminated lines may be split into bounded
prefixed fragments. Existing service-journal retention is managed by systemd,
not by vcpi.

To see startup output, use `LOG_LEVEL=DEBUG ./vcpid` before the daemon starts.
The launcher also gates successful build output: it is quiet by default and
streams with `[build]` prefixes at DEBUG. Failed builds always report their
diagnostics; in quiet mode only the final 64 KiB of a failed step is retained
in memory and printed, with a notice if earlier output was discarded.

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

Type `help` for commands grouped by purpose: instruments & effects; volume,
mute & solo; audio devices & output; MIDI ports & routing; sequencing, tempo
& Link; save & load sessions; status, signal & logging; and help, exit & shutdown.
Commands are alphabetical within each group. Use `help <command>` for
detailed syntax and usage; `help get` and `help set` include parameter-targeting
syntax and examples.
The interactive overview uses one compact table, with a different colour for each
category's heading and command names; descriptions remain neutral. Descriptions
wrap to the terminal width; detailed `help <command>` output is unchanged.

The interactive client uses `prompt_toolkit` for the `vcpi>` command line
and Rich for terminal presentation. `help`, `get`, `status`, `info`, and
`flow` use tables. `flow explore` temporarily opens a full-screen routing
explorer, then returns to the prompt. Piped output remains plain text.
Other responses, including `help <command>`, retain their text.

- Up/Down recall commands from the current client session.
- `Ctrl-C` cancels the current input without disconnecting.
- `Ctrl-D` on an empty prompt disconnects; `quit` and `exit` also disconnect.
- Redirected input or output uses the plain-text client, without Rich formatting.
- For direct Python startup, install dependencies with `.venv/bin/pip install -r requirements.txt`.

When connected with `./vcpi` (or `python main.py cli`), press `Tab` after a
command or partial argument to show matching choices. Suggestions come from the
daemon's shared command grammar and cached state, not a separate client-side
command list. Tab does not execute commands, start audio, or read live plugin
parameters.

| Context | Suggestions |
|---|---|
| Command name / `help` | Available commands and help topics |
| `audio` / `audio start` | Subcommands and cached output-device indexes |
| `midi` / `midimix` | Subcommands, port directions, current hardware indexes, and opened-input close indexes |
| `midi link` / `midi cut` | MIDI channels and routing slots |
| `seq` | Actions, sequence banks, and target slots |
| `ableton` / `debug` | Link actions and debug on/off |
| `gain` / `mute` / `solo` / `note` | Valid slot targets; `gain` also offers `master` |
| `slot` | Slot types, installed plugins, patch names, sample packs/samples, and loaded effect indexes for `fx clear` |
| `info` / `get` / `set` | Plugin targets and actual loaded effect indexes |
| `get` / `set` parameter names | Cached visible parameters, excluding MIDI CC helpers |
| `set` value | Boolean literals and enum choices, quoted when necessary |
| `save` / `load` | Saved session names from the daemon's sessions directory |

Incomplete quotes and multiword plugin/enum names are completed as a whole,
without duplicating words already typed. Session names retain their raw-name
syntax. Arbitrary gain, tempo, note, velocity, and duration values are not
guessed. Commands without arguments have no additional suggestions; unavailable
hardware or empty plugin/effect lists may also yield no choices.

### Plugin Commands

| Command | Description |
|---|---|
| `slot <slot> vst <path\|vst_name> [--name <label>]` | Load VST instrument into slot |
| `slot <slot> wav <pack> <sample> [name]` | Load `sampler/samples/<pack>/<sample>.wav` as one-shot sampler into slot |
| `slot <slot> vcv <patch_name> [name]` | Load Cardinal into slot from `patches/<patch_name>.vcv` |
| `slot <slot\|master> fx <path\|vst_name> [--name <label>]` | Load effect into slot insert chain or master bus |
| `slot <slot> clear` | Clear instrument from slot |
| `slot <slot\|master> fx clear <fx_index>` | Remove effect by index |
| `get <slot> [name]` | Read all visible instrument parameters, or one named parameter |
| `get <slot> fx <fx_index> [name]` | Read all visible slot-effect parameters, or one named parameter |
| `get master [fx_index] [name]` | Read master-effect parameters; effect index defaults to 1 |
| `set <slot> <name> <value>` | Set instrument parameter |
| `set <slot> fx <fx_index> <name> <value>` | Set slot-effect parameter |
| `set master <fx_index> <name> <value>` | Set master-effect parameter; explicit index required |
| `info <slot>` | Show plugin metadata (vendor, category, version, latency, param count) |
| `info <slot> fx <fx_index>` | Show metadata for a slot effect |
| `info master [fx_index]` | Show metadata for a master bus effect |

VST names and paths may contain spaces: `slot 1 vst Acid V` loads the plugin
named `Acid V` and uses that name as the slot label. Quotes are also supported.
To override the display label, use `slot 1 vst Acid V --name Bass`.
The same syntax applies to `slot <slot|master> fx`; trailing words without
`--name` are part of the plugin name, not a custom label.

For `get`, omit `name` to list all visible parameters or supply a single name
to read only that parameter. Use parameter names from the listing; quote names
or values containing spaces. An unknown name produces an error. Slot-effect
targets require `fx <fx_index>`; an index alone is not a slot-effect target.
In `get master`, a numeric token immediately after `master` is always an
effect index, not a parameter name. With no index, `get master` and
`get master <name>` read the first master effect. All `set` forms require
both a name and a value, and `set master` requires an explicit effect index.
Do not add extra target or name arguments.

Examples (parameter names and choices depend on the loaded plugin):

```text
get 1
get 1 cutoff_hz
get 1 fx 2
get 1 fx 2 wet
get master
get master 2
get master 2 threshold_db
get master threshold_db
set 1 cutoff_hz 880
set 1 fx 2 wet 0.5
set master 2 threshold_db -6
help get
help set
```

Each parameter prints a line `  name = value`, with units when available.
When metadata is available, the same line ends with either
`  (range low .. high)` or `  (choices choice1, choice2)`. There are no
slider bars. An empty parameter list prints `  No parameters`.

`set` waits for the owning process to apply the change between render blocks
and prints the actual applied value, including plugin clamping. Boolean values
may use `true`/`false`; quote enum values containing spaces, for example
`set 1 transpose_semitones "24 semitones"`. `get` and `info`
read cached snapshots and never call live plugin getters. Slot metadata is
captured at loading, chain edits, and explicit session-state capture, sequentially
with native rendering in that slot's process. Master metadata capture requires
the output stream stopped. A parameter setter returns only its actual value,
raw/display delta, and acknowledgment—not a fresh catalog of all parameters.
Plugin-side changes between full captures can make diagnostics temporarily stale.

`get` omits JUCE-generated MIDI controller helper parameters
whose labels have the form `MIDI CC <channel>|<controller>`. Ordinary plugin
parameters, including non-automatable controls, remain visible. This is only a
display filter; MIDI CC input and underlying parameter access are unchanged.

### Mixer Commands

| Command | Description |
|---|---|
| `gain <slot> <value>` | Set slot gain (0.0-1.0) |
| `gain master [value]` | Get or set master gain |
| `mute <slot>` | Toggle slot mute |
| `solo <slot>` | Toggle slot solo |
| `flow [all\|midi\|controls\|audio\|1-8\|master\|explore]` | Inspect routing, mapped controls, and output; see visualization commands |

### Audio Commands

All audio operations are subcommands of `audio`:

| Command | Description |
|---|---|
| `audio start [device]` | Start audio engine |
| `audio stop` | Stop audio engine |
| `audio devices` | List audio devices and their input/output channel capacities |
| `audio diagnose` | Show selected output, mixer controls, realtime scheduling, MIDI delivery counters, and lifetime signal peaks |

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
The catalog includes input-only devices; choose an entry with at least two
output channels for stereo playback. Channel capacities come from PortAudio's
device metadata, including CoreAudio devices on macOS.

Running `audio start` again stops and closes the previous output stream before
opening the new one, including when changing devices. If opening or starting
the replacement fails, audio remains stopped.

#### Diagnosing a running but silent engine

`audio diagnose` reads native engine/slot transport counters and cached slot
metadata through the audio owner's control channel. It does not inspect plugins,
refresh the device catalog, or stop rendering. The output includes the selected
output name/backend, running state, master gain, callback errors/underruns,
output and slot realtime scheduling, and each loaded slot's gain, mute/solo
state, worker failure, missing blocks, and:

- **MIDI sent / consumed / pending:** messages published to that slot's native
  MIDI FIFO, messages dequeued by its renderer, and messages still queued.
  Consumption establishes delivery to the renderer, not that the instrument
  accepted the note or produced sound.
- **Slot audio peak:** highest absolute rendered sample after slot inserts,
  before mixer gain/mute/solo.
- **Output peak:** highest absolute final sample submitted to the output
  callback's device buffer, after mixing, master FX/gain, clipping, and failure
  silencing. This does not measure what the physical speakers play.

Peaks are **cumulative high-water marks**, not current meters: the output peak
is retained since audio-owner creation, and each slot peak since that slot's
transport was created. A short test note remains visible after its note-off.
Stopping/restarting audio or changing gain does not reset them. Replacing a slot
starts that slot's peak anew; restarting the daemon creates a new output owner.
An already nonzero peak cannot prove that a later note produced sound, and an
unchanged peak may simply mean that the later signal was quieter.

For a concrete test, use an already loaded instrument slot and take before/after
snapshots (replace `1` with its slot number):

```text
vcpi> audio diagnose
vcpi> note 1 60 100 1000
# Wait at least a second for the note and note-off to reach the renderer.
vcpi> audio diagnose
```

Compare the MIDI counters and inspect the signal chain in order:

1. If output is not running or a worker failure is reported, address that
   failure first. Zero render errors alone do not establish an audible signal.
2. If that slot's sent count does not advance, the test did not reach its native
   MIDI FIFO. If sent advances but consumed does not catch up and pending
   remains nonzero, delivery is stalled before the slot renderer.
3. If consumed advances but the slot peak is still zero, the rendered slot has
   not produced a nonzero sample during its lifetime. Inspect the instrument's
   preset/note range and slot insert chain; correct MIDI delivery is not enough.
4. If the slot peak is nonzero but output peak is still zero, inspect slot gain,
   mute, other slots' solo flags, master gain, and master FX. A solo on another
   slot can exclude this slot even when its own `solo` flag is false.
5. If output peak is nonzero but playback remains inaudible, native output has
   produced signal at some point. Check the displayed selected device against
   the intended speakers/headphones, then macOS/device volume, output routing,
   and physical connections. This result does not certify the current note or
   hardware playback.

When reporting silence, include both `audio diagnose` snapshots and the exact
test-note command. If old peaks obscure a fresh test, use a newly loaded slot
for its peak, or restart the daemon to reset the output owner's peak.


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

#### Selective realtime scheduling

On Linux, native rendering attempts `SCHED_FIFO` priority **20** for the output
callback thread and **10** for each slot DSP thread by default. Promotion is
attempted once per render-thread lifetime: at the output's first actual callback
after stream startup, and at slot-runner startup. Python and process-control
threads retain normal `SCHED_OTHER` scheduling; process isolation is unchanged.
There is no whole-daemon realtime-priority flag. On macOS and other non-Linux
platforms, this feature reports `unsupported` and changes no scheduling policy.
Build the extension on each platform as described in `README.md`.

`status` displays cached output/slot scheduling health, without RPC to native
owners. `audio diagnose` requests fresh native stats through the control
channel. Live OS policy/priority queries happen on the control side, not in the
render callback. Both show the scheduling state, observed policy/priority,
requested priority, native Linux thread ID (TID), and any errno for running
threads:

| State | Meaning |
|---|---|
| `inactive` | No output stream is active; TID is 0. |
| `pending` | The output stream is starting, but its first callback has not attempted promotion yet. |
| `active` | The live thread has `SCHED_FIFO` at exactly the requested priority. |
| `denied` | The initial promotion failed; the observed policy/priority and errno show the actual result. |
| `degraded` | Promotion succeeded initially, but the live policy/priority no longer matches the request. |
| `unavailable` | The live thread's scheduling policy/priority cannot be queried. |
| `unsupported` | This selective scheduling feature is Linux-only. |

An output stop resets scheduling health to `inactive`; restarting attempts
promotion again on the new callback thread. Slot promotion health remains
associated with that slot's runner lifetime even while output is stopped.
`denied` is nonterminal: rendering continues at the actual policy, commonly
`SCHED_OTHER priority 0`, with e.g. `errno=1 (Operation not permitted)`.
Check the reported result rather than assuming requested priority means success.

Native `Engine.stats()` and `Transport.stats()` expose a `realtime` dictionary
with `state`, `policy`, `priority` (actual), `requested_priority`, `error` (integer
errno, 0 on success), and `thread_id` (native Linux TID, 0 before startup).
The control facade carries output health as `RemoteAudioEngine.output_realtime`
and slot health as `InstrumentSlot.worker_realtime` in initial state and through
its existing periodic update channel.

Linux permission must be configured separately from promotion. The supplied
[Pi service](rpi-build/README.md#selective-realtime-scheduling) grants a finite
`LimitRTPRIO=20` allowance inherited by children while still running as `pi`;
it does not set a default FIFO policy or add `CAP_SYS_NICE`. An SSH/login shell
does not inherit the service's allowance, so manual startup may report `denied`;
inspect the shell's limit with `ulimit -r`. Do not run the whole daemon under
`chrt` or as root to bypass this distinction.

This scheduling change does **not** change buffers or one-block handoff latency,
or guarantee that overloaded plugins, device backends, or the OS meet deadlines.
Measure callback timing, underruns, and missing slot blocks before tuning;
global realtime promotion, wakeup replacement, memory locking, affinity,
governor changes, and kernel tuning are outside this feature.

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

- Per-slot instruments, native VST3 state, effects, gain, mute/solo
- Native master-effect state and master gain
- MIDI channel routing
- BPM and Ableton Link state
- Sequencer banks and links
- Audio output device and MIDI connections

New saves use session format **2**: each VST3 instrument, slot insert, and master
effect stores a base64-encoded native state chunk. This preserves plugin-owned
settings, including settings not exposed as parameters, without replaying MIDI
helpers or action controls. WAV slots remain path-based.

Saving waits for prior controls/MIDI and captures each slot between render jobs.
Pending VST3 parameter edits require one MIDI-free processing block before
serialization; ordinary captures without pending edits do not advance DSP, and
WAV capture never renders an extra block. Capture can delay the inspected slot.
Without master effects, saving leaves the output stream running. With master
effects, it also pauses the output stream for native master-state capture.
Read-only diagnostics never capture chunks; ordinary updates do not carry them.

Restoration stays paused until plugin/control changes are complete. Native state
is validated before loading; deferred plugin loads are prerolled without MIDI
before metadata refresh and subsequent notes. A state chunk takes precedence
over any parameter dictionary. Invalid/missing format-2 chunks are reported,
not silently replaced by parameter replay. State transfers retain the ordinary
metadata limits and allow at most 128 MiB per chunk and 512 MiB per transfer,
with finite control-operation deadlines.

Existing format-1 sessions remain readable. Only parameters present in the
newly loaded plugin and marked automatable are restored, in batches of at most
64; skipped unknown/nonautomatable controls produce warnings. This conservative
migration may lose nonautomatable settings, and automation flags cannot identify
every vendor-specific action. Back up old sessions before migration; reselect a
preset if its original non-parameter state was never saved. Reloaded slots
replace already-corrupted live instances; saving a silent instance alone does
not repair its state.

JSON is written in the control process. A failed native owner or state capture
makes saving fail rather than overwrite the last good session.

On startup restore, vcpi attempts to reconnect audio and MIDI targets
automatically.

An interrupted startup retires native owners without saving partially restored
state; the original session file remains unchanged. Native owners ignore
foreground Ctrl-C, leaving the control process to cancel pending work and
perform teardown. Canceled control channels cannot accept a late reply as a
subsequent operation. Slot-parent supervision waits for confirmed parent death
before unlinking orphaned shared semaphore resources.

### Visualization Commands

The `flow` and `info` commands use cached control-side
metadata from the native JUCE VST3 host. Parameter inspection uses `get`,
described under [Plugin Commands](#plugin-commands). Numeric parameters retain
display values and unit-qualified names (kHz displays use Hz values).
Text choices and boolean switches are inferred at load even when a plugin
omits VST3 step counts. `raw_value` remains normalized.

**`flow`** shows a routing overview: connected MIDI inputs, MIDI Mix connections,
sequencer clock, all eight instrument/FX chains, slot eligibility, master bus,
and audio output. Focused views expose the details without one oversized table:

| Command | View |
|---|---|
| `flow` | Compact whole-system overview |
| `flow midi` | Open generic inputs, all 16 channel routes, sequencer bank destinations and patterns |
| `flow controls` | MIDI Mix CC/note bindings, knob targets, cached values/ranges, and LED feedback connection |
| `flow audio` | Ordered audio chains, output device/backend, stream format, and output health |
| `flow 1` … `flow 8` | Slot source/path, MIDI and sequencer inputs, inserts, mixer/worker state, and controls |
| `flow master` | Master effects, gain/control, and output |
| `flow all` | Every view, including empty slots and unlinked routes |
| `flow explore` | Read-only full-screen explorer in the interactive client |

Inside `flow explore`:

- Left/Right or Tab/Shift-Tab cycle through views.
- `o`, `m`, `c`, `a` jump to overview, MIDI, controls, or audio.
- `1`–`8` jump to a slot; `9` opens the master bus.
- Up/Down, PageUp/PageDown, Home/End scroll the selected view.
- `r` refreshes the snapshot. Refresh is manual; navigation sends no requests.
  A failed refresh leaves the previous snapshot visible with an error.
- `q`, Escape, Ctrl-C, or Ctrl-D return to the command prompt.

Tables wrap to the terminal width. Redirected input/output uses plain-text
tables; `flow explore` explains the interactive-client requirement instead of
blocking the daemon.

In colour-capable terminals, state words use **green** for on/true, active,
connected, running, or ready; **red** for off/false, stopped, muted, disabled,
blocked, or failed; and **yellow** for inactive, disconnected, unlinked, empty,
unknown, or pending states. Boolean control values and LED on/off indicators
use the same colours. Labels remain visible without colour; a reported
`failure: none` is not marked as a failure.

Generic MIDI inputs share the channel map; sequencer banks target slots directly.
MIDI Mix knobs map to the first three **instrument** parameters in insertion
order, not insert or master-effect parameters. Disconnected ports and unbound
controls are explicit. Mute/solo, disabled slots, zero gains, stopped audio, and
worker/owner failures are shown as output blockers.

These views perform no hardware discovery, native-owner requests, or routing
changes. They show cached applied values and routing eligibility, **not live
meters or proof of audible sound**. Link tempo is the cached configured tempo,
not a fresh peer-tempo reading. There is no external audio-input path.

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

`info` supports slot instruments, per-slot effects, and master bus effects:

```text
info 1            # slot 1 instrument
info 1 fx 1       # slot 1, first effect
info master 1     # first master bus effect
```

To inspect the corresponding parameters, use `get`:

```text
get 1             # slot 1 instrument
get 1 fx 2        # slot 1, second effect
get master        # first master bus effect (index defaults to 1)
```

For example, numeric parameters with range metadata print as:

```text
  cutoff_hz = 880.0 Hz  (range 20 .. 2e+04)
  resonance = 0.3  (range 0 .. 1)
  attack_ms = 50.0 ms  (range 0 .. 5000)
```

Choice parameters list available choices instead of a numeric range:

```text
  mode = Hall  (choices Room, Hall, Plate)
```

### Status and Exit

| Command | Description |
|---|---|
| `about` | Print the vcpi logo and basic project info |
| `status` | Print combined system status |
| `ps` | Show daemon/child processes and threads with sampled CPU and resident memory |
| `deps` | Check optional dependency availability |
| `debug [on\|off]` | Show current logging level; enable DEBUG or return to INFO |
| `shutdown` | Shut down the vcpi daemon process |
| `quit` / `exit` / `Ctrl-D` | Disconnect this client session |

`ps` samples the daemon and its descendants (audio owner, loaded slot workers,
and helper processes) over approximately **200 ms**. It excludes the separate
CLI client and unrelated system processes. PID/TID and process roles identify
each row; process rows show state, total CPU, RSS in MiB, and thread count.
Thread rows show native thread IDs, names where available, and CPU usage.
**100% CPU means one fully occupied core**; process CPU includes its threads
and can exceed 100%. Thread memory is shared with the process, so it is not
repeated or summed. RSS across processes can also include shared pages.
A dash indicates a non-applicable field or a newly observed thread without
a CPU baseline. Tasks that exit during collection are omitted; inaccessible
details are reported explicitly. Run `ps` again to refresh.

Collection uses `psutil` on the control side, without audio/plugin RPCs or
render-callback changes. Other control commands wait during the short sample;
native audio rendering continues independently. Linux thread names come from
`/proc`, with Python names for daemon threads; other platforms may show generic
thread names.

`status` reads shared output-callback health and cached metadata; it does not
contact native owners or query PortAudio while rendering. `Audio owner`
identifies the output/mixer PID; `Audio failure` takes precedence over stale
running flags. `Render` distinguishes maximum slot-process capacity from the
eligible slot count in the last callback. `Slot buffering` states added latency.
Each `Slot N worker` row shows its PID, failure, and cumulative missing blocks;
these cached worker-health fields refresh approximately every 100 ms.

`Output realtime` and each `Slot N realtime` row report the selective scheduling
state and, for live threads, actual versus requested priority, policy, TID, and
any errno. These are cached control-side observations, not a status-time RPC.
An `active` scheduling row is not a guarantee of meeting the audio deadline.

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
If the output owner has terminally failed, shutdown warns that it cannot capture
native state and preserves the existing session file instead of overwriting it
with cached state. Changes since the last successful save are not persisted.
Explicit `save` commands still fail when the owner is unavailable; unrelated
save errors and cleanup failures remain errors.
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
