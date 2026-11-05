# vcpi Usage Reference

This reference covers command-line startup flags and interactive `vcpi>`
commands.

Source of truth:

- `core/main.py`
- `core/cli.py`

## Conventions

- Tracks are **1-8**.
- MIDI channels are **1-16**.
- `fx_index` values are **1-based** in CLI commands.
- `master` means the global master effects bus.
- Hardware audio/MIDI port indexes are **0-based**; use the indexes printed by discovery.
- Closing a MIDI input uses its **1-based active-input number**, not its hardware port index.
- Main targets and values are positional; optional labels and modifiers use `--flags`.
- Quote paths, session names, labels, and device names containing spaces.
- Fixed-arity commands reject unexpected arguments. Options cannot be silently ignored.

## Startup Modes

| Command | What it does |
|---|---|
| `./vcpid` | Starts headless server mode; creates `.venv` and synchronizes dependencies |
| `./vcpi` | Connects interactive client mode; creates `.venv` and synchronizes dependencies |
| `python main.py serve` | Starts headless server with Unix socket control |
| `python main.py cli` | Connects to a running headless server |

Server startup displays the vcpi logo regardless of `LOG_LEVEL`, before
initializing the host.

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
| `--sample-rate <hz>` | `44100` | Positive integer audio sample rate in Hz; startup-only |
| `--buffer-size <frames>` | `512` | Positive integer buffer size in frames; startup-only |
| `--bpm <value>` | restored, otherwise `120.0` | Positive finite tempo in beats per minute |
| `--link` / `--no-link` | restored, otherwise off | Explicitly enable or disable Ableton Link |
| `--midi-input <port> [port ...]` | restored | MIDI input indexes or quoted names; repeatable, occurrences accumulate |
| `--midimix-input <port>` | restored | MIDI Mix input index or quoted name |
| `--midimix-output <port>` | restored | MIDI Mix output index or quoted name (LED feedback) |
| `--output <device>` | restored, otherwise unopened | Start audio on this output index or quoted name, overriding the saved output |
| `--session <path>` | `~/.config/vcpi/session.json` | Automatic session file path |
| `--no-restore` | off | Skip restoring the automatic session at startup |

When running `serve`, vcpi automatically reopens a saved audio output after
restoring the session. An explicit `--output` starts that device instead, without
opening the saved device first, including with `--no-restore`. A fresh session
without a saved or explicit output stays stopped; use `audio start [device]`.
This restores the previous saved-output behavior: no separate running/stopped
flag is stored, so even an output stopped before saving is reopened on restart.

Precedence for tempo, Link, and device connections is **defaults → restored
session → explicit startup options**. Overridden saved connections are not
opened first. Explicit `--midi-input` replaces the saved input selection; use
multiple values or repeated occurrences to open several inputs. Sample rate and
buffer size come from startup options/defaults, not the session.

```bash
./vcpid --sample-rate 48000 --buffer-size 512 --bpm 128 --no-link
./vcpid --midi-input 0 --midi-input "Keyboard MIDI" --output "USB Audio"
```

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
The change applies to the daemon, live audio/track workers, and subsequently
created workers. It lasts until the daemon exits and does not edit service or
shell configuration. Logs remain in the daemon terminal or service journal,
not in the client response. Use `help debug` for command help.

### Setting lifetime

| Settings | Lifetime and source |
|---|---|
| Sample rate, buffer size, socket, automatic session path | Startup options; restart the daemon to change |
| Instruments/effects, mixer, routing, tempo, Link, sequences, device targets | Session state; explicit startup options override matching restored settings |
| `debug on` / `debug off` | Current daemon and workers only; not saved in sessions |
| `LOG_LEVEL`, `VST3_PATH`, `VST_PATH`, `VCPI_PATCHES_DIR`, `CARDINAL_VST3_PATH`, `PLUGDATA_VST3_PATH` | Environment inherited at startup; runtime commands do not edit shell/service configuration |


Raw native/plugin stdout and stderr (including ALSA/JACK probing and plugin
print statements) are discarded unless DEBUG is enabled. With `debug on`, they
stream to the daemon terminal with `[daemon]`, `[audio]`, or `[track N]` prefixes.
There are no vcpi per-process log files or replay buffers. `debug off` stops
streaming and discards subsequent raw output; structured vcpi activity messages,
warnings, and errors continue to appear. Long or unterminated lines may be split into bounded
prefixed fragments. Existing service-journal retention is managed by systemd,
not by vcpi.

To see runtime startup diagnostics, use `LOG_LEVEL=DEBUG ./vcpid` before the
daemon starts. The launcher's native CMake configure and build output always
streams with `[build]` prefixes, regardless of `LOG_LEVEL`, including on the
first launch. Failed builds report the failing command and stop startup.

VST3 plugin search and name resolution:

- `track <n> vst` and `track <n> fx` accept a plugin **name** (e.g. `Dexed`) or a
  full path (e.g. `/usr/lib/vst3/Dexed.vst3`). Names are resolved by scanning
  known search directories.
- Search order: directories from `VST3_PATH`, then `VST_PATH`, followed by
  the working directory, the repository root, `~/.vst3`, `/usr/lib/vst3`, `/usr/local/lib/vst3`,
  `~/Library/Audio/Plug-Ins/VST3`, and `/Library/Audio/Plug-Ins/VST3`.
  The former repository/working-directory `vst3/` subdirectories are not searched automatically.
- Name matching: exact stem match first, then case-insensitive, then unique
  prefix match. Ambiguous names produce an error listing the candidates.
- Tab completion for `track <n> vst` and `track <n> fx` lists all detected plugin names.
- Install plugins compatible with your OS and CPU architecture in a standard
  user/system directory above, or set `VST3_PATH` or `VST_PATH` to a
  colon-separated list of additional plugin directories before starting the daemon.

Cardinal/VCV helpers:

- `track <n> vcv` looks for patch files in `patches/` by default.
- `track <n> vcv` does not auto-route channels; use `midi link <ch> <track>` explicitly.
- Override patch directory with `VCPI_PATCHES_DIR`.
- Override Cardinal plugin path with `CARDINAL_VST3_PATH`.
- Author patches in Cardinal with Host MIDI and Host Audio modules wired to the
  instrument. Both JSON and compressed `.vcv` projects are imported through
  Cardinal's VST3 `patch` state; a raw `.vcv` file is not a VST3 preset/state chunk.
  Unsupported plugin state interfaces report an error rather than falling back
  to an empty instance.
- Session saves capture the loaded plugin state, so restoring a current-format
  session does not require re-importing the original `.vcv` file.
- If `audio diagnose` shows MIDI consumed but a track peak of zero, inspect the
  patch import and internal MIDI/audio wiring before changing output devices.

plugdata/Pure Data helpers:

- Install the **instrument** `plugdata.vst3` bundle from
  [plugdata v0.9.3-2](https://github.com/plugdata-team/plugdata/releases/tag/v0.9.3-2)
  for your OS and CPU architecture. The standalone app and `plugdata-fx.vst3`
  are not substitutes for this track source.
- Pi images built with `rpi-build/prepare-image.sh` install this pinned ARM64
  instrument bundle automatically at `/usr/local/lib/vst3/plugdata.vst3`.
  No manual plugin install, path override, or first-boot download is needed.
- Install the bundle in `~/.vst3`, `/usr/lib/vst3`, or `/usr/local/lib/vst3`
  on Linux, or `~/Library/Audio/Plug-Ins/VST3` or
  `/Library/Audio/Plug-Ins/VST3` on macOS. For another location, set
  `PLUGDATA_VST3_PATH` to the bundle's full path before starting the daemon.
  Repository-local and working-directory plugin folders are not searched
  automatically.
- `track <n> pd <patch_name[.pd]> [--name <label>]` loads from `patches/`,
  sharing `VCPI_PATCHES_DIR` with Cardinal. Absolute paths and nested patch names
  are accepted. Quote names containing spaces, or use the same multiword-name
  syntax as `track <n> vst`; labels require `--name`.
- Import runs headlessly in a staged track process. Failure leaves the previous
  track intact. MIDI routing remains explicit: `midi link <ch> <track>`.
- Patches must be UTF-8 Pure Data files, at most 16 MiB. Use MIDI objects such as
  `notein` and audio output such as `dac~`; there is no external audio-input path
  into an instrument track. The included `sine` patch is a velocity-sensitive
  monophonic sine instrument with 5 ms fades and a full-velocity peak of 0.6.
  `classic-subtractive-synth` keeps the tutorial's oversampled oscillator and
  `bob~` filter, with MIDI input and a self-contained amplitude/filter envelope
  (5 ms attack, 150 ms decay, 65% sustain, 180 ms release). Neither requires
  GUI interaction or the tutorial's `output~` / `adsr` abstractions.
  `poly-pad` is a self-contained eight-voice stereo
  pad with oldest-note stealing, velocity sensitivity, detuned saw oscillators,
  a sine body, and a 1.8 kHz low-pass filter. Its squared amplitude envelope has
  an 800 ms attack and 2200 ms release; hold notes for at least a second to hear
  the full swell. It uses only vanilla Pd objects, with no external assets.
- The importer recognizes plugdata's **0.9.3** state layout (the version reported
  by release v0.9.3-2). Other versions/layouts fail explicitly rather than
  silently loading an empty instance.
- Sessions capture the main patch in native plugin state, so restoration does
  not require its original `.pd` file. Relative abstractions, samples, and other
  dependencies are **not bundled**; keep them at their original locations.
  Custom externals must be supported by the installed plugdata build.

```text
track 1 pd sine
midi link 1 1
audio start
note 1 60 --velocity 100 --duration-ms 500
```

Load the polyphonic pad and play it from MIDI channel 1:

```text
track 1 pd poly-pad
midi link 1 1
audio start
note 1 60 --velocity 100 --duration-ms 3000
```

`poly-pad` exposes three plugdata automation controls, shared by all eight voices:

| MIDI Mix knob | Parameter | Default | Range |
|---|---|---|---|
| 1 | `cutoff` | 1800 Hz | 20–12000 Hz |
| 2 | `attack` | 800 ms | 1–5000 ms |
| 3 | `release` | 2200 ms | 1–10000 ms |

For example, `set 1 cutoff 950` changes the pad's filter and
`set 1 attack 350` shortens its fade-in. Attack applies to subsequent note-ons;
release applies to subsequent note-offs. Long envelopes still need time to play.
The MIDI Mix fader remains the track gain, not one of these patch controls.
Automation values are preserved in native plugin session state.
To edit oscillator detuning or output gain, open `patches/poly-pad.pd` in
Pure Data or plugdata and keep those settings matched across the eight voices.
The pad's output is 6 dB louder than the original example while retaining
headroom for chords. Reload it with `track 1 pd poly-pad` after updating
the file; a running instance or saved session can still contain the older patch.
In standalone Pure Data, enable DSP and configure a MIDI input; vcpi handles
audio processing when hosted in plugdata. The patch itself accepts all MIDI
channels, so use vcpi's routing to select which channel reaches the track.

For short sequencer notes, use `sine` or `classic-subtractive-synth` rather
than the pad's slow swell. For example:

```text
track 1 pd classic-subtractive-synth
audio start
note 1 60 --velocity 127 --duration-ms 100
```

`classic-subtractive-synth` exposes these plugdata automation controls through
the patch-native [`param` object](https://github.com/plugdata-team/plugdata/blob/v0.9.3/Resources/Patches/param-help.pd):

| MIDI Mix knob | Parameter | Default | Range |
|---|---|---|---|
| 1 | `cutoff` | 300 Hz | 20–12000 Hz |
| 2 | `resonance` | 2.5 | 0–4 |
| 3 | `envelope_amount` | 2000 Hz | 0–8000 Hz |

The envelope amount is added to the base cutoff according to the existing
filter envelope. Defaults preserve the original patch sound. For example,
`set 1 cutoff 1800` changes the base cutoff while the MIDI Mix fader still
controls the track's gain. plugdata's separate `volume` remains available to
`get`/`set`, but is excluded from automatic knob mapping.

Reload with `track 1 pd classic-subtractive-synth` to use the updated file.
Restoring an older session can bring back its embedded, pre-automation patch.

Both monophonic patches accept note-on/note-off on all MIDI channels and replace
the current note when a newer note arrives. Releasing a stolen older note does
not stop the newer one. Use `midi link <ch> <track>` for external MIDI routing,
or `seq link <bank> <track>` for vcpi's internal sequencer. Reload a patch after
updating its file; previously saved sessions retain their embedded old version.

Examples:

```bash
LOG_LEVEL=INFO ./vcpid
LOG_LEVEL=WARNING python main.py serve
```

Server/client specific:

| Mode | Flag | Default | Description |
|---|---|---|---|
| `serve` | `--socket <path>` | auto (see above) | Unix socket to bind |
| `cli` | `--socket <path>` | auto (see above) | Unix socket to connect to |

## Interactive Commands

Type `help` for commands grouped by purpose. System-related groups come first:
save & load sessions; status, signal & logging; and help, exit & shutdown.
A horizontal separator follows these groups. The overview command, `show`,
appears next, immediately before instruments & effects; then Mixer;
audio devices & output; MIDI ports & routing; and sequencing, tempo & Link.
The separator appears in both plain and interactive output.
Commands follow an explicit order within each group: `save`, `load`; `status`,
`ps`, `deps`, `debug`; `help`, `about`, `quit`, `shutdown`; `show`; `track`, `get`,
`set`; `gain`, `solo`, `mute`; `audio`; `midi`, `midimix`, `note`; and `tempo`,
`ableton`, `seq`. The `exit` alias appears in the description of `quit`, not as a
separate row; it remains available as a command and completion.
Use `help <command>` for detailed syntax, defaults, and usage. Nested topics
such as `help track wav`, `help audio start`, and `help midi input close`
focus on one operation; Tab completes valid topics. Unknown topics are errors.
The interactive overview uses one compact table, with a different colour for each
category's heading and command names; descriptions remain neutral. Descriptions
wrap to the terminal width; detailed help remains readable plain text.

The interactive client uses `prompt_toolkit` for the `vcpi>` command line
and Rich for terminal presentation. `help`, `get`, `status`, and
`show <view>` use tables. Plain `show` opens a full-screen routing explorer
with a visible tab bar, then returns to the prompt. Piped output remains plain text.
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
| `midi link` / `midi cut` | MIDI channels and routing tracks |
| `seq` | Actions, sequence banks, and target tracks |
| `ableton` / `debug` | Link actions and debug on/off |
| `gain` / `mute` / `solo` / `note` | Track targets, mixer states, and note option flags; `gain` also offers `master` |
| `track` | Track types, installed plugins, patch names, sample packs/samples, and loaded effect indexes for `fx clear` |
| `show` / `get` / `set` | View or plugin targets and actual loaded effect indexes |
| `get` parameter names | Cached musical controls and `--controls`, `--settings`, `--midi`, `--unused`, `--all` |
| `get <target> <listing flag>` | No further arguments |
| `set` parameter names | Cached controls and plugin settings; no MIDI or unused slots |
| `set` value | Boolean literals and enum choices, quoted when necessary |
| `save` / `load` | Saved session names from the daemon's sessions directory |
| `ps` | `--all`, `--cpu`, and `--mem`; conflicting/used options are not offered |

Incomplete quotes and multiword plugin/enum names are completed as a whole,
without duplicating words already typed. Session names containing spaces are
quoted on insertion. Arbitrary gain, tempo, note, velocity, and duration values are not
guessed. Commands without arguments have no additional suggestions; unavailable
hardware or empty plugin/effect lists may also yield no choices.

### Plugin Commands

| Command | Description |
|---|---|
| `track <track> vst <path\|vst_name> [--name <label>]` | Load VST instrument into track |
| `track <track> wav <pack> <sample> [--name <label>]` | Load `sampler/samples/<pack>/<sample>.wav` as one-shot sampler into track |
| `track <track> deck <audio-file> [--name <label>]` | Create a paused streaming deck, or append to its session-owned playlist |
| `track <track> vcv <patch_name> [--name <label>]` | Load Cardinal into track from `patches/<patch_name>.vcv` |
| `track <track> pd <patch_name[.pd]> [--name <label>]` | Load plugdata with a Pure Data patch from `patches/` or an absolute path |
| `track <track\|master> fx <path\|vst_name> [--name <label>]` | Load effect into track insert chain or master bus |
| `track <track> clear` | Clear instrument from track |
| `track <track\|master> fx clear <fx_index>` | Remove effect by index |
| `get <track> [name \| <listing flag>]` | Read instrument controls, a selected group, or one exact parameter |
| `get <track> fx <fx_index> [name \| <listing flag>]` | Read track-effect controls, a selected group, or one exact parameter |
| `get master [fx_index] [name \| <listing flag>]` | Read master-effect controls, a selected group, or one exact parameter; index defaults to 1 |
| `set <track> <name> <value>` | Set instrument parameter |
| `set <track> fx <fx_index> <name> <value>` | Set track-effect parameter |
| `set master <fx_index> <name> <value>` | Set master-effect parameter; explicit index required |
| `show <track>` | Show track state and full cached instrument metadata |
| `show <track> fx <fx_index>` | Show full metadata for a track effect; explicit 1-based index required |
| `show master fx <fx_index>` | Show full metadata for a master bus effect; explicit 1-based index required |

VST names and paths may contain spaces: `track 1 vst Acid V` loads the plugin
named `Acid V` and uses that name as the track label. Quotes are also supported.
To override the display label, use `track 1 vst Acid V --name Bass`.
The same syntax applies to `track <track|master> fx`; trailing words without
`--name` are part of the plugin name, not a custom label.
All source types use `--name <label>` for optional display labels. Quote VCV
patch names, WAV pack/sample names, and deck paths containing spaces; positional
labels are not accepted. For example:

```text
track 1 wav piano c4-soft --name "Soft Piano"
track 2 deck "/music/opening song.flac" --name "Deck B"
track 3 vcv "Warm Pad" --name Atmosphere
```


For `get`, omit `name` to list musical controls, or supply a single exact name
to read any cached parameter, including MIDI helpers and unused slots.
Alternatively, use one listing flag: `--controls` (the default), `--settings`,
`--midi`, `--unused`, or `--all`. Flags affect listing only; they cannot be
combined with each other or with a parameter name. Use parameter names from the
listing; quote names or values containing spaces. An unknown name produces an error.
Track-effect targets require
`fx <fx_index>`; an index alone is not a track-effect target.
In `get master`, a numeric token immediately after `master` is always an
effect index, not a parameter name. With no index, `get master` and
`get master <name>` read the first master effect. All `set` forms require
both a name and a value, and `set master` requires an explicit effect index.
Do not add extra target or name arguments.

Examples (parameter names and choices depend on the loaded plugin):

```text
get 1
get 1 --settings
get 1 --all
get 1 cutoff_hz
get 1 fx 2
get 1 fx 2 wet
get 1 fx 2 --midi
get master
get master 2
get master 2 threshold_db
get master threshold_db
get master --unused
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
`--all` emits `Controls:`, `Plugin settings:`, `MIDI parameters:`, and
`Unused parameters:` sections, preserving the same parameter rows within each.
The interactive Rich table retains those section headings. Single-group
listings use the normal ungrouped format, including `No parameters` if empty.

`set` waits for the owning process to apply the change between render blocks
and prints the actual applied value, including plugin clamping. Boolean values
may use `true`/`false`; quote enum values containing spaces, for example
`set 1 transpose_semitones "24 semitones"`. `get` and `show`
read cached snapshots and never call live plugin getters. Track metadata is
captured at loading, chain edits, and explicit session-state capture, sequentially
with native rendering in that track's process. Master metadata capture requires
the output stream stopped. A parameter setter returns only its actual value,
raw/display delta, and acknowledgment—not a fresh catalog of all parameters.
Plugin-side changes between full captures can make diagnostics temporarily stale.

Parameter groups distinguish plugin controls from infrastructure:

- `controls` (default): musical controls, including legitimate zero values and
  non-automatable controls. Cardinal's 24 CV `Parameter N` controls remain here.
- `settings`: known plugdata bypass/program controls and Cardinal bypass/window
  settings, such as tooltips, cable opacity, and knob mode.
- `midi`: JUCE-generated `MIDI CC <channel>|<controller>` helpers and Cardinal's
  `MIDI Ch <channel> CC <controller>` bank.
- `unused`: plugdata slots whose raw labels begin with `(DISABLED)`.
- `all`: every cached parameter, separated into the four groups above.

Plugin-specific grouping is identity-scoped; unrelated plugin controls are not
classified as settings or unused merely because of their name or value.
The plugdata rule follows its documented
[`(DISABLED)` parameter titles](https://github.com/plugdata-team/plugdata/blob/v0.9.3/Source/Utility/PluginParameter.h);
Cardinal's settings follow its
[host parameter definitions](https://github.com/DISTRHO/Cardinal/blob/26.02/src/CardinalPlugin.cpp).
These groups do not create patch connections: a Pure Data patch must export
automation controls, and Cardinal's host parameters must be wired inside the patch.
`get` name completion suggests controls and the listing flags; use
`get <target> --settings` to discover setting names. `set` name completion
includes controls and settings, excluding MIDI and unused slots. Explicit MIDI
names remain readable and writable; setting an unused slot fails before any
engine mutation. MIDI CC input, raw cached parameters, and native plugin state
chunks are unchanged. Automatic knob mapping uses controls only, skipping
settings, MIDI parameters, unused slots, and plugdata's global `volume`.
The MIDI Mix fader continues to control vcpi's track gain; the separate plugdata
volume remains accessible through `get`/`set` but does not consume a knob.

### Deck Commands

Decks play continuous tracks, not MIDI-triggered `wav` sampler voices. They detect
a beat grid automatically and follow the global tempo and beat phase by default,
using Ableton Link's timeline when enabled. Play/pause is still controlled per
deck, independently of global transport start/stop. Create up to eight ordinary mixer tracks:

```text
track 1 deck /music/opening.wav --name Deck-A
track 1 deck /music/second.flac
track 2 deck /music/other.mp3 --name Deck-B
tempo 120
audio start
set 1 play true
set 2 play true
```

Each deck owns an ordered playlist saved inside the session; no separate JSON
bank file is required. The first audio-file command creates a paused deck.
Repeating it appends another song without replacing the deck, interrupting the
selected song, or losing mixer settings, routing or inserts. An existing name
is preserved unless `--name` is supplied.

```text
track 1 deck status
set 1 track_select 2
track 1 deck remove 1
save live-set
```

Status lists song titles, paths and 1-based selection indexes. Removing an
unselected song preserves playback; indexes close up around the removed entry.
Removing the selected song prepares the next song at that index (or the previous
song when removing the last entry), paused at zero. The final song cannot be
removed individually: use `track 1 clear` to remove the deck.

Audio paths are expanded and saved as absolute paths. Stable song IDs preserve
the selected identity independently of playlist indexes. Session files contain
the playlist metadata, not copies of audio files; the audio must remain available.
Catalogs have a 512 KiB transfer budget with 1024-byte text fields and no
128-song limit. Large catalogs use bounded multipart IPC, not larger frames.

Supported audio is mono (duplicated to stereo) or stereo AIFF (`.aif`, `.aiff`,
`.aifc`, supported encodings), WAV (`.wav`, `.wave`, supported PCM/float encodings),
MP3 and FLAC, at source rates from 8 kHz to 768 kHz. Other formats and multichannel
layouts are rejected. Decoding uses native JUCE readers; FFmpeg is not a runtime
dependency. Audio is streamed through bounded read-ahead storage and retained
sinc interpolation, not loaded in full for playback. Decks do not apply an
additional fixed low-pass prefilter: playback prioritizes preserving source
bandwidth rather than cutting treble for the maximum possible speed. Speeding
up audio or converting to a lower output sample rate can therefore introduce
aliasing; the sinc interpolator is not a ratio-adaptive anti-alias filter.
For original-speed playback, use `set <track> sync false`, `set <track> pitch 0`
and `set <track> nudge 0`. Sync and manual speed changes still change pitch.

| Command | Behaviour |
|---|---|
| `get 1` | Deck parameters and their values, units and ranges, available for `set` |
| `track 1 deck status` | Cached playlist, identity, transport, detected grid, sync state and errors |
| `set 1 track_select 2` | Request entry 2; acknowledgement means accepted, not loaded |
| `track 1 deck <audio-file> [--name <label>]` | Append a song, or create a paused one-song deck |
| `track 1 deck remove 2` | Remove playlist entry 2; final-entry removal requires `track 1 clear` |
| `set 1 seek 90` | Seek in source seconds, retaining play state; range follows selected song duration |
| `set 1 play true` / `set 1 play false` | Resume / pause, preserving source position |
| `set 1 sync true` / `set 1 sync false` | Follow global/Link tempo and beat phase / use manual pitch and nudge |
| `set 1 pitch -1.25` | Vinyl-style speed and pitch, -16 to +16 percent |
| `set 1 nudge 1` / `set 1 nudge 0` | Temporary speed offset (-1..+1 gives up to ±2%); release to zero |
| `track 1 deck seek 90` | Seek in original-track seconds, bounded to track duration; retain play state |
| `track 1 deck cue set` | Store current source position without moving |
| `track 1 deck cue` | Recall cue and pause |

The first three exposed sound controls are **`track_select`, `seek`, `nudge`**,
so MIDI Mix maps them automatically to the top, middle and bottom knobs of each
track strip. Selection spans playlist entries 1..N; seek spans 0..song duration;
nudge spans -1..+1. Playlist edits and committed song changes refresh those
ranges. Nudge affects manual playback when sync is off or unavailable; return it
to zero after nudging. Remaining controls are `play`, `pitch` and `sync`.

`get <track>` lists only the deck's get/set parameters, just like other sources;
`get <track> <parameter>` reads one parameter. Use `track <track> deck status`
for transport diagnostics and the indexed playlist. Empty `error` or `sync_error`
fields in status are normal when no error is present.

Queen Mary DSP analyzes the entire selected file off the render thread, using
complex-domain spectral onset detection and tempo tracking. vcpi fits a constant
tempo and phase from supported stable beat regions; the old energy-envelope
detectors are no longer used. Analysis normalizes its private input to 22.05 kHz
with a 512-frame FFT and 256-frame hop; this never resamples or filters the
playback signal. Source audio is decoded in chunks, not retained in full.

An uncached load waits for analysis and read-ahead preparation before becoming
ready. During selection, existing audio remains usable while the new file is
analyzed. The fitted range is 60–200 BPM and requires at least 16 supported
stable beats. A beatless intro can be followed by a usable region later in the
file. Variable tempo, short clips or ambiguous rhythms may still lack a reliable
constant grid; half/double-tempo ambiguity is not universally resolvable.

Status reports `bpm`, `beat_offset` (source seconds modulo a beat),
`beat_confidence` (0–1), `sync`, `sync_active`, `sync_error`, `playback_speed` and
`beat_phase_error` (signed beats, wrapped to ±0.5). Confidence measures agreement
and phase residual among accepted stable regions, not a probability or the
fraction of the entire song with beats. Unreliable analysis reports
`No reliable beat grid` and leaves playback manual.

Analysis is cached persistently as small per-file JSON records:

| Platform / setting | Cache directory |
|---|---|
| Absolute `XDG_CACHE_HOME` set | `$XDG_CACHE_HOME/vcpi/analysis/` |
| Linux default | `~/.cache/vcpi/analysis/` |
| macOS default | `~/Library/Caches/vcpi/analysis/` |

The same audio file shares its result across decks, playlists, session restores
and daemon restarts. Identity includes the canonical path, file size, device and
inode, high-resolution modification/change times, and analyzer/grid version.
Changed files or analyzer versions invalidate the entry; moving a file to a new
canonical path requires fresh analysis. No-grid results are cached too.

The cache is separate from sessions and music folders. Missing, malformed or
stale records are recomputed; unavailable/unwritable storage does not prevent
loading. Writes are atomic across concurrent deck processes. To force fresh
analysis, stop the daemon, delete the applicable analysis cache directory, then
restart and reload the session. Deleting the cache never deletes music or
playlist data. Set `XDG_CACHE_HOME` before starting the daemon to choose a
different cache root; relative values are ignored.

Set the target with `tempo 128`; enable Link normally to follow its shared
timeline instead. Local tempo changes preserve beat continuity. Sync matches
tempo and gradually corrects beat phase, rather than jumping the source cursor;
allow a few seconds to settle after starting, seeking or changing tempo.
Supported target/source tempo ratios are 0.5–2.0, with up to ±8% temporary phase
correction. Unsupported ratios report an error and leave playback manual.
This aligns beats, not automatically detected bar/downbeat or musical phrases.

Sync uses vinyl-style speed/pitch changes, **not key lock or time stretching**.
While sync is active it owns playback speed; stored pitch/nudge controls apply
when sync is off or unavailable. For manual beatmatching:

```text
set 1 sync false
set 1 pitch -1.25
set 1 nudge 1
set 1 nudge 0
```

Manual pitch and nudge are smoothed; pitch changes pitch as well as speed.
Selections/seek prepare outside rendering; the old track remains usable until an
audio-side commit. Successful selection fades out, resets cue/position to zero,
clears nudge and pauses, retaining pitch, mixer, routing and inserts. Newest
selection wins; selecting the active entry cancels a pending selection. A failed
load keeps current playback and reports the requested identity and error.

Mute continues transport. Stopping output freezes position without changing the
deck play flag; pending prepared changes commit when rendering resumes. EOF stops
at duration without wrapping; Play from ended restarts at cue. Temporary shortages
fade to silence and hold position until matching data arrives. Status progress is
capped at 10 Hz. There is no key lock/time stretching, scratching, reverse,
looping, waveform UI or crossfader.

Routed generic MIDI input uses the following local deck mapping. Route the device
channel explicitly with `midi link <channel> <track>`, then open its input port.
Other source types retain their usual MIDI behaviour.

| MIDI message | Deck action |
|---|---|
| Note 60 press | Toggle play/pause |
| Note 61 press | Recall cue and pause |
| Note 62 press | Set cue |
| Note 63 press/release | Hold/release negative nudge |
| Note 64 press/release | Hold/release positive nudge |
| CC 20, value 0..127 | Pitch linearly -16..+16 percent |
| CC 21, value 0..127 | Nearest playlist selection 1..N (7-bit resolution) |

Note-off and velocity-zero note-on both release; opposing nudge holds cancel.
Closing the input, removing its channel route or replacing the deck clears held
nudge. MIDI Mix mute/solo buttons are unchanged and are not nudge bindings.
Use CLI integer selection to address every entry in playlists larger than MIDI's
7-bit selection resolution.

Physical listening/manual-beatmatching acceptance on Linux and macOS and
read-ahead/deadline measurements on the intended Pi hardware remain unverified;
hardware-free rendering does not establish live hardware suitability.

### Mixer Commands

| Command | Description |
|---|---|
| `gain <track\|master> [value]` | Read or set linear gain (0.0–1.0) |
| `mute <track> [on\|off\|toggle]` | Set mute explicitly; omitted state toggles |
| `solo <track> [on\|off\|toggle]` | Set solo explicitly; omitted state toggles |
| `show [all\|midi\|controls\|audio\|1-8\|master]` | Open the tabbed explorer, or print a focused routing/plugin view; see visualization commands |

`gain 1` reads without changing state. `mute 1 on` and `solo 1 off` are
idempotent; bare `mute 1` and `solo 1` are convenient toggle shortcuts.

### Audio Commands

All audio operations are subcommands of `audio`:

| Command | Description |
|---|---|
| `audio start [device]` | Start audio using the preferred/restored output, or select an index or quoted full name; report opened device/backend |
| `audio stop` | Stop audio engine |
| `audio devices` | List audio devices, channel capacities, and backend; mark the active or last-opened entry |
| `audio diagnose` | Show opened output, mixer controls, realtime scheduling, MIDI delivery counters, and lifetime signal peaks |

The daemon spawns an audio-output process and a persistent native process per
loaded track. Command parsing/display work, MIDI ports, Link, sequencing, and
session-file I/O remain in Python. Each track's C++ render thread owns its
instrument and insert effects; the C++ PortAudio callback owns mixing and
master FX. Neither rendering path calls Python or acquires the GIL. The callback
never waits for track rendering, command responses, display work, or filesystem
operations. Python API entry points constructing `VcpiCore` must use the usual
`if __name__ == "__main__":` spawn guard.

`audio devices` displays the cached device catalog, refreshed at audio-owner
startup and while stopped during `audio start`/`audio stop`. After replugging
devices, stop audio before using the catalog's current indexes.
The catalog includes input-only devices; choose an entry with at least two
output channels for stereo playback. Channel capacities come from PortAudio's
device metadata, including CoreAudio devices on macOS.

`audio start` without an argument uses PortAudio's default output. After a
successful start, vcpi prints the opened entry as `[index] name` with its
backend. The same index/name appears in `status`, `audio diagnose`, and the
`show` output views; `audio devices` marks it `[active]`. Once stopped, the
cached identity is retained as the last-opened device, not an active stream.
The identity changes only after a replacement stream starts successfully.

On ALSA, a device named `default` is a system alias: PortAudio does not identify
the physical output behind it. vcpi labels that limitation explicitly rather
than guessing a sound card. To select an unambiguous hardware output, use
`audio devices`, then `audio start <index>` for the desired stereo output
entry. Full names containing spaces are also accepted without quotes:
`audio start USB Audio Device`.

Running `audio start` again stops and closes the previous output stream before
opening the new one, including when changing devices. If opening or starting
the replacement fails, audio remains stopped.

#### Diagnosing a running but silent engine

`audio diagnose` reads native engine/track transport counters and cached track
metadata through the audio owner's control channel. It does not inspect plugins,
refresh the device catalog, or stop rendering. The output includes the selected
output index/name/backend, running state, master gain, callback errors/underruns,
output and track realtime scheduling, and each loaded track's gain, mute/solo
state, worker failure, missing blocks, and:

- **MIDI sent / consumed / pending:** messages published to that track's native
  MIDI FIFO, messages dequeued by its renderer, and messages still queued.
  Consumption establishes delivery to the renderer, not that the instrument
  accepted the note or produced sound.
- **Track audio peak:** highest absolute rendered sample after track inserts,
  before mixer gain/mute/solo.
- **Output peak:** highest absolute final sample submitted to the output
  callback's device buffer, after mixing, master FX/gain, clipping, and failure
  silencing. This does not measure what the physical speakers play.

Peaks are **cumulative high-water marks**, not current meters: the output peak
is retained since audio-owner creation, and each track peak since that track's
transport was created. A short test note remains visible after its note-off.
Stopping/restarting audio or changing gain does not reset them. Replacing a track
starts that track's peak anew; restarting the daemon creates a new output owner.
An already nonzero peak cannot prove that a later note produced sound, and an
unchanged peak may simply mean that the later signal was quieter.

For a concrete test, use an already loaded instrument track and take before/after
snapshots (replace `1` with its track number):

```text
vcpi> audio diagnose
vcpi> note 1 60 --velocity 100 --duration-ms 1000
# Wait at least a second for the note and note-off to reach the renderer.
vcpi> audio diagnose
```

Compare the MIDI counters and inspect the signal chain in order:

1. If output is not running or a worker failure is reported, address that
   failure first. Zero render errors alone do not establish an audible signal.
2. If that track's sent count does not advance, the test did not reach its native
   MIDI FIFO. If sent advances but consumed does not catch up and pending
   remains nonzero, delivery is stalled before the track renderer.
3. If consumed advances but the track peak is still zero, the rendered track has
   not produced a nonzero sample during its lifetime. Inspect the instrument's
   preset/note range and track insert chain; correct MIDI delivery is not enough.
4. If the track peak is nonzero but output peak is still zero, inspect track gain,
   mute, other tracks' solo flags, master gain, and master FX. A solo on another
   track can exclude this track even when its own `solo` flag is false.
5. If output peak is nonzero but playback remains inaudible, native output has
   produced signal at some point. Check the displayed selected device against
   the intended speakers/headphones, then macOS/device volume, output routing,
   and physical connections. This result does not certify the current note or
   hardware playback.

When reporting silence, include both `audio diagnose` snapshots and the exact
test-note command. If old peaks obscure a fresh test, use a newly loaded track
for its peak, or restart the daemon to reset the output owner's peak.


All track audio uses preallocated shared-memory stereo buffers with sequence
tags and nonblocking ownership checks. Each callback consumes the previous
track block and requests the next; this adds **one block** to note-to-sound
latency: **512 samples / 11.610 ms** at the defaults, in addition to backend and
plugin latency. The three handoff cells do not add three blocks of latency.
An unavailable/late block silences only that track for that block and increments
its missing-block counter; old blocks are never replayed. Muted and solo-excluded
tracks still render so MIDI note-offs and plugin tails advance. Gain/mix, sampler,
and event storage are preallocated. Processes increase memory/scheduling cost;
native rendering removes interpreter contention but does not guarantee hard
real-time behavior from the OS, device backend, or third-party plugins.

Native output controls use a bounded 128-entry FIFO. Each track has a fixed
4096-message MIDI FIFO with a maximum encoded message size of 1024 bytes.
MIDI order and note-offs are preserved even when late audio jobs are skipped.
Heavy traffic can increase rendering/control latency. Global ingress saturation
explicitly stops the output owner; track MIDI overflow or oversized messages
explicitly fail only that track. Events for a retired generation cannot affect
its replacement.

Instrument/WAV/Cardinal/plugdata loads initialize and warm a new track process while
existing tracks continue playing. A ready replacement switches at a callback
boundary; only then is the old process retired. Failed loading leaves the old
source playing. Removal stops only that track. Insert edits and parameter
inspection/setters use native plugin ownership and may briefly interrupt that
track, not the output stream or other tracks. Failed chain construction/warmup
retains the previous chain. Master-chain edits and native master metadata capture
still pause the output stream. Restoration deliberately stays paused until
complete. If reopening fails, audio remains stopped; failed native teardown is
terminal when callback quiescence cannot be established.

For persistent underruns, restart the daemon with `./vcpid --buffer-size 1024` (or
`2048`), reconnect, and issue `audio start`. Larger buffers provide more
scheduling headroom at the cost of note-to-sound latency.

#### Selective realtime scheduling

On Linux, native rendering attempts `SCHED_FIFO` priority **20** for the output
callback thread and **10** for each track DSP thread by default. Promotion is
attempted once per render-thread lifetime: at the output's first actual callback
after stream startup, and at track-runner startup. Python and process-control
threads retain normal `SCHED_OTHER` scheduling; process isolation is unchanged.
There is no whole-daemon realtime-priority flag. On macOS and other non-Linux
platforms, this feature reports `unsupported` and changes no scheduling policy.
Build the extension on each platform as described in `README.md`.

`status` displays cached output/track scheduling health, without RPC to native
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
promotion again on the new callback thread. Track promotion health remains
associated with that track's runner lifetime even while output is stopped.
`denied` is nonterminal: rendering continues at the actual policy, commonly
`SCHED_OTHER priority 0`, with e.g. `errno=1 (Operation not permitted)`.
Check the reported result rather than assuming requested priority means success.

Native `Engine.stats()` and `Transport.stats()` expose a `realtime` dictionary
with `state`, `policy`, `priority` (actual), `requested_priority`, `error` (integer
errno, 0 on success), and `thread_id` (native Linux TID, 0 before startup).
The control facade carries output health as `RemoteAudioEngine.output_realtime`
and track health as `InstrumentTrack.worker_realtime` in initial state and through
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
Measure callback timing, underruns, and missing track blocks before tuning;
global realtime promotion, wakeup replacement, memory locking, affinity,
governor changes, and kernel tuning are outside this feature.

### MIDI Commands

All MIDI operations are subcommands of `midi`:

| Command | Description |
|---|---|
| `midi ports input` | List MIDI input ports |
| `midi ports output` | List MIDI output ports |
| `midi input <port>` | Open a MIDI input by 0-based hardware index or quoted name |
| `midi input close <active-input-number>` | Close an opened input by its 1-based position in the active list, not hardware index |
| `midi link <ch> <track>` | Route MIDI channel to track |
| `midi cut <ch>` | Remove MIDI channel route |
| `midimix input <port>` | Open Akai MIDI Mix input by index or quoted name |
| `midimix output <port>` | Open Akai MIDI Mix output by index or quoted name (LED feedback) |
| `note <track> <note> [--velocity <value>] [--duration-ms <value>]` | Send a test MIDI note (0–127); velocity defaults to 100, duration to 300 ms |

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
Quoted full port names are accepted too, for example `midi input "Keyboard MIDI"`.
`midi input close 1` closes the first active input shown by `show midi`, not
hardware port 1. Active-input numbers shift when an input is closed.
Note velocity must be 0–127 and duration must be positive and finite.

You can open multiple MIDI inputs simultaneously. All MIDI inputs share the
same channel routing table (`midi link <ch> <track>`).

Important: `midi link <ch> <track>` only maps MIDI channels internally. It does
not open hardware ports; `midi input` and `midimix input` do.

WAV sampler behavior:

- `track <n> wav` plays the file when MIDI `note_on` events reach that track.
- `track 2 wav 909 bassdrum` resolves to `sampler/samples/909/bassdrum.wav`.
- Built-in packs: `808`, `909`, `piano`, `organ`, `strings`, `synth-pads`, `synth-leads`.
- The `.wav` extension is optional in `<sample>`.
- Notes are pitch-shifted around middle C (MIDI note 60).
- It is one-shot playback (note-off does not cut the sample).

Melodic examples:

```text
vcpi> track 1 wav piano c4-soft
vcpi> track 2 wav organ c4-drawbar
vcpi> track 3 wav strings c4-ensemble
vcpi> track 4 wav synth-pads c4-warm
vcpi> track 5 wav synth-leads c4-mono-saw
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
| `seq link <bank> <track>` | Attach sequence bank to a track (starts playback) |
| `seq cut <track>` | Remove all sequence links from a track |

Note names are case-insensitive. Sharps (`C#`), flats (`Bb`), and octave
suffixes (`C5`, `F#3`) are supported. Default octave is 4 (middle C).

Examples:

```text
vcpi> seq 1 d c b a        # bank 1: D4 C4 B4 A4, plays as 4 quarter notes
vcpi> seq 2 c              # bank 2: just C4, plays once per bar
vcpi> seq 3 C#5 Bb4 G4     # bank 3: 3 notes per bar
vcpi> seq link 1 5          # play bank 1 through track 5
vcpi> seq link 2 3          # play bank 2 through track 3
vcpi> seq cut 5             # stop sequence on track 5
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

Tempo must be positive and finite for startup, `tempo`, and `ableton link`.
Omitting BPM reads the current tempo with `tempo`, or preserves it when enabling
Link with `ableton link`.

### Session Commands

| Command | Description |
|---|---|
| `save [name]` | Save to `sessions/<name>.json`, or to the active loaded session when name is omitted |
| `load <name>` | Load session from `sessions/<name>.json` |

Named sessions are saved in the working directory's `sessions/` directory if
present, otherwise the repository's `sessions/` directory. Tab completion lists
available session names; quote names containing spaces.
Loading a session replaces the current master-effects chain rather than
appending another copy of the saved effects.

Bare `save` requires a previously loaded named session; otherwise supply a name.
The automatic session defaults to `~/.config/vcpi/session.json` (overridden by
`--session`). Shutdown writes the automatic session. `load` also updates it so
that shutdown preserves the loaded state; named session files are written only
by explicit `save`.

Saved/restored session state includes:

- Per-track instruments, native VST3 state, effects, gain, mute/solo
- Native master-effect state and master gain
- MIDI channel routing
- BPM and Ableton Link state
- Sequencer banks and links
- Audio output device and MIDI connections

New saves use session format **3**: each VST3 instrument, track insert, and master
effect stores a base64-encoded native state chunk. This preserves plugin-owned
settings, including settings not exposed as parameters, without replaying MIDI
helpers or action controls. WAV tracks remain path-based.
Pure Data/plugdata tracks also use native chunks, embedding the main patch while
retaining its original location for resolving external files. Abstractions and
sample files must remain installed separately.
Decks instead store an explicit `deck` payload containing the full ordered
`tracks` list (stable ID, absolute audio path and title), playlist identity,
committed stable `track_id`, `cue` seconds, `pitch` and boolean `sync`. They do not
store a VST3 chunk for the source; insert chunks are preserved normally. Restore
needs no external playlist manifest: it resolves the saved selection, prepares
at cue paused, sets nudge to zero and reanalyzes the selected audio.
Legacy bank-based deck sessions are still readable; their manifests are resolved
on restore and subsequent saves embed the playlist. Older payloads without
`sync` default to automatic sync. Missing audio, IDs or legacy banks are reported
per track; unaffected tracks continue restoring. A pending selection is never
persisted in place of the committed track.


Format 3 stores mixer instruments under `tracks` and sequencer destinations
under `sequences[].linked_track`. New saves write only these names. Formats 1
and 2 remain readable: their legacy `slots` and `linked_slot` keys are migrated
when loading, and the next save writes format 3. These file-format migrations
do not provide legacy CLI command aliases.

Saving waits for prior controls/MIDI and captures each track between render jobs.
Pending VST3 parameter edits require one MIDI-free processing block before
serialization; ordinary captures without pending edits do not advance DSP, and
WAV capture never renders an extra block. Capture can delay the inspected track.
Without master effects, saving leaves the output stream running. With master
effects, it also pauses the output stream for native master-state capture.
Read-only diagnostics never capture chunks; ordinary updates do not carry them.

Restoration stays paused until plugin/control changes are complete. Native state
is validated before loading; deferred plugin loads are prerolled without MIDI
before metadata refresh and subsequent notes. A state chunk takes precedence
over any parameter dictionary. Invalid/missing format-2/3 chunks are reported,
not silently replaced by parameter replay. State transfers retain the ordinary
metadata limits and allow at most 128 MiB per chunk and 512 MiB per transfer,
with finite control-operation deadlines.

For format-1 sessions, only parameters present in the newly loaded plugin and
marked automatable are restored, in batches of at most
64; skipped unknown/nonautomatable controls produce warnings. This conservative
migration may lose nonautomatable settings, and automation flags cannot identify
every vendor-specific action. Back up old sessions before migration; reselect a
preset if its original non-parameter state was never saved. Reloaded tracks
replace already-corrupted live instances; saving a silent instance alone does
not repair its state.

JSON is written in the control process. A failed native owner or state capture
makes saving fail rather than overwrite the last good session.

Startup restores saved MIDI connections and reopens the saved audio output.
Explicit startup device flags override saved connections; `--no-restore` skips
saved connections, while an explicit `--output` still starts audio.
Interactive `load` also reconnects/starts the saved audio output after restoration.

An interrupted startup retires native owners without saving partially restored
state; the original session file remains unchanged. Native owners ignore
foreground Ctrl-C, leaving the control process to cancel pending work and
perform teardown. Canceled control channels cannot accept a late reply as a
subsequent operation. Track-parent supervision waits for confirmed parent death
before unlinking orphaned shared semaphore resources.

### Visualization Commands

The `show` command uses cached control-side
metadata from the native JUCE VST3 host. Parameter inspection uses `get`,
described under [Plugin Commands](#plugin-commands). Numeric parameters retain
display values and unit-qualified names (kHz displays use Hz values).
Text choices and boolean switches are inferred at load even when a plugin
omits VST3 step counts. `raw_value` remains normalized.

**`show`** opens a read-only tabbed explorer in the interactive client, starting
with a routing overview: connected MIDI inputs, MIDI Mix connections, sequencer
clock, all eight instrument/FX chains, track eligibility, master bus, and audio
output. Focused commands print individual tables:
The command is named `show` (formerly `flow`); the old command name is not an alias.

| Command | View |
|---|---|
| `show` | Full-screen tabbed explorer; compact whole-system overview in plain output |
| `show midi` | Open generic inputs, all 16 channel routes, sequencer bank destinations and patterns |
| `show controls` | MIDI Mix CC/note bindings, knob targets, cached values/ranges, and LED feedback connection |
| `show audio` | Ordered audio chains, output device/backend, stream format, and output health |
| `show 1` … `show 8` | Track source/path, MIDI and sequencer inputs, inserts, mixer/worker state, controls, and full cached instrument metadata |
| `show <track> fx <index>` | Full cached metadata for the selected track effect |
| `show master` | Master effects, gain/control, and output |
| `show master fx <index>` | Full cached metadata for the selected master effect |
| `show all` | Every view, including empty tracks, unlinked routes, and loaded track/master effect metadata sections |

Effect metadata views require an explicit 1-based index. `show master` remains
the master routing/mixer/output view; it does not select an effect implicitly.
Loaded effect sections also appear in the explorer's view cycle.
Plugin metadata separates reported parameter totals from sound controls, plugin
settings, MIDI parameters, and unused slots. Automatable, boolean, and discrete
counts describe all reported parameters, not just the sound-control group.

Inside `show`:

- The tab bar at the top lists available views and highlights the active tab.
  Tabs wrap to fit the terminal width.
- Left/Right or Tab/Shift-Tab cycle through views.
- `o`, `m`, `c`, `a` jump to overview, MIDI, controls, or audio.
- `1`–`8` jump to a track; `9` opens the master bus.
- Up/Down, PageUp/PageDown, Home/End scroll the selected view.
- `r` refreshes the snapshot. Refresh is manual; navigation sends no requests.
  A failed refresh leaves the previous snapshot visible with an error.
- `q`, Escape, Ctrl-C, or Ctrl-D return to the command prompt.

Tables wrap to the terminal width. Redirected input/output uses plain-text
tables; plain `show` prints the overview without opening a full-screen interface.

In colour-capable terminals, state words use **green** for on/true, active,
connected, running, or ready; **red** for off/false, stopped, muted, disabled,
blocked, or failed; and **yellow** for inactive, disconnected, unlinked, empty,
unknown, or pending states. Boolean control values and LED on/off indicators
use the same colours. Labels remain visible without colour; a reported
`failure: none` is not marked as a failure.

Generic MIDI inputs share the channel map; sequencer banks target tracks directly.
MIDI Mix knobs map to the first three **instrument sound controls** in insertion
order, skipping plugin settings, MIDI parameters, unused slots, and plugdata's global
volume—not insert or master-effect parameters. Zero and non-automatable sound
controls remain eligible.
Disconnected ports and unbound
controls are explicit. Mute/solo, disabled tracks, zero gains, stopped audio, and
worker/owner failures are shown as output blockers.

MIDI Mix faders adjust track gains and the master gain. Adjacent queued mixer
edits are combined into one snapshot at an audio callback boundary, keeping
the latest value for each field without waiting one audio block per CC event.
Applied-state views and LED reconciliation update after that boundary.
Track replacements and explicit control barriers retain FIFO ordering;
mixer batches do not cross them.

These views perform no hardware discovery, native-owner requests, or routing
changes. They show cached applied values and routing eligibility, **not live
meters or proof of audible sound**. Link tempo is the cached configured tempo,
not a fresh peer-tempo reading. There is no external audio-input path.

`show <track>` combines existing routing, mixer, worker, and control state with
the instrument's cached name, path, vendor, version, category, type, ID, latency,
description, and parameter counts (total, automatable, boolean, and discrete).
Effect views expose the same full cached plugin metadata for the selected effect:

```text
show 1             # track 1 state and instrument metadata
show 1 fx 1        # track 1, first effect metadata
show master fx 1   # first master bus effect metadata
```

Metadata inspection is part of `show`; there is no `info` command or alias.

To inspect the corresponding parameters, use `get`:

```text
get 1             # track 1 instrument
get 1 fx 2        # track 1, second effect
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
| `ps [--all] [--cpu\|--mem]` | Show daemon/child processes and threads; optionally sort by descending CPU or resident memory |
| `deps` | Check optional dependency availability |
| `debug [on\|off]` | Show current logging level; enable DEBUG or return to INFO |
| `shutdown` | Shut down the vcpi daemon process |
| `quit` / `exit` / `Ctrl-D` | Disconnect this client session |

The interactive startup banner and `about` logo use a diagonal yellow
(`#FFE037`) → green (`#00C600`) → purple (`#8404D9`) gradient, with broad yellow
and purple regions and a narrow green centre. Truecolour terminals display the
exact colours; other colour terminals approximate them. Non-interactive output
remains plain text.

`ps` samples the daemon and its descendants (audio owner, loaded track workers,
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

`ps --cpu` sorts processes by descending sampled CPU and sorts threads within
each process by CPU (threads without a baseline come last). `ps --mem` sorts
processes by descending RSS; thread rows stay in TID order because they share
their process's memory. Threads remain grouped under their parent process.
Without either flag, the daemon comes first, followed by children in PID order,
with threads in TID order. Use only one sort flag; either can be combined with
`--all`, for example `ps --all --cpu` or `ps --mem --all`.

Collection uses `psutil` on the control side, without audio/plugin RPCs or
render-callback changes. Other control commands wait during the short sample;
native audio rendering continues independently. Linux thread names come from
`/proc`, with Python names for daemon threads; other platforms may show generic
thread names.

By default, `ps` hides idle threads with generic names such as `python` when
their sampled CPU rounds to 0.0%. Process main threads, named threads, and
known output/track render threads remain visible. `ps --all` shows every thread.
The footer reports how many rows were hidden; process CPU, RSS, and thread
counts always include hidden threads. This is only a display filter: NumPy,
BLAS, plugin pools, and audio/control thread behavior are unchanged. Generic
names do not reliably identify BLAS workers, so other idle unnamed helpers
are filtered too.

`status` reads shared output-callback health and cached metadata; it does not
contact native owners or query PortAudio while rendering. `Audio owner`
identifies the output/mixer PID; `Audio failure` takes precedence over stale
running flags. `Render` distinguishes maximum track-process capacity from the
eligible track count in the last callback. `Track buffering` states added latency.
Each `Track N worker` row shows its PID, failure, and cumulative missing blocks;
these cached worker-health fields refresh approximately every 100 ms.

`Preferred output` shows the selected index/name that a bare `audio start` will
use, or `(system default)`. This is distinct from the actual last-opened output
device; selecting a preference does not open an audio stream.

`Output realtime` and each `Track N realtime` row report the selective scheduling
state and, for live threads, actual versus requested priority, policy, TID, and
any errno. These are cached control-side observations, not a status-time RPC.
An `active` scheduling row is not a guarantee of meeting the audio deadline.

`Callback time` shows mixer/master callback last/peak execution time and the
block deadline, **not native instrument/insert rendering time**. A fast mixer
with missing track blocks is still audible trouble. `Deadline misses` counts
output callbacks exceeding that deadline, not every OS scheduling delay.
`Underruns` counts backend-reported output underflows; zero does not imply zero
track dropouts. `Render errors` counts output processing errors. Output counters
are cumulative across stream restarts; a replacement track starts new worker
counters. The control tick copies native callback counters into shared health
fields; the audio callback does not log or call Python.

If a running output callback stops completing for two seconds (or eight block
periods, whichever is longer), supervision stops the output owner. Native output
control acknowledgments have a five-second deadline. A timeout permanently
fails/silences the owner; queued work cannot later produce an audible success.

A track render stalled for two seconds is terminated locally. Track initialization
and insert lifecycle waits are bounded at 90 seconds; setters/native snapshot
requests at five seconds. Lifecycle work is exempt from the render-stall timer.
The output callback never waits for those deadlines. Track failure leaves other
tracks playing; reload that track to recover. No automatic restart hides lost
plugin state. Children exit when their owning output process dies.

Output-owner death/channel failure produces explicit stopped/error state while
cached diagnostics and client disconnect remain usable. Restart the daemon
after a terminal output-owner failure.

`shutdown` saves before teardown, stops MIDI/sequencing and joins pending
note-off timers, disables Link, then shuts down the output owner and track children.
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
- MIDI input events at `DEBUG`
- MIDI Mix fader/knob events at `DEBUG`; mute/solo actions remain at `INFO`
- Final applied track/master gains at `INFO`, after that gain has been quiet for
  300 ms. Further updates replace the pending summary and restart its quiet
  interval, even across queue drains. Gain changes themselves remain immediate;
  only logging is delayed. Track replacement discards the old track's pending
  summary, and shutdown flushes pending final values without waiting.
- Final applied MIDI Mix knob parameters at `INFO`, using the same 300 ms quiet
  interval independently for each track/parameter. Summaries use the value
  acknowledged by the track worker, not the requested MIDI value. Failed,
  unbound, and retired-track requests do not produce successful applied logs.
  Parameter changes are immediate; only the summary is delayed. Shutdown flushes
  pending final knob values.
