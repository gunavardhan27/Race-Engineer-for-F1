"""Radio Test System — Talk to the engineer without the game and verify audio chain.

Features:
1. Push-to-talk without game running:
   - Hold-to-talk button on page (mouse/touch: press = start, release = stop).
   - Hold Space bar while on the Radio test page.
   - Optional controller button via pygame (config radio_test.joystick_button), using
     SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS=1. Only active while page is open.
   - Text box to type a question (skips STT).
2. Pipeline: audio -> STT -> route() -> deterministic or AI agent -> TTS -> radio FX -> playback.
   Logs each transmission to radio log with source "radio_test".
3. No-data answers: all deterministic intents answer clearly instead of staying silent.
   AI agent gets empty STATE and explains it has no data instead of inventing numbers.
4. Demo mode toggle: loads built-in sample session (reusing test_synthetic packet builders).
   Shows DEMO DATA banner; prefixes "Demo data." to the first reply. Never stored in DB or mixed with real sessions.
5. Diagnostics for every transmission with pass/fail and reasons:
   - Mic: device name, live level meter, peak dB, clipping, silent check
   - STT: transcript, time in ms, empty transcript check, skipped on typed input
   - Routing: deterministic intent or AI provider with fallback info
   - Reply text: AI TTFT and total duration
   - TTS: engine, time in ms, synthesis error handling
   - Playback: output device name, missing device fallback
   - Total turnaround latency from release to audio start
6. Guided test checklist: auto-marked on matching expected results.
7. Live session gating: if session is live, shows warning and disables page PTT.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import threading
import time
from typing import Any, Callable, Dict, Optional

import numpy as np

from audio_devices import resolve_device
from audio_player import AudioPlayer
from config import AppConfig
from f1telemetry.deterministic import Reply, route
from f1telemetry.packets import parse
from f1telemetry.state import RaceState
from radio_check import RadioCheckManager
from radio_fx import apply_radio_fx
from stt import build_initial_prompt

log = logging.getLogger("engineer.radio_test")

DEFAULT_CHECKLIST = [
    {
        "id": "radio_check",
        "title": 'Say or transmit "Radio check"',
        "expected": 'Loud and clear ("Loud and clear. No telemetry yet.")',
        "passed": False,
    },
    {
        "id": "gap_ahead",
        "title": 'Ask "Gap ahead?"',
        "expected": 'No data ("No telemetry, I can\'t see the gaps.") or realistic gap in demo mode',
        "passed": False,
    },
    {
        "id": "tyres",
        "title": 'Ask "How are the tyres?"',
        "expected": 'No data ("No car data yet.") or tyre wear breakdown in demo mode',
        "passed": False,
    },
    {
        "id": "ai_question",
        "title": 'Ask AI advice ("I\'m understeering in slow corners, what can I change?")',
        "expected": "AI racing advice reply without inventing telemetry numbers",
        "passed": False,
    },
    {
        "id": "typed_question",
        "title": "Type a question in the text box instead of speaking",
        "expected": "Immediate answer bypassing STT",
        "passed": False,
    },
]


def build_demo_state() -> RaceState:
    """Builds a rich synthetic RaceState using test_synthetic packet builders."""
    from test_synthetic import (
        damage_packet,
        lap_packet,
        motion_packet,
        participants_packet,
        session_packet,
        status_packet,
        telemetry_packet,
    )

    st = RaceState()
    setattr(st, "is_demo", True)

    packets_to_feed = [
        session_packet(),
        participants_packet({0: "Player", 1: "Verstappen", 2: "Norris", 3: "Leclerc", 4: "Piastri"}),
        lap_packet(10, {0: (2, 1400, 91500), 1: (1, 0, 90800), 2: (3, 2200, 92100), 3: (4, 4500, 92600)}),
        status_packet(fuel=24.5, delta=0.5),  # Fuel is plus 0.5 laps
        damage_packet(wear=(18, 16, 28, 22), fl_wing=0),  # Worst wear is front left (28%)
        telemetry_packet(),
        motion_packet(),
    ]

    for raw in packets_to_feed:
        h, data = parse(raw)
        if data is not None:
            st.update(h, data)

    # Ensure demo state flags
    st.last_packet_time = time.monotonic()
    setattr(st, "is_demo", True)
    return st


class RadioTestManager:
    """Orchestrates the offline radio test bench and end-to-end diagnostic evaluations."""

    def __init__(
        self,
        engineer: Any = None,
        tts: Any = None,
        player: Optional[AudioPlayer] = None,
        capture: Any = None,
        stt: Any = None,
        agent: Any = None,
        state: Optional[RaceState] = None,
        personality: Any = None,
        broadcaster: Any = None,
        latency: Any = None,
        config: Optional[AppConfig] = None,
        is_session_live_fn: Optional[Callable[[], bool]] = None,
    ):
        self.engineer = engineer
        self.tts = tts
        self.player = player
        self.capture = capture
        self.stt = stt
        self.agent = agent
        self.state = state
        self.personality = personality
        self.broadcaster = broadcaster
        self.latency = latency
        self.config = config or AppConfig()
        self.is_session_live_fn = is_session_live_fn or (lambda: False)

        self._lock = threading.Lock()
        self.is_page_open: bool = False
        self.is_recording: bool = False
        self.demo_mode: bool = False
        self.demo_state: Optional[RaceState] = None
        self.demo_announced_first_reply: bool = False
        self._ptt_press_time: float = 0.0

        self.latest_diagnostics: Optional[Dict[str, Any]] = None
        self.checklist: list[Dict[str, Any]] = [dict(item) for item in DEFAULT_CHECKLIST]

    def is_session_live(self) -> bool:
        """Returns True if a live session is currently receiving in-game telemetry."""
        try:
            return bool(self.is_session_live_fn())
        except Exception as e:
            log.debug("Error checking live session state: %s", e)
            return False

    def set_page_open(self, open_state: bool) -> Dict[str, Any]:
        """Notifies manager that Radio Test page is open or closed in dashboard."""
        with self._lock:
            self.is_page_open = bool(open_state)
            if not self.is_page_open and self.is_recording:
                self.is_recording = False
                if self.capture and hasattr(self.capture, "on_ptt"):
                    self.capture.on_ptt(False)
        return {"status": "ok", "page_open": self.is_page_open}

    def set_demo_mode(self, enabled: bool) -> Dict[str, Any]:
        """Toggles demo mode with synthetic realistic session data."""
        with self._lock:
            self.demo_mode = bool(enabled)
            if self.demo_mode:
                self.demo_state = build_demo_state()
                self.demo_announced_first_reply = False
                log.info("Radio Test Demo Mode ENABLED: synthetic session state loaded.")
            else:
                self.demo_state = None
                self.demo_announced_first_reply = False
                log.info("Radio Test Demo Mode DISABLED: demo state cleared.")

        if self.broadcaster:
            self.broadcaster.broadcast("radio_test_demo_mode", {"demo_mode": self.demo_mode})

        return {
            "status": "ok",
            "demo_mode": self.demo_mode,
            "banner": "DEMO DATA" if self.demo_mode else None,
        }

    def start_ptt(self) -> Dict[str, Any]:
        """Starts audio recording from microphone for push-to-talk."""
        if self.is_session_live():
            return {
                "status": "blocked",
                "message": "Live session running: use your wheel push-to-talk",
                "session_live": True,
            }

        with self._lock:
            self.is_recording = True
            self._ptt_press_time = time.monotonic()
            if self.capture:
                self.capture.reset_clipping()
                if hasattr(self.capture, "on_ptt"):
                    self.capture.on_ptt(True)

        log.debug("Radio Test: PTT started")
        return {"status": "recording"}

    async def stop_ptt(self, manual_audio: Optional[np.ndarray] = None) -> Dict[str, Any]:
        """Stops recording and executes the transmission through the pipeline."""
        if self.is_session_live():
            with self._lock:
                self.is_recording = False
            return {
                "status": "blocked",
                "message": "Live session running: use your wheel push-to-talk",
                "session_live": True,
            }

        audio = manual_audio
        with self._lock:
            if not self.is_recording and audio is None:
                return {"status": "idle", "message": "PTT was not active"}
            self.is_recording = False
            if audio is None and self.capture and hasattr(self.capture, "on_ptt"):
                audio = self.capture.on_ptt(False, ignore_min_duration=True)
                if audio is None and hasattr(self.capture, "capture_now"):
                    audio = self.capture.capture_now(ignore_min_duration=True)

        return await self.process_transmission(audio=audio, typed_text=None)

    async def submit_typed_text(self, text: str) -> Dict[str, Any]:
        """Submits a typed question directly, bypassing STT."""
        if self.is_session_live():
            return {
                "status": "blocked",
                "message": "Live session running: use your wheel push-to-talk",
                "session_live": True,
            }

        clean_text = (text or "").strip()
        if not clean_text:
            return {"status": "error", "message": "Text question cannot be empty"}

        return await self.process_transmission(audio=None, typed_text=clean_text)

    async def process_transmission(
        self,
        audio: Optional[np.ndarray] = None,
        typed_text: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Executes full transmission pipeline: audio -> STT -> route() -> answer/AI -> TTS -> radio FX -> playback."""
        t_release = time.monotonic()

        input_dev_name = (
            getattr(self.capture, "device", None)
            or getattr(self.config.audio, "input_device", None)
            or "Default Microphone"
        )
        output_dev_name = (
            getattr(self.player, "device", None)
            or getattr(self.config.audio, "output_device", None)
            or "Default Output"
        )
        tts_engine = getattr(self.config.tts, "engine", "piper")

        diag: Dict[str, Any] = {
            "mic": {
                "ok": True,
                "status": "pass",
                "device_name": str(input_dev_name),
                "peak": 0.0,
                "peak_db": -100.0,
                "clipping": False,
                "message": "Mic OK",
            },
            "stt": {
                "ok": True,
                "status": "pass",
                "skipped": False,
                "transcript": "",
                "duration_ms": 0.0,
                "message": "",
            },
            "routing": {
                "ok": True,
                "status": "pass",
                "kind": "",
                "intent": None,
                "provider": None,
                "fallback": None,
                "message": "",
            },
            "reply": {
                "ok": True,
                "status": "pass",
                "text": "",
                "ttft_ms": None,
                "total_ms": None,
                "message": "",
            },
            "tts": {
                "ok": True,
                "status": "pass",
                "engine": str(tts_engine),
                "duration_ms": 0.0,
                "message": "",
            },
            "playback": {
                "ok": True,
                "status": "pass",
                "device_name": str(output_dev_name),
                "fallback_info": None,
                "message": f"Played on {output_dev_name}",
            },
            "total_turnaround_ms": 0.0,
        }

        # ------------------------------------------------------------------
        # 1. Microphone Evaluation & 2. Speech-to-Text
        # ------------------------------------------------------------------
        transcript = ""
        is_typed = bool(typed_text)

        if is_typed:
            transcript = typed_text.strip()
            diag["mic"]["message"] = "Bypassed (typed question)"
            diag["stt"]["skipped"] = True
            diag["stt"]["transcript"] = transcript
            diag["stt"]["message"] = f'Skipped (typed text: "{transcript}")'
        else:
            if audio is None or len(audio) == 0:
                diag["mic"]["ok"] = False
                diag["mic"]["status"] = "fail"
                diag["mic"]["message"] = "Mic silent: check the input device or Windows microphone permission"
                diag["stt"]["ok"] = False
                diag["stt"]["status"] = "fail"
                diag["stt"]["message"] = "Couldn't make that out"
                diag["total_turnaround_ms"] = round((time.monotonic() - t_release) * 1000.0, 1)
                self._record_diagnostic(diag)
                return {"status": "ok", "diagnostic": diag, "checklist": self.checklist}

            audio = audio.astype(np.float32)
            peak = float(np.max(np.abs(audio)))
            rms = float(np.sqrt(np.mean(audio**2)))
            peak_db = 20.0 * math.log10(peak) if peak > 1e-5 else -100.0
            clipping = (peak >= 0.98) or (
                getattr(self.capture, "clipping_detected", False) if self.capture else False
            )

            diag["mic"]["peak"] = round(peak, 4)
            diag["mic"]["peak_db"] = round(peak_db, 1)
            diag["mic"]["clipping"] = clipping

            if peak < 0.005:
                # Silent mic failure
                diag["mic"]["ok"] = False
                diag["mic"]["status"] = "fail"
                diag["mic"]["message"] = "Mic silent: check the input device or Windows microphone permission"
                diag["stt"]["ok"] = False
                diag["stt"]["status"] = "fail"
                diag["stt"]["message"] = "Couldn't make that out"
                diag["total_turnaround_ms"] = round((time.monotonic() - t_release) * 1000.0, 1)
                self._record_diagnostic(diag)
                return {"status": "ok", "diagnostic": diag, "checklist": self.checklist}

            if clipping:
                diag["mic"]["message"] = f"Peak: {peak_db:.1f} dB (Warning: microphone clipped! Reduce mic gain)"
            else:
                diag["mic"]["message"] = f"Mic OK (Peak: {peak_db:.1f} dB)"

            # Run STT transcription
            t0_stt = time.monotonic()
            if self.stt:
                try:
                    active_st = self.demo_state if (self.demo_mode and self.demo_state) else (self.state or RaceState())
                    c_names = (
                        self.engineer.get_corner_names()
                        if self.engineer and hasattr(self.engineer, "get_corner_names")
                        else []
                    )
                    prompt = build_initial_prompt(names=active_st.names, corner_names=c_names)
                    transcript = await asyncio_to_thread(self.stt.transcribe, audio, prompt)
                except Exception as e:
                    log.warning("Radio test STT failed: %s", e)
                    transcript = ""

            stt_ms = round((time.monotonic() - t0_stt) * 1000.0, 1)
            diag["stt"]["duration_ms"] = stt_ms
            transcript = (transcript or "").strip()
            diag["stt"]["transcript"] = transcript

            if not transcript:
                diag["stt"]["ok"] = False
                diag["stt"]["status"] = "fail"
                diag["stt"]["message"] = "Couldn't make that out"
                diag["total_turnaround_ms"] = round((time.monotonic() - t_release) * 1000.0, 1)
                self._record_diagnostic(diag)
                return {"status": "ok", "diagnostic": diag, "checklist": self.checklist}

            diag["stt"]["message"] = f'Transcript: "{transcript}" ({stt_ms:.0f} ms)'

        # ------------------------------------------------------------------
        # Log Driver Transmission to Radio Log with source "radio_test"
        # ------------------------------------------------------------------
        if self.broadcaster:
            self.broadcaster.broadcast_radio("driver", transcript, source="radio_test")

        # ------------------------------------------------------------------
        # 3. Routing & Answering
        # ------------------------------------------------------------------
        active_state = self.demo_state if (self.demo_mode and self.demo_state) else (self.state or RaceState())
        reply = route(transcript, active_state)
        reply_text = ""

        if reply.kind == "answer":
            diag["routing"]["kind"] = "deterministic"
            diag["routing"]["intent"] = reply.intent
            diag["routing"]["message"] = f"Intent: {reply.intent}"

            if reply.intent == "radio_check":
                reply_text = RadioCheckManager.build_voice_reply(active_state, self.personality)
            else:
                reply_text = reply.text or "No data on that yet."
            diag["reply"]["text"] = reply_text
            diag["reply"]["message"] = reply_text
        else:
            diag["routing"]["kind"] = "ai"
            primary_name = getattr(self.config.llm, "primary", "groq")
            diag["routing"]["provider"] = primary_name

            # Check Usage Guard for AI calls (prompt 13)
            if self.engineer and hasattr(self.engineer, "usage_guard") and self.engineer.usage_guard:
                try:
                    allowed, reason = self.engineer.usage_guard.check_allowed(primary_name)
                    if not allowed:
                        log.warning("UsageGuard blocked AI request in radio test: %s", reason)
                except Exception as e:
                    log.debug("Usage guard check error: %s", e)

            # Query AI Agent with diagnostics
            snapshot = active_state.snapshot() if getattr(active_state, "has_telemetry", False) else {}
            k_text = (
                self.engineer.get_knowledge_text(transcript)
                if self.engineer and hasattr(self.engineer, "get_knowledge_text")
                else ""
            )

            if self.agent and hasattr(self.agent, "answer_with_diagnostics"):
                ai_res = await self.agent.answer_with_diagnostics(transcript, snapshot, k_text)
                reply_text = ai_res.get("reply_text", "Copy, standing by.")
                diag["reply"]["ttft_ms"] = ai_res.get("ttft_ms")
                diag["reply"]["total_ms"] = ai_res.get("total_ms")

                p_used = ai_res.get("provider", primary_name)
                diag["routing"]["provider"] = p_used
                if ai_res.get("fallback_used"):
                    fb_name = ai_res.get("fallback_name", "fallback")
                    diag["routing"]["fallback"] = fb_name
                    diag["routing"]["message"] = f"AI ({p_used} - Fallback from {primary_name})"
                else:
                    diag["routing"]["message"] = f"AI ({p_used})"
            else:
                reply_text = "Copy, standing by."
                diag["routing"]["message"] = f"AI ({primary_name})"

            diag["reply"]["text"] = reply_text
            diag["reply"]["message"] = reply_text

        # ------------------------------------------------------------------
        # 4. Demo Mode First-Reply Prefix
        # ------------------------------------------------------------------
        if self.demo_mode and not self.demo_announced_first_reply:
            reply_text = "Demo data. " + reply_text
            self.demo_announced_first_reply = True
            diag["reply"]["text"] = reply_text
            diag["reply"]["message"] = reply_text

        # ------------------------------------------------------------------
        # 5. Text-To-Speech (TTS)
        # ------------------------------------------------------------------
        synth_audio: Optional[np.ndarray] = None
        sr = 16000
        t0_tts = time.monotonic()

        if self.tts:
            try:
                synth_audio, sr = await self.tts.synth(reply_text)
                tts_ms = round((time.monotonic() - t0_tts) * 1000.0, 1)
                diag["tts"]["duration_ms"] = tts_ms
                engine_name = self.tts.__class__.__name__.replace("TTS", "").replace("Provider", "")
                diag["tts"]["engine"] = engine_name
                diag["tts"]["message"] = f"Synthesized in {tts_ms:.0f} ms ({engine_name})"
            except Exception as e:
                log.warning("Radio test TTS synthesis failed: %s", e)
                diag["tts"]["ok"] = False
                diag["tts"]["status"] = "fail"
                diag["tts"]["message"] = f"Synthesis failed: {e}"
        else:
            diag["tts"]["ok"] = False
            diag["tts"]["status"] = "fail"
            diag["tts"]["message"] = "TTS engine unavailable"

        # ------------------------------------------------------------------
        # 6. Radio FX & Playback
        # ------------------------------------------------------------------
        total_turnaround_ms = round((time.monotonic() - t_release) * 1000.0, 1)
        diag["total_turnaround_ms"] = total_turnaround_ms

        if synth_audio is not None and len(synth_audio) > 0:
            if getattr(self.config.tts, "radio_fx", True):
                try:
                    synth_audio = apply_radio_fx(synth_audio, sr, enable_clicks=True)
                except Exception as e:
                    log.debug("Radio FX error: %s", e)

            eff_vol = getattr(self.config.volumes, "engineer", 1.0)
            if eff_vol != 1.0:
                synth_audio = synth_audio * eff_vol

            if self.player:
                try:
                    # Test output device validity
                    dev_spec = getattr(self.player, "device", None)
                    resolved_out, out_warn = resolve_device(dev_spec, kind="output")
                    if out_warn:
                        diag["playback"]["ok"] = False
                        diag["playback"]["status"] = "fail"
                        diag["playback"]["fallback_info"] = out_warn
                        diag["playback"]["message"] = "Output device missing: falling back to default"

                    await asyncio_to_thread(self.player.play_direct, synth_audio, sr)
                    if diag["playback"]["ok"]:
                        diag["playback"]["message"] = f"Played on {self.player.device or 'Default Output'}"
                except Exception as e:
                    log.warning("Radio test playback error: %s", e)
                    diag["playback"]["ok"] = False
                    diag["playback"]["status"] = "fail"
                    diag["playback"]["message"] = f"Output device missing: {e}"
            else:
                diag["playback"]["ok"] = False
                diag["playback"]["status"] = "fail"
                diag["playback"]["message"] = "Output device missing: Audio player unavailable"
        else:
            if diag["tts"]["status"] != "fail":
                diag["playback"]["status"] = "skipped"
                diag["playback"]["message"] = "No audio generated to play"

        # ------------------------------------------------------------------
        # Log Engineer Transmission to Radio Log with source "radio_test"
        # ------------------------------------------------------------------
        if self.broadcaster:
            self.broadcaster.broadcast_radio(
                "engineer",
                reply_text,
                latency_ms=total_turnaround_ms,
                source="radio_test",
            )

        # ------------------------------------------------------------------
        # 7. Evaluate Guided Checklist
        # ------------------------------------------------------------------
        self._evaluate_guided_checklist(transcript, diag)

        self._record_diagnostic(diag)
        return {
            "status": "ok",
            "diagnostic": diag,
            "checklist": self.checklist,
        }

    def _evaluate_guided_checklist(self, transcript: str, diag: Dict[str, Any]) -> None:
        """Updates guided checklist automatically when expected results match."""
        t_lower = (transcript or "").lower()
        reply_lower = (diag.get("reply", {}).get("text") or "").lower()
        intent = diag.get("routing", {}).get("intent")
        kind = diag.get("routing", {}).get("kind")
        stt_skipped = diag.get("stt", {}).get("skipped", False)

        for item in self.checklist:
            c_id = item["id"]
            if c_id == "radio_check":
                if ("radio check" in t_lower or "comms check" in t_lower or intent == "radio_check") and (
                    "loud and clear" in reply_lower
                ):
                    item["passed"] = True
            elif c_id == "gap_ahead":
                if ("gap ahead" in t_lower or "in front" in t_lower or intent == "gap_ahead") and (
                    "no telemetry" in reply_lower
                    or "can't see the gaps" in reply_lower
                    or "ahead" in reply_lower
                    or "leading" in reply_lower
                ):
                    item["passed"] = True
            elif c_id == "tyres":
                if ("tyre" in t_lower or "tire" in t_lower or "wear" in t_lower or intent == "tyres") and (
                    "no car data" in reply_lower
                    or "percent wear" in reply_lower
                    or "compound" in reply_lower
                    or "laps old" in reply_lower
                ):
                    item["passed"] = True
            elif c_id == "ai_question":
                if kind == "ai" and diag.get("reply", {}).get("ok") and len(reply_lower) > 5:
                    item["passed"] = True
            elif c_id == "typed_question":
                if stt_skipped and diag.get("reply", {}).get("ok") and len(reply_lower) > 5:
                    item["passed"] = True

    def _record_diagnostic(self, diag: Dict[str, Any]) -> None:
        """Stores latest diagnostic and broadcasts to dashboard clients."""
        self.latest_diagnostics = diag
        if self.broadcaster:
            self.broadcaster.broadcast(
                "radio_test_diagnostic",
                {
                    "diagnostic": diag,
                    "checklist": self.checklist,
                },
            )

    def get_state(self) -> Dict[str, Any]:
        """Returns current radio test bench state for frontend initialization."""
        return {
            "page_open": self.is_page_open,
            "demo_mode": self.demo_mode,
            "session_live": self.is_session_live(),
            "checklist": self.checklist,
            "latest_diagnostics": self.latest_diagnostics,
            "joystick_button": getattr(self.config.radio_test, "joystick_button", None),
        }


def start_radio_test_joystick_listener(
    manager: RadioTestManager,
    joystick_index: int = 0,
    button_index: int = 0,
) -> Optional[threading.Thread]:
    """Background listener for controller button PTT on the Radio test page."""
    os.environ["SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS"] = "1"

    try:
        import pygame
    except ImportError:
        log.warning("pygame is not installed; radio test controller PTT unavailable")
        return None

    def _poll_joystick():
        try:
            pygame.init()
            pygame.joystick.init()
            count = pygame.joystick.get_count()
            if count <= joystick_index:
                log.info("Radio test: Joystick index %d not connected (count=%d)", joystick_index, count)
                return

            joy = pygame.joystick.Joystick(joystick_index)
            joy.init()
            log.info("Radio test controller listener active: '%s', button %d", joy.get_name(), button_index)

            last_state = False
            while True:
                time.sleep(0.015)
                # Only active while the Radio test page is open and no live session is running
                if not manager.is_page_open or manager.is_session_live():
                    if last_state:
                        last_state = False
                        try:
                            loop = asyncio.get_event_loop()
                            if loop.is_running():
                                loop.create_task(manager.stop_ptt())
                            else:
                                asyncio.run(manager.stop_ptt())
                        except Exception:
                            pass
                    continue

                pygame.event.pump()
                current_state = bool(joy.get_button(button_index))
                if current_state != last_state:
                    last_state = current_state
                    if current_state:
                        manager.start_ptt()
                    else:
                        try:
                            # Run stop_ptt asynchronously
                            if manager.engineer and getattr(manager.engineer, "loop", None):
                                asyncio.run_coroutine_threadsafe(manager.stop_ptt(), manager.engineer.loop)
                            else:
                                asyncio.run(manager.stop_ptt())
                        except Exception as e:
                            log.debug("Controller stop_ptt error: %s", e)
        except Exception as e:
            log.debug("Radio test controller polling terminated: %s", e)
        finally:
            try:
                pygame.quit()
            except Exception:
                pass

    t = threading.Thread(target=_poll_joystick, daemon=True, name="RadioTestJoystick")
    t.start()
    return t


async def asyncio_to_thread(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Asyncio runner helper for synchronous functions."""
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except AttributeError:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))
