"""Unit tests for in-game radio check features (Feature A)."""
import numpy as np
import pytest

from config import AppConfig, PersonaConfig, RadioCheckConfig
from f1telemetry.state import RaceState
from personality import PersonalityEngine
from radio_check import RadioCheckManager, TwoWayState


def test_two_way_radio_check_normal_flow():
    """Two-way check replies with transcript when audio has adequate level."""
    mgr = RadioCheckManager()
    prompt = mgr.start_two_way_check(timeout_s=8.0, now=100.0)
    assert prompt == "Radio check, how do you read?"
    assert mgr.state == TwoWayState.WAITING_FOR_DRIVER

    audio = np.array([0.3, -0.4, 0.25], dtype=np.float32)
    reply = mgr.on_audio(audio, stt_transcribe_fn=lambda a: "Loud and clear")

    assert reply == "Loud and clear. I heard: Loud and clear."
    assert mgr.state == TwoWayState.IDLE


def test_two_way_radio_check_no_audio():
    """Two-way check handles no audio received."""
    mgr = RadioCheckManager()
    mgr.start_two_way_check(timeout_s=8.0, now=100.0)

    # Empty array
    reply_empty = mgr.on_audio(np.array([], dtype=np.float32), stt_transcribe_fn=lambda a: "test")
    assert reply_empty == "Nothing received. Check your mic."

    # None audio
    mgr.start_two_way_check(timeout_s=8.0, now=100.0)
    reply_none = mgr.on_audio(None, stt_transcribe_fn=lambda a: "test")
    assert reply_none == "Nothing received. Check your mic."


def test_two_way_radio_check_very_quiet_audio():
    """Two-way check detects low peak amplitude (< 0.02) and warns driver."""
    mgr = RadioCheckManager()
    mgr.start_two_way_check(timeout_s=8.0, now=100.0)

    # Peak amplitude is 0.015 (< 0.02)
    quiet_audio = np.array([0.005, -0.015, 0.01], dtype=np.float32)
    reply = mgr.on_audio(quiet_audio, stt_transcribe_fn=lambda a: "hello")
    assert reply == "Very quiet, check your mic level."


def test_two_way_radio_check_empty_transcript():
    """Two-way check with audible volume but undecipherable speech."""
    mgr = RadioCheckManager()
    mgr.start_two_way_check(timeout_s=8.0, now=100.0)

    audio = np.array([0.5, -0.5, 0.3], dtype=np.float32)
    reply = mgr.on_audio(audio, stt_transcribe_fn=lambda a: "   ")
    assert reply == "Couldn't make that out, say again."


def test_two_way_radio_check_timeout():
    """Two-way check expires after 8s if no audio is received."""
    mgr = RadioCheckManager()
    mgr.start_two_way_check(timeout_s=8.0, now=50.0)

    # Check at 54.0s (under deadline)
    assert mgr.check_timeout(now=54.0) is None
    assert mgr.state == TwoWayState.WAITING_FOR_DRIVER

    # Check at 58.01s (past deadline)
    timeout_msg = mgr.check_timeout(now=58.01)
    assert timeout_msg == "Nothing received. Check your mic."
    assert mgr.state == TwoWayState.IDLE


def test_session_start_check_fires_after_3s():
    """Session start check fires 3s after first packets and only once per session."""
    mgr = RadioCheckManager(RadioCheckConfig(on_session_start=True))
    session_uid = 123456789

    # First packet seen at t=10.0s
    assert mgr.on_session_packet(session_uid, "Monza", quiet_mode=False, now=10.0) is None

    # At t=12.0s (< 3.0s elapsed)
    assert mgr.on_session_packet(session_uid, "Monza", quiet_mode=False, now=12.0) is None

    # At t=13.1s (>= 3.0s elapsed) -> Fires
    res = mgr.on_session_packet(session_uid, "Monza", quiet_mode=False, now=13.1)
    assert res == "Radio check. Telemetry's live, we're at Monza."

    # Subsequent packets in same session do not fire again
    assert mgr.on_session_packet(session_uid, "Monza", quiet_mode=False, now=14.0) is None
    assert mgr.on_session_packet(session_uid, "Monza", quiet_mode=False, now=20.0) is None


def test_session_start_check_skipped_in_quiet_mode():
    """Session start check is suppressed if quiet_mode is active."""
    mgr = RadioCheckManager(RadioCheckConfig(on_session_start=True))
    session_uid = 987654321

    assert mgr.on_session_packet(session_uid, "Spa", quiet_mode=True, now=10.0) is None
    # 3 seconds later in quiet mode
    res = mgr.on_session_packet(session_uid, "Spa", quiet_mode=True, now=13.1)
    assert res is None


def test_voice_radio_check_with_persona_and_profanity():
    """Voice reply appends persona radio_check line respecting profanity filter."""
    st = RaceState()
    import time
    st.last_packet_time = time.monotonic()  # telemetry live

    # 1. Clean profanity
    cfg_clean = PersonaConfig(name="cocky", profanity="none")
    engine_clean = PersonalityEngine(config=cfg_clean)
    reply_clean = RadioCheckManager.build_voice_reply(st, engine_clean)
    assert "Loud and clear. Telemetry's live." in reply_clean
    # Clean check: should not contain mild/strong curse words
    assert "hell" not in reply_clean.lower()
    assert "bloody" not in reply_clean.lower()

    # 2. Professional persona (no reaction lines)
    cfg_prof = PersonaConfig(name="professional")
    engine_prof = PersonalityEngine(config=cfg_prof)
    reply_prof = RadioCheckManager.build_voice_reply(st, engine_prof)
    assert reply_prof == "Loud and clear. Telemetry's live."
