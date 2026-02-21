# Built-in Sample Packs

These WAV files are intended for the `load wav` sampler command.

`load wav <slot> <pack> <sample>` resolves to `samples/<pack>/<sample>.wav`.

Available packs:

- `808` - TR-808 drum one-shots
- `909` - TR-909 drum one-shots
- `piano` - procedural piano one-shots
- `organ` - procedural organ one-shots
- `strings` - procedural strings one-shots
- `synth-pads` - procedural synth pad one-shots
- `synth-leads` - procedural synth lead one-shots

Examples:

```text
vcpi> load wav 2 piano c4-soft
vcpi> load wav 3 organ c4-drawbar
vcpi> load wav 4 strings c4-ensemble
vcpi> load wav 5 synth-pads c4-warm
vcpi> load wav 6 synth-leads c4-mono-saw
```
