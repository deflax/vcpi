"""WAV file I/O and audio conversion helpers."""

from __future__ import annotations

import wave
from pathlib import Path

from core.deps import np


def decode_pcm(raw: bytes, sample_width: int) -> np.ndarray:
    """Decode PCM bytes into float32 samples in range [-1.0, 1.0]."""
    if sample_width == 1:
        data = np.frombuffer(raw, dtype=np.uint8).astype(np.float32)
        return (data - 128.0) / 128.0

    if sample_width == 2:
        data = np.frombuffer(raw, dtype="<i2").astype(np.float32)
        return data / 32768.0

    if sample_width == 3:
        packed = np.frombuffer(raw, dtype=np.uint8)
        packed = packed.reshape(-1, 3).astype(np.int32)
        data = packed[:, 0] | (packed[:, 1] << 8) | (packed[:, 2] << 16)
        sign_bit = 1 << 23
        data = (data ^ sign_bit) - sign_bit
        return data.astype(np.float32) / 8388608.0

    if sample_width == 4:
        data = np.frombuffer(raw, dtype="<i4").astype(np.float32)
        return data / 2147483648.0

    raise ValueError(f"unsupported WAV sample width: {sample_width} bytes")


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    """Read a PCM WAV file into a (channels, frames) float32 array."""
    with wave.open(str(path), "rb") as handle:
        channels = handle.getnchannels()
        sample_rate = handle.getframerate()
        sample_width = handle.getsampwidth()
        frame_count = handle.getnframes()
        comp_type = handle.getcomptype()

        if comp_type != "NONE":
            raise ValueError(f"unsupported WAV compression: {comp_type}")

        raw = handle.readframes(frame_count)

    if channels <= 0:
        raise ValueError("invalid WAV channel count")
    if not raw:
        raise ValueError("WAV file is empty")

    pcm = decode_pcm(raw, sample_width)
    if pcm.size % channels != 0:
        raise ValueError("WAV frame data is malformed")

    audio = pcm.reshape(-1, channels).T
    return audio.astype(np.float32), int(sample_rate)


def resample_linear(audio: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """Resample (channels, frames) audio with linear interpolation."""
    if src_rate == dst_rate or audio.shape[1] <= 1:
        return audio

    src_frames = audio.shape[1]
    dst_frames = max(1, int(round(src_frames * (dst_rate / src_rate))))

    positions = np.linspace(0, src_frames - 1, num=dst_frames, dtype=np.float64)
    idx0 = np.floor(positions).astype(np.int64)
    idx1 = np.minimum(idx0 + 1, src_frames - 1)
    frac = (positions - idx0).astype(np.float32)

    left = audio[:, idx0]
    right = audio[:, idx1]
    return (left * (1.0 - frac) + right * frac).astype(np.float32)


def adapt_channels(audio: np.ndarray, output_channels: int) -> np.ndarray:
    """Convert channel count to match engine output channels."""
    in_channels = audio.shape[0]
    if in_channels == output_channels:
        return audio

    if in_channels == 1 and output_channels == 2:
        return np.vstack([audio, audio])

    if in_channels > output_channels:
        return audio[:output_channels, :]

    extra = output_channels - in_channels
    tails = [audio[-1:, :]] * extra
    return np.vstack([audio, *tails])
