# Bundled VST3 Plugins

This directory holds open-license VST3 plugins for use with vcpi's
`slot <n> vst` and `slot <n> fx` commands.

## Architecture-specific fetch scripts

- `fetch-vsts-amd64`: plugin downloads for Linux amd64/x86_64
- `fetch-vsts-aarch64`: plugin downloads for Linux aarch64 (for example Raspberry Pi)

## Quick Start

```bash
# Linux amd64/x86_64
./vst3/fetch-vsts-amd64

# Linux aarch64 (Raspberry Pi)
./vst3/fetch-vsts-aarch64
```

You can also target groups or individual plugins:

```bash
./vst3/fetch-vsts-amd64 synths
./vst3/fetch-vsts-amd64 dragonfly
./vst3/fetch-vsts-aarch64 effects
./vst3/fetch-vsts-aarch64 surge
```

After fetching, load plugins by name:

```text
vcpi> slot 1 vst Surge XT
vcpi> slot 2 vst OB-Xf
vcpi> slot master fx DragonflyHallReverb
```

## Plugin Catalog

### amd64/x86_64 (`fetch-vsts-amd64`)

Synths:

- Dexed
- Surge XT
- Odin 2
- OB-Xf
- Geonkick
- JC-303
- Firefly Synth 2

Effects:

- Dragonfly Reverb (Hall/Room/Plate/Early)
- Firefly Synth 2 FX

### aarch64 (`fetch-vsts-aarch64`)

Synths:

- Surge XT (OBS nightly package)
- OB-Xf (OBS nightly package)

Effects:

- Dragonfly Reverb (GitHub arm64 release)

## Optional plugdata patch runtime

`slot <n> pd <patch>` requires the instrument `plugdata.vst3` from
[plugdata v0.9.3-2](https://github.com/plugdata-team/plugdata/releases/tag/v0.9.3-2).
Download the release matching the target OS and CPU architecture, then place
`plugdata.vst3` in this directory with its `Contents/` tree intact. Alternatively,
install it in a standard VST3 directory or set `PLUGDATA_VST3_PATH` to the bundle
before starting vcpi. The standalone app and `plugdata-fx.vst3` are not used by
the `pd` slot command.

Neither architecture-specific fetch script installs plugdata. Its internal
0.9.3 patch-state layout is explicitly checked; unsupported layouts are rejected.
See [USAGE.md](../USAGE.md) for patch loading, routing, and external-file
requirements. The repository includes `patches/plugdata-midi-sine.pd`.


## How It Works

- Downloads and extractions are performed into `vst3/`.
- Downloaded archives are cached in `vst3/.cache/` (gitignored).
- `.vst3` bundles are gitignored; run the fetch script after cloning.
- vcpi's VST search path includes this directory automatically.

## Requirements

- `fetch-vsts-amd64`: `curl`, `unzip`, `tar`
- `fetch-vsts-aarch64`: `curl`, `gzip`, `ar`, `tar`
