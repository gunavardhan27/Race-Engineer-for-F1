"""In-Game Radio Check System for F1 Race Engineer.

Features:
- Handles two-way radio check state machine (command "radio_check"):
  1. "Radio check, how do you read?"
  2. Waits up to 8s for driver PTT transmission.
  3. "Loud and clear. I heard: <transcript>."
     or "Nothing received. Check your mic." (timeout / no audio)
     or "Very quiet, check your mic level." (peak level < 0.02)
     or "Couldn't make that out, say again." (empty transcript).
- Automatic session-start radio check (config radio_check.on_session_start):
  3s after first packets of a new session: "Radio check. Telemetry's live, we're at <track>."
  Fires once per session; skipped in quiet mode.
- Voice radio check intent reply formatting:
  "Loud and clear. Telemetry's live." with persona line appended when active.
"""
from __future__ import annotations

import enum
import logging
import time
from typing import Any, Callable, Dict, Optional, Set

import numpy as np

from config import RadioCheckConfig
from f1telemetry.state import RaceState, TRACKS

log = logging.getLogger("engineer.radio_check")


class TwoWayState(enum.Enum):
    IDLE = "idle"
    WAITING_FOR_DRIVER = "waiting_for_driver"


class RadioCheckManager:
    def __init__(self, config: Optional[RadioCheckConfig] = None):
        self.config = config or RadioCheckConfig()
        self.state = TwoWayState.IDLE
        self.deadline: float = 0.0
        self.driver_speaking: bool = False

        # Session start tracking: session_uid -> first_seen_timestamp
        self._session_first_seen: Dict[int, float] = {}
        self._sessions_announced: Set[int] = set()

    def start_two_way_check(
        self,
        say_fn: Optional[Callable[[str], None]] = None,
        timeout_s: float = 8.0,
        now: Optional[float] = None,
    ) -> str:
        """Initiates two-way radio check command."""
        t = now if now is not None else time.monotonic()
        self.state = TwoWayState.WAITING_FOR_DRIVER
        self.deadline = t + timeout_s
        self.driver_speaking = False

        prompt = "Radio check, how do you read?"
        log.info("Two-way radio check initiated: %r (waiting up to %.1fs)", prompt, timeout_s)
        if say_fn:
            say_fn(prompt)
        return prompt

    def on_ptt(self, down: bool) -> None:
        """Tracks PTT transitions while waiting for two-way check answer."""
        if self.state == TwoWayState.WAITING_FOR_DRIVER and down:
            self.driver_speaking = True

    def on_audio(
        self,
        audio: Optional[np.ndarray],
        stt_transcribe_fn: Callable[[np.ndarray], str],
    ) -> Optional[str]:
        """Processes driver audio during two-way radio check."""
        if self.state != TwoWayState.WAITING_FOR_DRIVER and not self.driver_speaking:
            return None

        self.state = TwoWayState.IDLE
        self.driver_speaking = False

        # 1. No audio received
        if audio is None or len(audio) == 0:
            msg = "Nothing received. Check your mic."
            log.info("Two-way radio check result: %s", msg)
            return msg

        # 2. Very low peak level check
        peak = float(np.max(np.abs(audio)))
        if peak < 0.02:
            msg = "Very quiet, check your mic level."
            log.info("Two-way radio check result (peak=%.4f): %s", peak, msg)
            return msg

        # 3. Transcribe audio with STT
        try:
            transcript = stt_transcribe_fn(audio).strip()
        except Exception as e:
            log.warning("Two-way radio check STT failed: %s", e)
            transcript = ""

        if not transcript:
            msg = "Couldn't make that out, say again."
            log.info("Two-way radio check result: %s", msg)
            return msg

        msg = f"Loud and clear. I heard: {transcript}."
        log.info("Two-way radio check success: %s", msg)
        return msg

    def check_timeout(self, now: Optional[float] = None) -> Optional[str]:
        """Checks if the 8s driver response deadline expired."""
        if self.state != TwoWayState.WAITING_FOR_DRIVER:
            return None

        t = now if now is not None else time.monotonic()
        if t >= self.deadline and not self.driver_speaking:
            self.state = TwoWayState.IDLE
            msg = "Nothing received. Check your mic."
            log.info("Two-way radio check timed out after deadline: %s", msg)
            return msg
        return None

    def on_session_packet(
        self,
        session_uid: int,
        track_name: str = "",
        quiet_mode: bool = False,
        now: Optional[float] = None,
    ) -> Optional[str]:
        """Evaluates 3s post-session-start check. Fires once per session, skipped in quiet mode."""
        if not self.config.on_session_start or not session_uid:
            return None

        if session_uid in self._sessions_announced:
            return None

        t = now if now is not None else time.monotonic()
        if session_uid not in self._session_first_seen:
            self._session_first_seen[session_uid] = t
            return None

        first_seen = self._session_first_seen[session_uid]
        if (t - first_seen) >= 3.0:
            self._sessions_announced.add(session_uid)
            if quiet_mode:
                log.info("Session start radio check skipped due to quiet mode (session_uid=%d)", session_uid)
                return None

            location = track_name or "the circuit"
            msg = f"Radio check. Telemetry's live, we're at {location}."
            log.info("Session start radio check triggered: %r", msg)
            return msg

        return None

    @staticmethod
    def build_voice_reply(
        st: RaceState,
        personality_engine: Optional[Any] = None,
    ) -> str:
        """Builds reply for deterministic voice intent 'radio_check'."""
        is_live = (st.last_packet_time > 0 and (time.monotonic() - st.last_packet_time) < 4.0)
        base = "Loud and clear. Telemetry's live." if is_live else "Loud and clear. No telemetry yet."

        if personality_engine and getattr(personality_engine, "lines", None):
            persona_name = getattr(personality_engine.config, "name", "professional")
            if persona_name != "professional":
                line = personality_engine.get_reaction_text("radio_check")
                if line:
                    return f"{base} {line}"
        return base
