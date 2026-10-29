"""Unit tests for TTS and playback interruption logic."""
import asyncio
from unittest.mock import MagicMock
import numpy as np
import pytest

from audio_player import AudioPlayer
from config import TTSConfig
from f1telemetry.deterministic import Callout, RadioQueue
from latency import LatencyTracker
from main import Engineer
from tts import PiperTTS, create_tts_engine


class FakeTTS:
    def __init__(self, sample_rate: int = 22050):
        self.sample_rate = sample_rate
        self.synth_calls = []

    async def synth(self, text: str) -> tuple[np.ndarray, int]:
        self.synth_calls.append(text)
        # 100 ms of dummy audio
        samples = int(self.sample_rate * 0.1)
        return np.full(samples, 0.1, dtype=np.float32), self.sample_rate

    async def warmup(self) -> None:
        pass


class FakeStreamPlayer(AudioPlayer):
    """AudioPlayer with mocked stream for fast unit testing."""

    def __init__(self):
        super().__init__(start_stream=False)
        self.played_audios = []

    def play(self, audio: np.ndarray, sample_rate: int, is_interrupted=None) -> bool:
        self.played_audios.append(audio)
        interrupt_fn = is_interrupted or (lambda: False)
        # Simulate 3 blocks
        block_size = max(1, len(audio) // 3)
        for i in range(0, len(audio), block_size):
            if interrupt_fn():
                return False
        return True


def test_playback_interruption_logic():
    """Verify that when interrupt_check returns True, play() terminates immediately and returns False."""
    player = AudioPlayer(start_stream=False)

    call_count = 0

    def mock_interrupt():
        nonlocal call_count
        call_count += 1
        return call_count >= 2   # Interrupt on second check

    audio = np.zeros(22050, dtype=np.float32)   # 1 second of audio
    # Play using headless simulation with interruption
    completed = player.play(audio, 22050, is_interrupted=mock_interrupt)
    assert not completed
    assert call_count >= 2


def test_playback_completes_without_interruption():
    """Verify that when interrupt_check stays False, play() finishes and returns True."""
    player = AudioPlayer(start_stream=False)
    # 80 ms of audio (2 blocks)
    audio = np.zeros(int(22050 * 0.08), dtype=np.float32)
    completed = player.play(audio, 22050, is_interrupted=lambda: False)
    assert completed


def test_speaker_queue_sequential_non_overlapping():
    """Verify that sentences in RadioQueue are processed sequentially without overlapping."""
    async def _run():
        radio = RadioQueue()
        latency = LatencyTracker()
        tts = FakeTTS()
        player = FakeStreamPlayer()

        engineer = Engineer(ptt_action=1, latency=latency, tts=tts, player=player)
        engineer.radio = radio

        import time
        # Push sentence 1 and sentence 2 to RadioQueue
        radio.push(Callout(-1, time.monotonic(), "Sentence one."))
        radio.push(Callout(-1, time.monotonic(), "Sentence two."))

        # Start speaker loop as task
        speaker_task = asyncio.create_task(engineer.speaker())

        # Allow speaker loop to process both items
        for _ in range(15):
            await asyncio.sleep(0.06)
            if len(player.played_audios) >= 2:
                break

        speaker_task.cancel()

        # Both sentences must have been synthesized and played in order
        assert tts.synth_calls == ["Sentence one.", "Sentence two."]
        assert len(player.played_audios) == 2

    asyncio.run(_run())


def test_speaker_interrupted_by_ptt():
    """If driver presses PTT while speaker is active, playback stops immediately."""
    async def _run():
        import time
        radio = RadioQueue()
        latency = LatencyTracker()
        tts = FakeTTS()

        interrupted = False

        class SlowInterruptiblePlayer(AudioPlayer):
            def __init__(self):
                super().__init__(start_stream=False)

            def play(self, audio, sample_rate, is_interrupted=None):
                nonlocal interrupted
                fn = is_interrupted or (lambda: False)
                for _ in range(20):
                    if fn():
                        interrupted = True
                        return False
                    time.sleep(0.02)
                return True

        player = SlowInterruptiblePlayer()
        engineer = Engineer(ptt_action=1, latency=latency, tts=tts, player=player)
        engineer.radio = radio

        radio.push(Callout(-1, time.monotonic(), "Long reply from engineer."))
        speaker_task = asyncio.create_task(engineer.speaker())

        # Wait until playback starts
        await asyncio.sleep(0.04)
        # Driver presses PTT while speaker is active
        engineer.on_ptt(True)

        await asyncio.sleep(0.08)
        speaker_task.cancel()

        assert interrupted, "Expected player to be interrupted by driver pressing PTT"


    asyncio.run(_run())


def test_tts_factory():
    """create_tts_engine must return PiperTTS for default configuration."""
    cfg = TTSConfig(engine="piper", voice="en_GB-alan-medium")
    engine = create_tts_engine(cfg)
    assert isinstance(engine, PiperTTS)
    assert engine.voice_name == "en_GB-alan-medium"
