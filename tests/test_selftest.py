"""Unit test for the --selftest execution flow."""
import asyncio
from unittest.mock import MagicMock
import numpy as np
import pytest

from audio_player import AudioPlayer
from f1telemetry.deterministic import RadioQueue
from latency import LatencyTracker
from main import Engineer, selftest


class MockTTS:
    async def synth(self, text: str):
        # Return 50ms of dummy audio
        return np.zeros(1102, dtype=np.float32), 22050

    async def warmup(self):
        pass


class MockLLM:
    async def stream(self, system: str, user: str):
        yield "Copy."
        yield " Bias two clicks forward."


def test_selftest_execution():
    """Verify that selftest runs both queries, plays audio, and computes latencies without errors."""
    async def _run():
        from agent import RaceEngineerAgent

        radio = RadioQueue()
        latency = LatencyTracker()
        tts = MockTTS()
        player = AudioPlayer(start_stream=False)

        engineer = Engineer(ptt_action=1, latency=latency, tts=tts, player=player)
        engineer.radio = radio
        engineer.agent = RaceEngineerAgent(llm=MockLLM(), radio=radio, latency=latency)

        # Start speaker worker
        speaker_task = asyncio.create_task(engineer.speaker())

        try:
            await selftest(engineer)
        finally:
            speaker_task.cancel()

        # Check that state was populated
        snap = engineer.state.snapshot()
        assert snap["track"] == "Silverstone"
        assert snap["position"] == 2
        assert snap["car_ahead"]["name"] == "Leclerc"

        # Check latency records were computed
        avgs = latency.rolling_averages()
        assert len(avgs) > 0

    asyncio.run(_run())
