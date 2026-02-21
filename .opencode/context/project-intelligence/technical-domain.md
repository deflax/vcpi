<!-- Context: project-intelligence/technical | Priority: critical | Version: 1.0 | Updated: 2026-02-21 -->

# Technical Domain

**Purpose**: Tech stack, architecture, and development patterns for vcpi.
**Last Updated**: 2026-02-21

## Quick Reference
**Update Triggers**: Tech stack changes | New patterns | Architecture decisions
**Audience**: Developers, AI agents

## Primary Stack
| Layer | Technology | Version | Rationale |
|-------|-----------|---------|-----------|
| Language | Python 3 | 3.10+ | `from __future__ import annotations`, `int \| str` union syntax |
| Audio I/O | sounddevice | >=0.5.0 | Real-time callback-based audio output |
| VST3 Host | pedalboard | >=0.9.0 | Spotify's VST3/AU plugin hosting library |
| MIDI | python-rtmidi + mido | >=1.5.0 / >=1.3.0 | Hardware MIDI I/O + message construction |
| Tempo Sync | aalink | >=0.1.0 | Ableton Link for multi-device BPM sync |
| Numerics | numpy | >=1.24.0 | Audio buffer manipulation |
| IPC | Unix domain sockets | stdlib | Headless server ↔ CLI client protocol |
| CLI | cmd.Cmd | stdlib | Interactive command-line with tab completion |
| Persistence | JSON files | stdlib | Session save/restore |

## Architecture
- **Client/Server**: `vcsrv` (daemon) ↔ `vcli` (client) over Unix socket
- **Central coordinator**: `VcpiCore` owns all subsystems (engine, link, MIDI, sequencer)
- **Real-time audio**: `AudioEngine` callback renders slots in parallel via `ThreadPoolExecutor`
- **8 instrument slots**: VST3, WAV sampler, or VCV/Cardinal per slot
- **MIDI routing**: channel-based (`midi link <ch> <slot>`), any number of inputs

## Code Patterns

### CLI Command (cmd.Cmd do_* method)
```python
def do_slot(self, arg):
    """Load/manage instrument slots: slot <n> vst|wav|vcv|fx|clear ..."""
    parts = arg.split()
    slot_index = _slot_to_internal(int(parts[0]))  # 1-based → 0-based
    subcommand = parts[1].lower()
    # dispatch by subcommand...
```

### Server Protocol (line-oriented text over Unix socket)
```
client → server:  one command per line (UTF-8)
server → client:  output lines terminated by \x00\n sentinel
```
Commands execute on main thread via queue; reader threads handle I/O.

### Data Model (dataclass)
```python
@dataclass
class InstrumentSlot:
    name: str
    path: str
    plugin: object          # duck-typed pedalboard plugin
    effects: list = field(default_factory=list)
    gain: float = 0.8
    source_type: str = "plugin"  # plugin | wav | vcv
```

### Module Pattern
- **Class-per-module**: `VcpiCore` in `host.py`, `AudioEngine` in `engine.py`
- **Controllers**: hardware-specific classes under `controllers/`
- **Duck-typed plugins**: `WavSamplerPlugin` matches pedalboard's `.process()` interface
- **Pure render functions**: `graph/` modules export `render_*()` functions
- **Lazy deps**: `core/deps.py` with `HAS_*` flags for optional imports

## Naming Conventions
| Type | Convention | Example |
|------|-----------|---------|
| Files | snake_case | `signal_flow.py`, `midi_input.py` |
| Classes | PascalCase | `VcpiCore`, `AudioEngine`, `InstrumentSlot` |
| Functions | snake_case | `load_instrument()`, `render_signal_flow()` |
| Constants | UPPER_SNAKE | `NUM_SLOTS`, `DEFAULT_SOCK_PATH` |
| Private | _leading_underscore | `_warmup_plugin()`, `_drain_commands()` |
| CLI commands | do_\<verb\> | `do_slot`, `do_midi`, `do_audio` |
| Packages | lowercase | `core/`, `sampler/`, `graph/`, `controllers/` |

## Code Standards
1. `from __future__ import annotations` in all core modules
2. Lazy optional deps via `core/deps.py` with `HAS_*` boolean flags
3. `logging.getLogger(__name__)` per module; bracketed prefixes: `[INST]`, `[FX]`, `[Link]`
4. `@dataclass` with `field(default_factory=...)` for mutable defaults
5. Type hints: `Optional[str]`, `int | str` unions, `list[...]` generics
6. Concise docstrings at module and method level
7. 1-based user-facing / 0-based internal (slots 1-8, channels 1-16)
8. Thread safety: GIL-atomic slot assignment, locks where needed, `ThreadPoolExecutor`
9. `pathlib.Path` throughout (no `os.path`)
10. No external test framework — no `tests/` directory

## Security Requirements
1. Unix socket permissions: `chmod 0o770` (group-accessible, not world-readable)
2. Input validation: slot bounds, MIDI channel range, path existence checks
3. Path safety: `Path.expanduser()` + candidate resolution, no arbitrary exec
4. Graceful error handling: `try/except` around plugin/MIDI/audio operations
5. No network exposure: Unix domain sockets only, no TCP/HTTP
6. No secrets/credentials: local-only tool, no API keys or auth

## Codebase References
| Context | File | Description |
|---------|------|-------------|
| Core orchestrator | `core/host.py` | `VcpiCore` — central coordinator |
| Audio engine | `core/engine.py` | `AudioEngine` — real-time callback + parallel render |
| CLI interface | `core/cli.py` | `HostCLI(cmd.Cmd)` — interactive commands |
| Server | `core/server.py` | `VcpiServer` — Unix socket daemon |
| Client | `core/client.py` | CLI client connecting to daemon |
| Data models | `core/models.py` | `InstrumentSlot` dataclass |
| Dependencies | `core/deps.py` | Lazy imports with `HAS_*` flags |
| MIDI | `core/midi.py` | Port listing utilities |
| Sampler | `sampler/plugin.py` | `WavSamplerPlugin` — duck-typed plugin |
| Controllers | `controllers/` | `MidiInputController`, `MidiMixController` |
| Renderers | `graph/` | `render_signal_flow()`, `render_knobs()`, etc. |
| Config | `requirements.txt` | Python dependencies |
| Entry point | `main.py` | Top-level entry → `core/main.py` |

## Related Files
- Business Domain: *(not yet created)*
- Decisions Log: *(not yet created)*
