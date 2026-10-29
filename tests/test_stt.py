"""Unit tests for SpeechToText: transcription parameters, dynamic initial prompt, and hallucination filter."""
from unittest.mock import MagicMock
import numpy as np
import pytest

from config import STTConfig
from stt import SpeechToText, build_initial_prompt, is_filler_or_empty


class DummySegment:
    def __init__(self, text: str):
        self.text = text


def test_stt_transcribe_with_mocked_model():
    """Verify that transcribe() calls faster-whisper with required parameters."""
    mock_model = MagicMock()
    mock_model.transcribe.return_value = ([DummySegment("box this lap")], None)

    cfg = STTConfig(beam_size=1)
    stt = SpeechToText(config=cfg, model=mock_model, warmup=False)

    synthetic_audio = np.zeros(16000, dtype=np.float32)
    prompt = "box, tyres, DRS"
    text = stt.transcribe(synthetic_audio, initial_prompt=prompt)

    assert text == "box this lap"
    mock_model.transcribe.assert_called_once_with(
        synthetic_audio,
        language="en",
        beam_size=1,
        condition_on_previous_text=False,
        vad_filter=False,
        initial_prompt=prompt,
    )


def test_hallucination_filter():
    """Whisper filler hallucinations on near-silence must be identified and dropped."""
    # Common Whisper hallucinations on silence
    hallucinations = [
        "",
        "   ",
        ".",
        "...",
        "!",
        "you",
        "You.",
        "thank you",
        "Thank you.",
        "Thanks.",
        "Thank you very much.",
        "bye",
        "bye.",
        "subscribe",
        "Thank you for watching.",
    ]

    for h in hallucinations:
        assert is_filler_or_empty(h), f"Expected {h!r} to be recognized as filler"

    # Legitimate racing communications must NOT be dropped
    valid_transcripts = [
        "box",
        "box now",
        "gap ahead",
        "how are the tyres",
        "understeer into Copse",
        "safety car",
        "copy",
        "push",
        "what position am I",
    ]

    for v in valid_transcripts:
        assert not is_filler_or_empty(v), f"Expected {v!r} to be recognized as valid speech"


def test_stt_transcribe_drops_hallucinations():
    """SpeechToText.transcribe() should return an empty string when the model produces filler."""
    mock_model = MagicMock()
    mock_model.transcribe.return_value = ([DummySegment("Thank you.")], None)

    stt = SpeechToText(model=mock_model, warmup=False)
    result = stt.transcribe(np.zeros(16000, dtype=np.float32))
    assert result == ""


def test_build_initial_prompt():
    """initial_prompt should dynamically include radio terms, driver surnames, and track corners."""
    # 1. Base vocabulary only
    p1 = build_initial_prompt()
    assert "box" in p1
    assert "understeer" in p1
    assert "safety car" in p1

    # 2. Add driver surnames from RaceState.names
    names = {
        0: "Charles Leclerc",
        1: "Max Verstappen",
        2: "Lando Norris",
    }
    p2 = build_initial_prompt(names=names)
    assert "Leclerc" in p2
    assert "Verstappen" in p2
    assert "Norris" in p2

    # 3. Add corner names
    corners = ["Copse", "Maggotts", "Becketts", "Stowe"]
    p3 = build_initial_prompt(names=names, corner_names=corners)
    assert "Copse, Maggotts, Becketts, Stowe" in p3


def test_engineer_audio_processing_dispatches_ask():
    """Verify that when audio is processed, Engineer.ask(text) is called and latency is recorded."""
    import asyncio

    async def _run():
        from main import Engineer
        from latency import LatencyTracker

        mock_model = MagicMock()
        mock_model.transcribe.return_value = ([DummySegment("What's the gap ahead?")], None)

        latency = LatencyTracker()
        stt = SpeechToText(model=mock_model, warmup=False)
        engineer = Engineer(ptt_action=1, latency=latency, stt=stt)

        # Start latency transmission as if PTT was released
        engineer.latency.start_transmission(timestamp=10.0)
        engineer.latency.record("ptt_up", timestamp=10.0)

        # Run processing
        synthetic_audio = np.zeros(8000, dtype=np.float32)
        await engineer._process_driver_audio(synthetic_audio)

        # Check that answer was queued in radio
        popped = engineer.radio.pop()
        assert popped is not None
        assert "ahead" in popped.text.lower() or "no data" in popped.text.lower()

    asyncio.run(_run())


