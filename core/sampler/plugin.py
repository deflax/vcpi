"""WavSamplerPlugin -- lightweight sampler with plugin-like API.

Provides the same ``send_midi`` + ``process`` interface that the audio
engine expects so WAV-backed instruments can sit alongside VST3 plugins.
"""

from __future__ import annotations

from pathlib import Path

from core.deps import np
from core.sampler.wav import read_wav, resample_linear, adapt_channels


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

        audio, src_rate = read_wav(path)
        audio = resample_linear(audio, src_rate, target_sample_rate)
        audio = adapt_channels(audio, output_channels)

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
