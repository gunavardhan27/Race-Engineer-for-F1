"""Speech-to-text with faster-whisper on CPU.

Loads faster-whisper (default "small.en", compute_type="int8"), runs warm-up on silence,
biases vocabulary dynamically with radio terms, driver surnames and corner names,
and filters out known Whisper hallucinations on silence.
"""
from __future__ import annotations

import io
import logging
import os
import re
from typing import Any, Optional

import httpx
import numpy as np

from config import STTConfig

log = logging.getLogger("engineer.stt")

RADIO_VOCABULARY = (
    "box, tyres, understeer, oversteer, DRS, ERS, front wing, brake bias, diff, "
    "lift and coast, push, copy, safety car, VSC, softs, mediums, hards, inters, wets"
)

FILLER_PHRASES = {
    "",
    "you",
    "thank you",
    "thanks",
    "thank you very much",
    "bye",
    "goodbye",
    "subscribe",
    "watching",
    "the end",
    "thank you for watching",
    "subtitles by",
}


def is_filler_or_empty(text: str) -> bool:
    """Returns True if the text is empty, punctuation-only, or a known Whisper filler hallucination."""
    if not text:
        return True
    # Strip punctuation, normalize whitespace, lowercase
    cleaned = re.sub(r"[^\w\s]", "", text).strip().lower()
    return cleaned in FILLER_PHRASES


def build_initial_prompt(
    names: Optional[dict[int, str]] = None,
    corner_names: Optional[list[str]] = None,
) -> str:
    """Builds a dynamic initial_prompt string to bias Whisper recognition toward F1 terminology."""
    parts = [RADIO_VOCABULARY]

    if names:
        surnames = []
        for name in names.values():
            if name:
                tokens = name.strip().split()
                if tokens:
                    surnames.append(tokens[-1].title())
        if surnames:
            parts.append(", ".join(sorted(set(surnames))))

    if corner_names:
        parts.append(", ".join(corner_names))

    return ". ".join(parts)


class SpeechToText:
    def __init__(
        self,
        config: Optional[STTConfig] = None,
        model=None,
        warmup: bool = True,
    ):
        self.config = config or STTConfig()
        if model is not None:
            self.model = model
        else:
            self.model = self._load_model()
            if warmup:
                self.warmup()

    def _load_model(self):
        from faster_whisper import WhisperModel
        log.info(
            "Loading faster-whisper model '%s' (device=%s, compute_type=%s)...",
            self.config.model,
            self.config.device,
            self.config.compute_type,
        )
        return WhisperModel(
            self.config.model,
            device=self.config.device,
            compute_type=self.config.compute_type,
        )

    def warmup(self) -> None:
        """Runs one warm-up transcription on silence to avoid latency on the first transmission."""
        try:
            silence = np.zeros(16000, dtype=np.float32)
            self.transcribe(silence)
            log.info("STT engine warmed up on silence")
        except Exception as e:
            log.warning("STT warmup warning: %s", e)

    def transcribe(self, audio: np.ndarray, initial_prompt: str = "") -> str:
        """Transcribes a 1D float32 audio array and applies the hallucination filter.
        Returns cleaned text, or an empty string if silence or filler."""
        if audio is None or len(audio) == 0:
            return ""

        if audio.dtype != np.float32:
            audio = audio.astype(np.float32)

        prompt = initial_prompt if initial_prompt else None
        segments, _ = self.model.transcribe(
            audio,
            language="en",
            beam_size=self.config.beam_size,
            condition_on_previous_text=False,
            vad_filter=False,
            initial_prompt=prompt,
        )

        text = "".join(segment.text for segment in segments).strip()
        if is_filler_or_empty(text):
            log.debug("Filtered out hallucinated filler text: %r", text)
            return ""

        return text


class GroqWhisperSTT:
    """Optional cloud STT using Groq Whisper with UsageGuard and local faster-whisper fallback."""

    def __init__(
        self,
        model: str = "whisper-large-v3",
        api_key: Optional[str] = None,
        usage_guard: Optional[Any] = None,
        fallback_engine: Optional[SpeechToText] = None,
    ):
        self.model = model
        self.api_key = api_key or os.getenv("GROQ_API_KEY", "")
        self.usage_guard = usage_guard
        self.fallback_engine = fallback_engine
        self.base_url = "https://api.groq.com/openai/v1/audio/transcriptions"

    def transcribe(self, audio: np.ndarray, initial_prompt: str = "") -> str:
        if audio is None or len(audio) == 0:
            return ""

        dur_s = float(len(audio)) / 16000.0

        # 1. Check UsageGuard
        if self.usage_guard:
            allowed, reason = self.usage_guard.check_allowed("groq_stt", est_audio_seconds=dur_s, model=self.model)
            if not allowed:
                log.warning("GroqWhisperSTT: Denied by UsageGuard (%s); falling back to local STT", reason)
                if self.fallback_engine:
                    return self.fallback_engine.transcribe(audio, initial_prompt)
                return ""

        # 2. Check API key
        if not self.api_key:
            log.warning("GroqWhisperSTT: GROQ_API_KEY not set; falling back to local STT")
            if self.fallback_engine:
                return self.fallback_engine.transcribe(audio, initial_prompt)
            return ""

        # 3. Call Groq audio transcriptions API
        try:
            import soundfile as sf
            bio = io.BytesIO()
            sf.write(bio, audio.astype(np.float32), 16000, format="WAV")
            bio.seek(0)
            wav_bytes = bio.read()

            files = {"file": ("audio.wav", wav_bytes, "audio/wav")}
            data = {"model": self.model, "language": "en"}
            if initial_prompt:
                data["prompt"] = initial_prompt

            headers = {"Authorization": f"Bearer {self.api_key}"}

            with httpx.Client(timeout=10.0) as client:
                resp = client.post(self.base_url, headers=headers, data=data, files=files)

            if resp.status_code == 429:
                if self.usage_guard:
                    self.usage_guard.record_usage("groq_stt", status_code=429, headers=dict(resp.headers), model=self.model)
                log.warning("GroqWhisperSTT got HTTP 429; falling back to local STT")
                if self.fallback_engine:
                    return self.fallback_engine.transcribe(audio, initial_prompt)
                return ""

            if resp.status_code != 200:
                log.warning("GroqWhisperSTT API error %d; falling back to local STT", resp.status_code)
                if self.fallback_engine:
                    return self.fallback_engine.transcribe(audio, initial_prompt)
                return ""

            # Record usage on success
            if self.usage_guard:
                self.usage_guard.record_usage(
                    "groq_stt",
                    requests=1,
                    audio_seconds=dur_s,
                    headers=dict(resp.headers),
                    status_code=200,
                    model=self.model,
                )

            res_json = resp.json()
            text = res_json.get("text", "").strip()
            if is_filler_or_empty(text):
                return ""
            return text

        except Exception as e:
            log.warning("GroqWhisperSTT request failed (%s); falling back to local STT", e)
            if self.fallback_engine:
                return self.fallback_engine.transcribe(audio, initial_prompt)
            return ""

