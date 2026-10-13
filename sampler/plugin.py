"""WavSamplerPlugin -- lightweight sampler with plugin-like API.

Provides the same ``send_midi`` + ``process`` interface that the audio
engine expects so WAV-backed instruments can sit alongside VST3 plugins.
"""

from __future__ import annotations

from pathlib import Path

from core.audio_deps import np
from sampler.wav import read_wav, resample_linear, adapt_channels


class WavSamplerPlugin:
    """Minimal sampler with plugin-like API used by the audio engine."""

    is_instrument = True
    info_type = "Sample"
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

        self._sample = sample.astype(np.float32, order="C")
        self._frames = int(self._sample.shape[1])
        self._voices: list[dict[str, float | int]] = []

        self._output = np.empty((self.output_channels, 0), dtype=np.float32)
        self._block_positions = np.empty(0, dtype=np.float32)
        self._positions = np.empty(0, dtype=np.float32)
        self._valid = np.empty(0, dtype=np.bool_)
        self._idx0 = np.empty(0, dtype=np.int64)
        self._idx1 = np.empty(0, dtype=np.int64)
        self._frac = np.empty(0, dtype=np.float64)
        self._weight = np.empty(0, dtype=np.float64)
        self._left = np.empty(0, dtype=np.float32)
        self._right = np.empty(0, dtype=np.float32)
        self._rendered = np.empty(0, dtype=np.float64)
        self._right_weighted = np.empty(0, dtype=np.float64)

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
        """Render voices; the returned buffer is valid until the next process call.

        Output and interpolation scratch are reused for consecutive blocks with
        the same frame count, including silent blocks.
        """
        del sample_rate

        frames = int(audio.shape[1])
        if self._output.shape[1] != frames:
            self._output = np.empty((self.output_channels, frames), dtype=np.float32)
            self._block_positions = np.arange(frames, dtype=np.float32)
            self._positions = np.empty(frames, dtype=np.float32)
            self._valid = np.empty(frames, dtype=np.bool_)
            self._idx0 = np.empty(frames, dtype=np.int64)
            self._idx1 = np.empty(frames, dtype=np.int64)
            self._frac = np.empty(frames, dtype=np.float64)
            self._weight = np.empty(frames, dtype=np.float64)
            self._left = np.empty(frames, dtype=np.float32)
            self._right = np.empty(frames, dtype=np.float32)
            self._rendered = np.empty(frames, dtype=np.float64)
            self._right_weighted = np.empty(frames, dtype=np.float64)

        out = self._output
        out.fill(0.0)
        if frames <= 0 or self._frames <= 0 or not self._voices:
            return out

        alive_count = 0
        for voice in self._voices:
            start = float(voice["position"])
            rate = float(voice["rate"])
            gain = float(voice["gain"])

            if start >= self._frames:
                continue

            np.multiply(self._block_positions, rate, out=self._positions)
            np.add(self._positions, start, out=self._positions)
            np.less(self._positions, self._frames, out=self._valid)
            sample_count = int(np.count_nonzero(self._valid))
            if sample_count <= 0:
                continue

            positions = self._positions[:sample_count]
            idx0 = self._idx0[:sample_count]
            idx1 = self._idx1[:sample_count]
            frac = self._frac[:sample_count]
            weight = self._weight[:sample_count]
            left = self._left[:sample_count]
            right = self._right[:sample_count]
            rendered = self._rendered[:sample_count]
            right_weighted = self._right_weighted[:sample_count]

            np.floor(positions, out=frac)
            np.copyto(idx0, frac, casting="unsafe")
            np.add(idx0, 1, out=idx1)
            np.minimum(idx1, self._frames - 1, out=idx1)
            np.subtract(positions, idx0, out=frac)
            np.subtract(1.0, frac, out=weight)

            # Contiguous one-channel gathers avoid advanced-indexing copies and
            # np.take's buffered output for a noncontiguous partial stereo block.
            # Float64 interpolation preserves the original int64-index promotion.
            for channel in range(self.output_channels):
                sample_channel = 0 if self._sample.shape[0] == 1 else channel
                np.take(self._sample[sample_channel], idx0, out=left, mode="clip")
                np.take(self._sample[sample_channel], idx1, out=right, mode="clip")
                np.multiply(left, weight, out=rendered)
                np.multiply(right, frac, out=right_weighted)
                np.add(rendered, right_weighted, out=rendered)
                np.multiply(rendered, gain, out=rendered)
                channel_out = out[channel, :sample_count]
                np.add(channel_out, rendered, out=channel_out)

            next_pos = start + (frames * rate)
            if next_pos < self._frames:
                voice["position"] = next_pos
                self._voices[alive_count] = voice
                alive_count += 1

        del self._voices[alive_count:]
        return out
