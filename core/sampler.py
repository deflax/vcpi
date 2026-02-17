"""Simple WAV-backed sampler instrument.

This module provides a lightweight plugin-like object with the same
``send_midi`` + ``process`` interface expected by the audio engine.
"""

from __future__ import annotations

import wave
from pathlib import Path

from core.deps import np


def _decode_pcm(raw: bytes, sample_width: int) -> np.ndarray:
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


def _read_wav(path: Path) -> tuple[np.ndarray, int]:
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

    pcm = _decode_pcm(raw, sample_width)
    if pcm.size % channels != 0:
        raise ValueError("WAV frame data is malformed")

    audio = pcm.reshape(-1, channels).T
    return audio.astype(np.float32), int(sample_rate)


def _resample_linear(audio: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
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


def _adapt_channels(audio: np.ndarray, output_channels: int) -> np.ndarray:
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


class WavSamplerPlugin:
    """Minimal sampler with plugin-like API used by the audio engine."""

    is_instrument = True
    parameters: dict[str, object] = {}

    def __init__(
        self,
        path: str,
        sample: np.ndarray,
        output_channels: int,
        root_note: int = 60,
        max_voices: int = 32,
    ):
        self.path_to_plugin_file = path
        self.output_channels = output_channels
        self.root_note = int(root_note)
        self.max_voices = max(1, int(max_voices))

        self._sample = sample.astype(np.float32)
        self._frames = int(self._sample.shape[1])
        self._voices: list[dict[str, float | int]] = []

    @classmethod
    def from_file(
        cls,
        wav_path: str,
        target_sample_rate: int,
        output_channels: int,
        root_note: int = 60,
        max_voices: int = 32,
    ) -> "WavSamplerPlugin":
        path = Path(wav_path).expanduser()
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(f"WAV not found: {path}")

        audio, src_rate = _read_wav(path)
        audio = _resample_linear(audio, src_rate, target_sample_rate)
        audio = _adapt_channels(audio, output_channels)

        return cls(
            path=str(path),
            sample=audio,
            output_channels=output_channels,
            root_note=root_note,
            max_voices=max_voices,
        )

    def send_midi(self, msg):
        """Handle note_on messages by spawning a one-shot sample voice."""
        msg_type = getattr(msg, "type", "")
        if msg_type != "note_on":
            return

        velocity = int(getattr(msg, "velocity", 0))
        if velocity <= 0:
            return

        note = int(getattr(msg, "note", self.root_note))
        semitones = note - self.root_note
        rate = float(2.0 ** (semitones / 12.0))

        if len(self._voices) >= self.max_voices:
            self._voices.pop(0)

        self._voices.append({
            "position": 0.0,
            "rate": rate,
            "gain": max(0.0, min(1.0, velocity / 127.0)),
            "note": note,
        })

    def process(self, audio: np.ndarray, sample_rate: int) -> np.ndarray:
        """Render active voices into an output block (channels, frames)."""
        del sample_rate

        frames = int(audio.shape[1])
        out = np.zeros((self.output_channels, frames), dtype=np.float32)

        if frames <= 0 or self._frames <= 0 or not self._voices:
            return out

        block_positions = np.arange(frames, dtype=np.float32)
        alive: list[dict[str, float | int]] = []

        for voice in self._voices:
            start = float(voice["position"])
            rate = float(voice["rate"])
            gain = float(voice["gain"])

            if start >= self._frames:
                continue

            positions = start + (block_positions * rate)
            valid = positions < self._frames
            sample_count = int(valid.sum())
            if sample_count <= 0:
                continue

            pos = positions[:sample_count]
            idx0 = np.floor(pos).astype(np.int64)
            idx1 = np.minimum(idx0 + 1, self._frames - 1)
            frac = pos - idx0

            left = self._sample[:, idx0]
            right = self._sample[:, idx1]
            rendered = left * (1.0 - frac) + right * frac

            out[:, :sample_count] += rendered * gain

            next_pos = start + (frames * rate)
            if next_pos < self._frames:
                voice["position"] = next_pos
                alive.append(voice)

        self._voices = alive
        return out
