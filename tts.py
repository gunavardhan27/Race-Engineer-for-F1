"""Text-to-speech engine abstraction and providers.

Supports:
1. PiperTTS (default, local, CPU-only, British voice en_GB-alan-medium)
2. KokoroTTS (optional, local, CPU-only via kokoro-onnx)
3. EdgeTTS (optional, cloud via edge-tts)

All engines expose:
  async synth(text: str) -> tuple[np.ndarray, int]
returning a 1D float32 NumPy array and the audio sample rate.
"""
from __future__ import annotations

import asyncio
import io
import logging
from pathlib import Path
from typing import Optional, Protocol, Tuple

import httpx
import numpy as np

from config import TTSConfig

log = logging.getLogger("engineer.tts")


class TTSProvider(Protocol):
    async def synth(self, text: str) -> Tuple[np.ndarray, int]:
        """Synthesizes text into (audio_float32, sample_rate)."""
        ...

    async def warmup(self) -> None:
        """Warms up the synthesizer on a short test phrase."""
        ...


def ensure_piper_voice(models_dir: Path, voice_name: str) -> Tuple[Path, Path]:
    """Downloads the required piper voice (.onnx and .onnx.json) if not already cached."""
    models_dir.mkdir(parents=True, exist_ok=True)
    onnx_file = models_dir / f"{voice_name}.onnx"
    json_file = models_dir / f"{voice_name}.onnx.json"

    if onnx_file.is_file() and json_file.is_file():
        return onnx_file, json_file

    parts = voice_name.split("-")
    lang_code = parts[0]
    speaker = parts[1] if len(parts) > 1 else "alan"
    quality = parts[2] if len(parts) > 2 else "medium"
    lang_family = lang_code.split("_")[0]

    base_url = f"https://huggingface.co/rhasspy/piper-voices/resolve/main/{lang_family}/{lang_code}/{speaker}/{quality}/{voice_name}"

    for suffix, target_file in [(".onnx.json", json_file), (".onnx", onnx_file)]:
        if target_file.is_file():
            continue
        url = f"{base_url}{suffix}"
        log.info("Downloading Piper voice asset: %s ...", url)
        tmp = target_file.with_suffix(f"{suffix}.tmp")
        try:
            with httpx.stream("GET", url, follow_redirects=True, timeout=60.0) as resp:
                resp.raise_for_status()
                with open(tmp, "wb") as f:
                    for chunk in resp.iter_bytes(chunk_size=65536):
                        f.write(chunk)
            tmp.replace(target_file)
            log.info("Cached %s (%d bytes)", target_file.name, target_file.stat().st_size)
        except Exception as e:
            if tmp.is_file():
                tmp.unlink(missing_ok=True)
            raise RuntimeError(f"Failed to download Piper voice file from {url}: {e}")

    return onnx_file, json_file


class PiperTTS:
    """Local, CPU-only Piper TTS using piper-tts package."""

    def __init__(
        self,
        voice_name: str = "en_GB-alan-medium",
        models_dir: str = "models/piper",
        voice=None,
    ):
        self.voice_name = voice_name
        self.models_dir = Path(models_dir)
        self.voice = voice

    def _ensure_loaded(self) -> None:
        if self.voice is not None:
            return
        from piper import PiperVoice

        onnx_path, json_path = ensure_piper_voice(self.models_dir, self.voice_name)
        self.voice = PiperVoice.load(onnx_path, config_path=json_path, use_cuda=False)
        log.info("PiperTTS loaded model: %s", onnx_path)

    def _synth_sync(self, text: str) -> Tuple[np.ndarray, int]:
        self._ensure_loaded()
        chunks = list(self.voice.synthesize(text))
        if not chunks:
            return np.array([], dtype=np.float32), 22050

        audio = np.concatenate([c.audio_float_array for c in chunks]).astype(np.float32)
        sample_rate = chunks[0].sample_rate
        return audio, sample_rate

    async def synth(self, text: str) -> Tuple[np.ndarray, int]:
        return await asyncio.to_thread(self._synth_sync, text)

    async def warmup(self) -> None:
        try:
            await self.synth("Radio check.")
            log.info("PiperTTS engine warmed up on 'Radio check.'")
        except Exception as e:
            log.warning("PiperTTS warmup warning: %s", e)


class KokoroTTS:
    """Optional local CPU TTS using kokoro-onnx."""

    def __init__(self, voice: str = "af_sarah"):
        self.voice = voice
        self._kokoro = None

    def _ensure_loaded(self):
        if self._kokoro is not None:
            return
        try:
            from kokoro_onnx import Kokoro
            self._kokoro = Kokoro("models/kokoro/kokoro-v0_19.onnx", "models/kokoro/voices.json")
        except ImportError:
            raise RuntimeError("kokoro-onnx is not installed. Install via: pip install kokoro-onnx")

    def _synth_sync(self, text: str) -> Tuple[np.ndarray, int]:
        self._ensure_loaded()
        samples, sample_rate = self._kokoro.create(text, voice=self.voice, speed=1.0, lang="en-us")
        return samples.astype(np.float32), sample_rate

    async def synth(self, text: str) -> Tuple[np.ndarray, int]:
        return await asyncio.to_thread(self._synth_sync, text)

    async def warmup(self) -> None:
        try:
            await self.synth("Radio check.")
            log.info("KokoroTTS engine warmed up")
        except Exception as e:
            log.warning("KokoroTTS warmup warning: %s", e)


class EdgeTTS:
    """Optional cloud TTS using edge-tts (off by default)."""

    def __init__(self, voice: str = "en-GB-RyanNeural"):
        self.voice = voice

    async def synth(self, text: str) -> Tuple[np.ndarray, int]:
        try:
            import edge_tts
        except ImportError:
            raise RuntimeError("edge-tts is not installed. Install via: pip install edge-tts")

        communicate = edge_tts.Communicate(text, self.voice)
        audio_bytes = bytearray()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_bytes.extend(chunk["data"])

        # Decode MP3 bytes into PCM float32 array
        import soundfile as sf
        with io.BytesIO(audio_bytes) as bio:
            data, sample_rate = sf.read(bio, dtype="float32")
        return data, sample_rate

    async def warmup(self) -> None:
        try:
            await self.synth("Radio check.")
            log.info("EdgeTTS engine warmed up")
        except Exception as e:
            log.warning("EdgeTTS warmup warning: %s", e)


def create_tts_engine(config: TTSConfig) -> TTSProvider:
    """Factory creating the configured TTS engine."""
    engine_name = (config.engine or "piper").lower()
    if engine_name == "piper":
        return PiperTTS(voice_name=config.voice)
    elif engine_name == "kokoro":
        return KokoroTTS(voice=config.voice)
    elif engine_name == "edge":
        return EdgeTTS(voice=config.voice)
    else:
        log.warning("Unknown TTS engine '%s'; falling back to PiperTTS", config.engine)
        return PiperTTS(voice_name=config.voice)
