"""Sampler sub-package -- WAV-backed instrument plugin."""

from core.sampler.plugin import WavSamplerPlugin
from core.sampler.wav import read_wav, resample_linear, adapt_channels, decode_pcm

__all__ = [
    "WavSamplerPlugin",
    "read_wav",
    "resample_linear",
    "adapt_channels",
    "decode_pcm",
]
