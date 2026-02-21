# Built-in Sample Packs

These WAV files are intended for the `slot <n> wav` sampler command.

`slot <n> wav <pack> <sample>` resolves to `samples/<pack>/<sample>.wav`.

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
vcpi> slot 2 wav piano c4-soft
vcpi> slot 3 wav organ c4-drawbar
vcpi> slot 4 wav strings c4-ensemble
vcpi> slot 5 wav synth-pads c4-warm
vcpi> slot 6 wav synth-leads c4-mono-saw
```
