"""Unit tests for RaceEngineerAgent: prompt formatting, sentence streaming, speech cleaning, number guardrail, and PTT cancellation."""
import asyncio
import json
import logging
from unittest.mock import MagicMock
import pytest

from agent import RaceEngineerAgent, build_prompt, check_number_guardrail, clean_for_speech, clean_snapshot
from f1telemetry.deterministic import RadioQueue
from latency import LatencyTracker
from main import Engineer


class FakeLLMClient:
    def __init__(self, tokens: list[str] = None, delay: float = 0.0):
        self.tokens = tokens if tokens is not None else ["Copy.", " Bias two clicks forward."]
        self.delay = delay

    async def stream(self, system: str, user: str):
        if self.delay > 0:
            await asyncio.sleep(self.delay)
        for tok in self.tokens:
            yield tok


def test_speech_cleaning():
    """Verify speech cleaner removes markdown, brackets, emojis, normalizes seconds, and keeps P-positions."""
    raw = "**Copy.** Gap to P3 is 0.8s! [Plan B] 👍"
    cleaned = clean_for_speech(raw)
    assert "**" not in cleaned
    assert "[" not in cleaned
    assert "]" not in cleaned
    assert "👍" not in cleaned
    assert "0.8" in cleaned
    assert "0.8s" not in cleaned
    assert "P3" in cleaned


def test_clean_snapshot_and_prompt_building():
    """Verify that None fields are removed and compact JSON is injected into user message."""
    snap = {
        "track": "Silverstone",
        "lap": 6,
        "empty_field": None,
        "fuel": {"kg": 40.2, "empty_sub": None},
    }
    clean = clean_snapshot(snap)
    assert "empty_field" not in clean
    assert "empty_sub" not in clean["fuel"]
    assert clean["fuel"]["kg"] == 40.2

    system, user = build_prompt("I'm understeering at Copse", snap, knowledge_text="Copse: fast right hander")
    assert "You are the driver's race engineer" in system
    assert "Silverstone" in user
    assert "Copse: fast right hander" in user
    assert "DRIVER: I'm understeering at Copse" in user


def test_clean_snapshot_strategy_intent():
    """Verify that strategy and pit questions retain pit_window, fuel, strategy, gaps, and tyres."""
    full_snap = {
        "lap": 15,
        "position": 2,
        "pit_window": {"ideal_lap": 18, "latest_lap": 23},
        "strategy": {
            "recommended_plan": "Plan A (1-Stop)",
            "recommended_stops": ["Lap 18 -> Hard"],
            "plan_b": "Plan B (2-Stop)",
            "undercut_opportunity": "Box this lap for the undercut on Norris.",
        },
        "tyres": {"compound": "Medium", "age_laps": 15, "wear_pct_per_lap": 2.1},
        "fuel": {"kg": 35.0, "laps_delta": 0.8},
        "car_ahead": {"name": "Norris", "gap_s": 1.2, "tyre_compound": "Medium"},
        "car_behind": {"name": "Russell", "gap_s": 3.4, "tyre_compound": "Hard"},
        "weather": {"now": "Clear", "next_rain": None},
        "safety_car": None,
        "unrelated_setup": {"wing": 22},
    }
    cleaned = clean_snapshot(full_snap, question="Strategy check. Should we box or change plan?")
    assert "strategy" in cleaned
    assert cleaned["strategy"]["recommended_plan"] == "Plan A (1-Stop)"
    assert "pit_window" in cleaned
    assert cleaned["pit_window"]["ideal_lap"] == 18
    assert "car_ahead" in cleaned
    assert "fuel" in cleaned
    assert "unrelated_setup" not in cleaned


def test_streaming_sentence_chunks_push_to_radio():
    """Tokens must be accumulated into sentences on .!? and pushed to RadioQueue at priority -1."""
    async def _run():
        radio = RadioQueue()
        latency = LatencyTracker()
        tokens = ["Copy", ".", " ", "Brake", " bias", " two", " forward", "."]
        fake_llm = FakeLLMClient(tokens)

        agent = RaceEngineerAgent(llm=fake_llm, radio=radio, latency=latency)
        snap = {"lap": 6}

        reply = await agent.answer("understeer", snap)
        assert "Copy" in reply
        assert "Brake bias two forward" in reply

        # Verify radio queue received 2 items with priority -1
        first_callout = radio.pop()
        assert first_callout is not None
        assert first_callout.priority == -1
        assert "Copy." in first_callout.text

        second_callout = radio.pop()
        assert second_callout is not None
        assert second_callout.priority == -1
        assert "Brake bias two forward." in second_callout.text

    asyncio.run(_run())


def test_hard_cap_two_sentences():
    """Agent must cap responses at 2 sentences even if the LLM generates more."""
    async def _run():
        radio = RadioQueue()
        tokens = [
            "Sentence one.",
            " Sentence two.",
            " Sentence three.",
            " Sentence four.",
        ]
        fake_llm = FakeLLMClient(tokens)
        agent = RaceEngineerAgent(llm=fake_llm, radio=radio)

        await agent.answer("help", {"lap": 1})

        # RadioQueue should have exactly 2 sentences
        c1 = radio.pop()
        c2 = radio.pop()
        c3 = radio.pop()

        assert c1 is not None and "Sentence one." in c1.text
        assert c2 is not None and "Sentence two." in c2.text
        assert c3 is None   # 3rd sentence must not be queued

    asyncio.run(_run())


def test_number_guardrail_allows_known_numbers_and_setup_clicks(caplog):
    """Numbers appearing in snapshot or question, plus setup clicks (1-3), must not warn."""
    snap_json = '{"lap": 6, "gap_s": 1.2}'
    question = "Gap to P3?"

    with caplog.at_level(logging.WARNING):
        # 1.2 is in snapshot, 2 is in allowed setup steps (1-3), 3 is in question
        hallucinated = check_number_guardrail("Gap is 1.2 to P3. Bias two clicks forward.", snap_json, question)
        assert len(hallucinated) == 0
        assert not any("Number guardrail warning" in m for m in caplog.messages)


def test_number_guardrail_warns_on_hallucinated_numbers(caplog):
    """Numbers not found in snapshot or question must log a guardrail warning."""
    snap_json = '{"lap": 6}'
    question = "Tyres?"

    with caplog.at_level(logging.WARNING):
        # 88 and 45 are not in snapshot or question, and not in {1, 2, 3}
        hallucinated = check_number_guardrail("Tyre wear is 88 percent with 45 laps left.", snap_json, question)
        assert "88" in hallucinated
        assert "45" in hallucinated
        assert any("Number guardrail warning" in m for m in caplog.messages)


def test_cancellation_on_new_ptt():
    """Pressing PTT must cancel any in-flight AI agent task."""
    async def _run():
        fake_llm = FakeLLMClient(delay=1.0)
        radio = RadioQueue()
        agent = RaceEngineerAgent(llm=fake_llm, radio=radio)

        engineer = Engineer(ptt_action=1, agent=agent)
        engineer.loop = asyncio.get_running_loop()

        # Trigger AI query
        engineer.ask("What is our strategy plan?")
        assert engineer._agent_task is not None
        assert not engineer._agent_task.done()

        # Driver presses PTT
        engineer.on_ptt(True)
        # Yield to event loop to process cancellation
        await asyncio.sleep(0.01)
        assert engineer._agent_task.cancelled()

    asyncio.run(_run())



def test_both_providers_fail_produces_radio_check():
    """If LLM stream yields nothing, agent falls back to 'Radio check, say again.'."""
    async def _run():
        radio = RadioQueue()
        fake_llm = FakeLLMClient(tokens=[])
        agent = RaceEngineerAgent(llm=fake_llm, radio=radio)

        reply = await agent.answer("strategy advice", {"lap": 6})
        assert reply == "Radio check, say again."

        c = radio.pop()
        assert c is not None
        assert c.priority == -1
        assert "Radio check, say again." in c.text

    asyncio.run(_run())
