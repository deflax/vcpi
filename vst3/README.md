# Bundled VST3 Plugins

This directory holds open-license VST3 plugins for use with vcpi's
`load vst` and `load fx` commands.

## Quick Start

```bash
./vst3/fetch-vsts              # download all plugins
./vst3/fetch-vsts synths       # download synths only
./vst3/fetch-vsts effects      # download effects only
./vst3/fetch-vsts dexed        # download only Dexed
./vst3/fetch-vsts fireflyfx    # download only Firefly Synth 2 FX
```

After fetching, load them by name:

```text
vcpi> load vst 1 Dexed
vcpi> load vst 2 Surge XT
vcpi> load vst 3 Odin2
vcpi> load vst 4 OB-Xf
vcpi> load vst 5 jc303
vcpi> load fx DragonflyHallReverb master
```

## Included Plugins

### Synths

| Plugin | Version | Type | License | Upstream |
|---|---|---|---|---|
| Dexed | 1.0.1 | DX7 FM synth | GPL-3.0 | https://github.com/asb2m10/dexed |
| Surge XT | 1.3.4 | Hybrid synth + FX | GPL-3.0 | https://github.com/surge-synthesizer/surge |
| Odin 2 | 2.4.1 | 24-voice polyphonic synth | GPL-3.0 | https://github.com/TheWaveWarden/odin2 |
| OB-Xf | Nightly | Oberheim OB-X polysynth | GPL-3.0 | https://github.com/surge-synthesizer/OB-Xf |
| Geonkick | 3.7.0 | Percussion synth | GPL-3.0 | https://github.com/quamplex/geonkick |
| JC-303 | 0.12.3 | TB-303 acid bass | GPL-3.0 | https://github.com/midilab/jc303 |
| Firefly Synth 2 | 2.1.0 | Semi-modular synth | GPL-3.0 | https://github.com/sjoerdvankreel/firefly-synth-2 |

### Effects

| Plugin | Version | Type | License | Upstream |
|---|---|---|---|---|
| Dragonfly Reverb | 3.2.10 | Hall/Room/Plate/Early reverbs | GPL-3.0 | https://github.com/michaelwillis/dragonfly-reverb |
| Firefly Synth 2 FX | 2.1.0 | FX processor | GPL-3.0 | https://github.com/sjoerdvankreel/firefly-synth-2 |

## How It Works

- `fetch-vsts` downloads official Linux x86_64 release archives from GitHub,
  extracts the `.vst3` bundles, and places them here.
- Downloaded archives are cached in `vst3/.cache/` (gitignored).
- The `.vst3` bundles themselves are gitignored -- run `fetch-vsts` after
  cloning to populate them.
- vcpi's VST search path includes this directory automatically.

## Requirements

`fetch-vsts` needs `curl`, `unzip`, and `tar` (standard on most Linux systems).
