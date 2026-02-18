# Bundled VST3 Plugins

This directory holds open-license VST3 plugins for use with vcpi's
`load vst` and `load fx` commands.

## Quick Start

```bash
./vst3/fetch-vsts          # download all plugins
./vst3/fetch-vsts dexed    # download only Dexed
```

After fetching, load them by name:

```text
vcpi> load vst 1 Dexed
vcpi> load vst 2 Surge XT
vcpi> load vst 3 Odin2
vcpi> load fx Surge XT Effects 1
```

## Included Plugins

| Plugin | Version | Type | License | Upstream |
|---|---|---|---|---|
| Dexed | 1.0.1 | FM synth (DX7) | GPL-3.0 | https://github.com/asb2m10/dexed |
| Surge XT | 1.3.4 | Hybrid synth + FX | GPL-3.0 | https://github.com/surge-synthesizer/surge |
| Odin 2 | 2.4.1 | Polyphonic synth | GPL-3.0 | https://github.com/TheWaveWarden/odin2 |

## How It Works

- `fetch-vsts` downloads official Linux x86_64 release archives from GitHub,
  extracts the `.vst3` bundles, and places them here.
- Downloaded archives are cached in `vst3/.cache/` (gitignored).
- The `.vst3` bundles themselves are gitignored -- run `fetch-vsts` after
  cloning to populate them.
- vcpi's VST search path includes this directory automatically.

## Requirements

`fetch-vsts` needs `curl`, `unzip`, and `tar` (standard on most Linux systems).
