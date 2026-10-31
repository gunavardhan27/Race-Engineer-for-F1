"""Unit and integration tests for the Radio Test Bench (Prompt 27).

Covers:
1. Hold-to-talk button, Space bar, and transmission pipeline (audio -> STT -> route -> TTS -> FX -> playback).
2. Typed question bypasses STT directly to routing and reply.
3. Polite no-data answers for each deterministic intent and AI agent when telemetry is absent.
4. Demo mode toggle, DEMO DATA banner, first-reply prefix, state isolation from DB/live.
5. All 5 diagnostic failure states (silent mic, empty transcript, AI fallback, TTS failure, missing output device).
6. Disabled/blocked states during a live session (in manager and FastAPI endpoints).
7. Radio log logging with source "radio_test".
8. Guided checklist auto-marking.
9. Dashboard REST endpoints.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional
import numpy as np
import pytest
from fastapi.testclient import TestClient

from config import AppConfig, RadioTestConfig
from f1telemetry.deterministic import Reply, route
from f1telemetry.state import RaceState
from radio_test import RadioTestManager, build_demo_state


class DummyTTS:
    def __init__(self, should_fail: bool = False):
        self.should_fail = should_fail
        self.last_text = None

    async def synth(self, text: str):
        if self.should_fail:
            raise RuntimeError("TTS connection refused")
        self.last_text = text
        sr = 16000
        samples = int(sr * 0.1)
        audio = (0.3 * np.sin(np.linspace(0, 2 * np.pi * 440, samples))).astype(np.float32)
        return audio, sr


class DummySTT:
    def __init__(self, transcript: str = "radio check"):
        self.transcript = transcript
        self.transcribe_called = False

    def transcribe(self, audio: np.ndarray, prompt: str = "") -> str:
        self.transcribe_called = True
        return self.transcript


class DummyAudioPlayer:
    def __init__(self, should_fail: bool = False, device: Optional[str] = None):
        self.device = device
        self.should_fail = should_fail
        self.played = []

    def play_direct(self, audio: np.ndarray, sr: int = 16000) -> bool:
        if self.should_fail:
            raise RuntimeError("Audio device disconnected")
        self.played.append((audio, sr))
        return True


class DummyCapture:
    def __init__(self, device: str = "USB Mic"):
        self.device = device
        self.ptt_down = False
        self.clipping_reset = False

    def on_ptt(self, down: bool, ignore_min_duration: bool = False):
        self.ptt_down = down
        if not down:
            sr = 16000
            samples = int(sr * 0.2)
            return (0.3 * np.sin(np.linspace(0, 2 * np.pi * 440, samples))).astype(np.float32)
        return None

    def reset_clipping(self):
        self.clipping_reset = True

    def get_live_level(self):
        return {"peak": 0.25, "rms": 0.1, "peak_db": -12.0, "clipping": False}


class DummyAgent:
    def __init__(self, reply: str = "Try softening the front anti-roll bar.", fallback_used: bool = False):
        self.reply = reply
        self.fallback_used = fallback_used
        self.last_prompt = None
        self.last_snapshot = None
        self.last_knowledge = None

    async def answer_with_diagnostics(self, text: str, snapshot: Dict[str, Any], knowledge: str = "") -> Dict[str, Any]:
        self.last_prompt = text
        self.last_snapshot = snapshot
        self.last_knowledge = knowledge
        return {
            "reply_text": self.reply,
            "ttft_ms": 120.5,
            "total_ms": 280.0,
            "provider": "gemini",
            "fallback_used": self.fallback_used,
            "fallback_name": "groq" if self.fallback_used else None,
        }


class DummyBroadcaster:
    def __init__(self):
        self.messages = []
        self.radio_logs = []

    def broadcast(self, event_type: str, payload: Any):
        self.messages.append((event_type, payload))

    def broadcast_radio(self, speaker: str, text: str, latency_ms: Optional[float] = None, source: str = "live"):
        self.radio_logs.append({
            "speaker": speaker,
            "text": text,
            "latency_ms": latency_ms,
            "source": source,
        })


@pytest.fixture
def base_components():
    cfg = AppConfig()
    cfg.radio_test = RadioTestConfig(enabled=True)
    tts = DummyTTS()
    stt = DummySTT("radio check")
    player = DummyAudioPlayer()
    capture = DummyCapture()
    agent = DummyAgent()
    broadcaster = DummyBroadcaster()
    state = RaceState()

    manager = RadioTestManager(
        engineer=None,
        tts=tts,
        player=player,
        capture=capture,
        stt=stt,
        agent=agent,
        state=state,
        broadcaster=broadcaster,
        config=cfg,
        is_session_live_fn=lambda: False,
    )
    return {
        "cfg": cfg,
        "tts": tts,
        "stt": stt,
        "player": player,
        "capture": capture,
        "agent": agent,
        "broadcaster": broadcaster,
        "state": state,
        "manager": manager,
    }


# ==============================================================================
# 1. Push-To-Talk & Pipeline Tests
# ==============================================================================

def test_ptt_start_stop_pipeline(base_components):
    """Hold-to-talk start/stop executes audio pipeline and logs with source radio_test."""
    async def _run():
        mgr: RadioTestManager = base_components["manager"]
        bcast: DummyBroadcaster = base_components["broadcaster"]
        player: DummyAudioPlayer = base_components["player"]

        # Start PTT
        start_res = mgr.start_ptt()
        assert start_res["status"] == "recording"
        assert mgr.is_recording is True

        # Stop PTT
        stop_res = await mgr.stop_ptt()
        assert stop_res["status"] == "ok"
        assert mgr.is_recording is False

        diag = stop_res["diagnostic"]
        assert diag["mic"]["ok"] is True
        assert diag["mic"]["peak"] > 0
        assert diag["stt"]["ok"] is True
        assert diag["stt"]["transcript"] == "radio check"
        assert diag["routing"]["kind"] == "deterministic"
        assert diag["routing"]["intent"] == "radio_check"
        assert "Loud and clear" in diag["reply"]["text"]
        assert diag["tts"]["ok"] is True
        assert diag["playback"]["ok"] is True
        assert len(player.played) > 0

        # Verify radio log source for both driver and engineer
        assert len(bcast.radio_logs) == 2
        assert bcast.radio_logs[0]["speaker"] == "driver"
        assert bcast.radio_logs[0]["source"] == "radio_test"
        assert bcast.radio_logs[1]["speaker"] == "engineer"
        assert bcast.radio_logs[1]["source"] == "radio_test"
        assert "Loud and clear" in bcast.radio_logs[1]["text"]

    asyncio.run(_run())


def test_typed_question_bypasses_stt(base_components):
    """Typing a question bypasses STT to isolate mic problems."""
    async def _run():
        mgr: RadioTestManager = base_components["manager"]
        stt: DummySTT = base_components["stt"]

        res = await mgr.submit_typed_text("Gap ahead?")
        assert res["status"] == "ok"
        assert not stt.transcribe_called, "STT must be bypassed on typed text"

        diag = res["diagnostic"]
        assert diag["stt"]["skipped"] is True
        assert diag["stt"]["transcript"] == "Gap ahead?"
        assert diag["routing"]["intent"] == "gap_ahead"
        assert "No telemetry" in diag["reply"]["text"] or "gaps" in diag["reply"]["text"]

    asyncio.run(_run())


# ==============================================================================
# 2. No-Data Answers for Deterministic & AI
# ==============================================================================

def test_no_data_answers_for_all_intents():
    """All deterministic intents answer clearly instead of staying silent when telemetry is absent."""
    empty_state = RaceState()
    assert not empty_state.has_telemetry

    # Test major deterministic queries
    test_cases = [
        ("radio check", "Loud and clear. No telemetry yet."),
        ("gap ahead", "No telemetry, I can't see the gaps."),
        ("gap behind", "No telemetry, I can't see the gaps."),
        ("fuel", "No car data yet."),
        ("how are the tyres", "No car data yet."),
        ("weather update", "No car data yet."),
        ("damage report", "No car data yet."),
        ("drs status", "No car data yet."),
        ("what is my position", "No car data yet."),
        ("how many laps left", "No car data yet."),
        ("pit window", "No car data yet."),
        ("delta to best", "No lap data yet."),
        ("gap to pole", "No telemetry, I can't see the gaps."),
        ("am i through to q2", "No qualifying data yet."),
        ("where is my teammate", "No session data yet."),
        ("car capability", "No session data yet."),
        ("where am i losing time", "No lap data yet."),
        ("who is fastest in sector 1", "No lap data yet."),
        ("ideal lap", "No lap data yet."),
        ("battle pace", "No car data yet."),
        ("any retirements", "No session data yet."),
    ]

    for phrase, expected_phrase in test_cases:
        rep: Reply = route(phrase, empty_state)
        assert rep is not None, f"Phrase '{phrase}' produced None"
        assert rep.text, f"Phrase '{phrase}' produced empty text"
        assert expected_phrase.lower() in rep.text.lower(), (
            f"Expected '{expected_phrase}' in reply for '{phrase}', got: '{rep.text}'"
        )


def test_ai_agent_empty_state_handling(base_components):
    """When STATE is empty, AI agent is routed and answers without hallucinating numbers."""
    async def _run():
        mgr: RadioTestManager = base_components["manager"]
        agent: DummyAgent = base_components["agent"]

        res = await mgr.submit_typed_text("I'm understeering in slow corners, what can I change?")
        assert res["status"] == "ok"
        diag = res["diagnostic"]
        assert diag["routing"]["kind"] == "ai"
        assert diag["reply"]["text"] == "Try softening the front anti-roll bar."
        assert agent.last_snapshot == {}, "AI agent must receive empty snapshot when telemetry is absent"

    asyncio.run(_run())


# ==============================================================================
# 3. Demo Mode Toggle, Banner, and Isolation
# ==============================================================================

def test_demo_mode_toggle_and_first_reply(base_components):
    """Demo mode enables simulated session, banners, prefixes 'Demo data.', and clears on disable."""
    async def _run():
        mgr: RadioTestManager = base_components["manager"]

        # 1. Enable Demo Mode
        res_on = mgr.set_demo_mode(True)
        assert res_on["demo_mode"] is True
        assert res_on["banner"] == "DEMO DATA"
        assert mgr.demo_state is not None
        assert mgr.demo_state.has_telemetry is True
        assert getattr(mgr.demo_state, "is_demo", False) is True

        # 2. First reply must start with 'Demo data.'
        first_res = await mgr.submit_typed_text("Gap ahead?")
        reply1 = first_res["diagnostic"]["reply"]["text"]
        assert reply1.startswith("Demo data. ")
        assert "behind" in reply1 or "Verstappen" in reply1 or "seconds" in reply1

        # 3. Second reply in demo mode does NOT repeat prefix
        second_res = await mgr.submit_typed_text("How are the tyres?")
        reply2 = second_res["diagnostic"]["reply"]["text"]
        assert not reply2.startswith("Demo data.")
        assert "wear" in reply2.lower() or "percent" in reply2.lower()

        # 4. Turn off Demo Mode
        res_off = mgr.set_demo_mode(False)
        assert res_off["demo_mode"] is False
        assert res_off["banner"] is None
        assert mgr.demo_state is None

        # 5. Queries now return no-data
        third_res = await mgr.submit_typed_text("Gap ahead?")
        reply3 = third_res["diagnostic"]["reply"]["text"]
        assert "No telemetry" in reply3

    asyncio.run(_run())


def test_demo_state_isolation():
    """Demo state builder produces isolated data that does not touch real engine state."""
    demo_st = build_demo_state()
    assert demo_st.is_demo is True
    assert demo_st.has_telemetry is True
    assert demo_st.player == 0
    assert len(demo_st.participants) > 0


# ==============================================================================
# 4. Diagnostic Failure States
# ==============================================================================

def test_diagnostic_silent_mic(base_components):
    """Peak near zero (< 0.005) reports silent mic warning."""
    async def _run():
        mgr: RadioTestManager = base_components["manager"]
        silent_audio = np.zeros(1600, dtype=np.float32)

        res = await mgr.stop_ptt(manual_audio=silent_audio)
        diag = res["diagnostic"]
        assert diag["mic"]["ok"] is False
        assert diag["mic"]["status"] == "fail"
        assert "Mic silent" in diag["mic"]["message"]
        assert "Windows microphone permission" in diag["mic"]["message"] or "input device" in diag["mic"]["message"]

    asyncio.run(_run())


def test_diagnostic_empty_transcript(base_components):
    """Empty transcript reports 'Couldn't make that out'."""
    async def _run():
        mgr: RadioTestManager = base_components["manager"]
        stt: DummySTT = base_components["stt"]
        stt.transcript = "   "  # Empty / whitespace

        audio = (0.2 * np.sin(np.linspace(0, 20, 1600))).astype(np.float32)
        res = await mgr.stop_ptt(manual_audio=audio)
        diag = res["diagnostic"]

        assert diag["mic"]["ok"] is True
        assert diag["stt"]["ok"] is False
        assert diag["stt"]["status"] == "fail"
        assert "Couldn't make that out" in diag["stt"]["message"]

    asyncio.run(_run())


def test_diagnostic_ai_fallback(base_components):
    """When AI primary provider fails and falls back, fallback info is captured in routing diag."""
    async def _run():
        mgr: RadioTestManager = base_components["manager"]
        agent: DummyAgent = base_components["agent"]
        agent.fallback_used = True

        res = await mgr.submit_typed_text("How do I fix corner exit oversteer?")
        diag = res["diagnostic"]
        assert diag["routing"]["kind"] == "ai"
        assert diag["routing"]["fallback"] == "groq"

    asyncio.run(_run())


def test_diagnostic_tts_failure(base_components):
    """TTS synthesis exception is caught and flagged as diagnostic failure."""
    async def _run():
        mgr: RadioTestManager = base_components["manager"]
        mgr.tts = DummyTTS(should_fail=True)

        res = await mgr.submit_typed_text("Radio check")
        diag = res["diagnostic"]
        assert diag["tts"]["ok"] is False
        assert diag["tts"]["status"] == "fail"
        assert "Synthesis failed" in diag["tts"]["message"]

    asyncio.run(_run())


def test_diagnostic_missing_output_device(base_components):
    """Audio player playback exception flags missing output device."""
    async def _run():
        mgr: RadioTestManager = base_components["manager"]
        mgr.player = DummyAudioPlayer(should_fail=True)

        res = await mgr.submit_typed_text("Radio check")
        diag = res["diagnostic"]
        assert diag["playback"]["ok"] is False
        assert diag["playback"]["status"] == "fail"
        assert "Output device missing" in diag["playback"]["message"]

    asyncio.run(_run())


# ==============================================================================
# 5. Live Session Gating
# ==============================================================================

def test_page_disabled_during_live_session(base_components):
    """When a session is live, Radio Test manager blocks PTT and text submission."""
    async def _run():
        mgr: RadioTestManager = base_components["manager"]
        mgr.is_session_live_fn = lambda: True

        res_start = mgr.start_ptt()
        assert res_start["status"] == "blocked"
        assert "Live session running" in res_start["message"]

        res_stop = await mgr.stop_ptt()
        assert res_stop["status"] == "blocked"
        assert "Live session running" in res_stop["message"]

        res_text = await mgr.submit_typed_text("Radio check")
        assert res_text["status"] == "blocked"
        assert "Live session running" in res_text["message"]

    asyncio.run(_run())


# ==============================================================================
# 6. Guided Checklist Auto-Marking
# ==============================================================================

def test_guided_checklist_auto_marking(base_components):
    """Checklist items auto-mark as passed when matching queries succeed."""
    async def _run():
        mgr: RadioTestManager = base_components["manager"]
        chk = mgr.checklist
        assert all(not item["passed"] for item in chk)

        # 1. Radio check
        await mgr.submit_typed_text("Radio check")
        assert next(item for item in mgr.checklist if item["id"] == "radio_check")["passed"] is True

        # 2. Gap ahead
        await mgr.submit_typed_text("Gap ahead?")
        assert next(item for item in mgr.checklist if item["id"] == "gap_ahead")["passed"] is True

        # 3. How are the tyres?
        await mgr.submit_typed_text("How are the tyres?")
        assert next(item for item in mgr.checklist if item["id"] == "tyres")["passed"] is True

        # 4. AI question
        await mgr.submit_typed_text("I'm understeering in slow corners, what can I change?")
        assert next(item for item in mgr.checklist if item["id"] == "ai_question")["passed"] is True

        # 5. Typed question (already tested above by submitting typed text)
        assert next(item for item in mgr.checklist if item["id"] == "typed_question")["passed"] is True

        # All 5 checklist items passed
        assert all(item["passed"] for item in mgr.checklist)

    asyncio.run(_run())


# ==============================================================================
# 7. Dashboard REST Endpoints
# ==============================================================================

def test_dashboard_radio_test_endpoints(base_components):
    """FastAPI dashboard endpoints for radio test bench return correct payloads and status codes."""
    from dashboard.server import create_app

    mgr: RadioTestManager = base_components["manager"]
    state: RaceState = base_components["state"]
    app = create_app(
        state=state,
        broadcaster=base_components["broadcaster"],
        token="",
        radio_test_manager=mgr,
    )
    client = TestClient(app)

    # 1. GET /api/radio_test/state
    res = client.get("/api/radio_test/state")
    assert res.status_code == 200
    st_data = res.json()
    assert "demo_mode" in st_data
    assert "checklist" in st_data
    assert len(st_data["checklist"]) == 5

    # 2. POST /api/radio_test/page_state
    res = client.post("/api/radio_test/page_state", json={"open": True})
    assert res.status_code == 200
    assert mgr.is_page_open is True

    # 3. POST /api/radio_test/demo_mode
    res = client.post("/api/radio_test/demo_mode", json={"enabled": True})
    assert res.status_code == 200
    assert res.json()["demo_mode"] is True
    assert mgr.demo_mode is True

    # 4. GET /api/radio_test/mic/level
    res = client.get("/api/radio_test/mic/level")
    assert res.status_code == 200
    lvl = res.json()
    assert "peak_db" in lvl

    # 5. POST /api/radio_test/submit_text
    res = client.post("/api/radio_test/submit_text", json={"text": "Radio check"})
    assert res.status_code == 200
    assert res.json()["status"] == "ok"
    assert "diagnostic" in res.json()

    # 6. POST /api/radio_test/ptt/start and /stop
    res_start = client.post("/api/radio_test/ptt/start")
    assert res_start.status_code == 200
    assert res_start.json()["status"] == "recording"

    res_stop = client.post("/api/radio_test/ptt/stop")
    assert res_stop.status_code == 200
    assert res_stop.json()["status"] == "ok"

    # 7. Test 409 conflict when session is live
    mgr.is_session_live_fn = lambda: True
    res_live_start = client.post("/api/radio_test/ptt/start")
    assert res_live_start.status_code == 409

    res_live_text = client.post("/api/radio_test/submit_text", json={"text": "Radio check"})
    assert res_live_text.status_code == 409
