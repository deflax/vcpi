# Bundled VST3 Plugins

This directory holds open-license VST3 plugins for use with vcpi's
`load vst` and `load fx` commands.

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
vcpi> load vst 1 Surge XT
vcpi> load vst 2 OB-Xf
vcpi> load fx DragonflyHallReverb master
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

## How It Works

- Downloads and extractions are performed into `vst3/`.
- Downloaded archives are cached in `vst3/.cache/` (gitignored).
- `.vst3` bundles are gitignored; run the fetch script after cloning.
- vcpi's VST search path includes this directory automatically.

## Requirements

- `fetch-vsts-amd64`: `curl`, `unzip`, `tar`
- `fetch-vsts-aarch64`: `curl`, `gzip`, `ar`, `tar`
