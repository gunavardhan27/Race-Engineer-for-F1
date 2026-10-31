"""Race engineer runtime: UDP in -> state + callouts -> radio queue out.

    python main.py                              # listen on 127.0.0.1:20777
    python main.py --config config.yaml         # load specific configuration
    python main.py --list-devices               # print audio devices and exit
    python main.py --selftest                   # run synthetic end-to-end selftest
    python main.py --record session.bin         # also save raw packets for offline dev
    python main.py --replay session.bin --speed 4
    python main.py --redis redis://localhost:6379/0

Type a question and press Enter to test routing without a mic.
STT, TTS and the AI agent plug in at the three marked hooks.
"""
from __future__ import annotations

import argparse
import asyncio
import faulthandler
faulthandler.enable()
import hashlib
import json
import logging
import os
import signal
import socket
import struct
import sys
import threading
import time
from pathlib import Path

import numpy as np

from single_instance import SingleInstanceLock
from port_checker import format_port_conflict_message
from audio_cache import AudioCacheManager

from agent import RaceEngineerAgent
from audio_devices import print_audio_devices
from audio_in import AudioCapture, start_joystick_listener
from audio_player import AudioPlayer
from config import AppConfig, load_config
from f1telemetry import packets
from f1telemetry.deterministic import Callout, Callouts, RadioQueue, route
from f1telemetry.spotter import Spotter, SPOTTER_PHRASES, SpotterState
from f1telemetry.state import RaceState, TRACKS, LapRecord, F1_RACE_POINTS, SESSION_TYPES
from f1telemetry.overtakes import BrakingZoneDetector, OvertakeDetector, DefendWarningDetector
from trackmodel import TrackModel, TrackModelBuilder, TrackModelStorage
from knowledge import KnowledgeBase
from latency import LatencyTracker
from llm import LLMClient
from pace import PaceAnalyzer
from personality import PersonalityEngine
from radio_check import RadioCheckManager
from radio_fx import apply_radio_fx
from sound_test import SoundTestManager
from stt import SpeechToText, build_initial_prompt
from tts import TTSProvider, create_tts_engine
from storage import SessionStorage, LapTraceCollector
from analysis import get_lap_comparison_summary, compute_lap_debrief
from setups import SetupsStorage, import_from_json, import_from_csv, export_as_sheet, export_as_json, validate_setup_params
from usage import UsageGuard
from seed_import import SeedImporter
from minisectors import MiniSectorTracker, MiniSectorStorage
from trackmap import TrackMapManager
from positions import PositionsBroadcaster, encode_positions_frame, extract_car_positions
from replay_manager import ReplayController
from tyres import TyreDegradationModel
from strategy import StrategyPredictor, StrategyPlan, UndercutOpportunity

log = logging.getLogger("engineer")
FRAME = struct.Struct("<dI")   # recording: seconds since start, payload length


class ExplainTracker:
    """Tracks and formats per-lap detected events during replay without audio."""

    def __init__(self):
        self.events_by_lap: dict[int, list[dict[str, Any]]] = {}
        self.resets: list[str] = []
        self.current_lap: int = 1

    def _get_lap_list(self, lap: int) -> list[dict[str, Any]]:
        if lap not in self.events_by_lap:
            self.events_by_lap[lap] = []
        return self.events_by_lap[lap]

    def on_reset(self, old_uid, new_uid):
        msg = f"{old_uid} -> {new_uid}"
        self.resets.append(msg)
        self._get_lap_list(self.current_lap).append({"type": "SESSION_RESET", "text": f"Session reset: {msg}"})

    def on_lap(self, rec):
        t_sec = (rec.lap_time_ms / 1000.0) if getattr(rec, "lap_time_ms", None) else 0.0
        self._get_lap_list(self.current_lap).append({
            "type": "LAP",
            "text": f"Lap {rec.lap} finished ({t_sec:.3f}s, valid={not rec.invalid}, P{rec.position})"
        })
        self.current_lap = rec.lap + 1

    def on_overtake(self, ot):
        lap = ot.get("lap_num", self.current_lap)
        d_name = ot.get("other_driver_name") or f"Car {ot.get('other_vehicle_idx', ot.get('car_idx'))}"
        direction = ot.get("direction", "pass")
        move = "Pass on" if direction == "pass" else "Overtaken by"
        cls_name = ot.get("classification", "clean")
        corner = ot.get("corner_name") or "straight"
        b_delta = ot.get("braking_delta_m")
        s_gap = ot.get("start_gap_m")
        feats = []
        if b_delta is not None:
            feats.append(f"brake_delta={b_delta:+.1f}m")
        if s_gap is not None:
            feats.append(f"start_gap={s_gap:.1f}m")
        if ot.get("overlap_at_turn_in"):
            feats.append("overlap=yes")
        if ot.get("ran_deep"):
            feats.append("ran_deep=yes")
        if ot.get("contact"):
            feats.append("contact=yes")
        if ot.get("forced_off"):
            feats.append("forced_off=yes")
        if ot.get("switchback"):
            feats.append("switchback=yes")
        feat_str = f" [{', '.join(feats)}]" if feats else ""
        text = f"{move} {d_name} | class: {cls_name} | corner: {corner}{feat_str}"
        self._get_lap_list(lap).append({"type": "OVERTAKE" if direction == "pass" else "OVERTAKEN", "text": text, "data": ot})

    def on_defend(self, df):
        lap = self.current_lap
        d_name = df.get("driver") or f"Car {df.get('car_idx')}"
        corner = df.get("corner") or "corner"
        look = "Inside Look" if df.get("inside_look") else "Defend Warning"
        d_fwd = df.get("d_forward")
        text = f"{look} into {corner} vs {d_name} (gap: {d_fwd}m)"
        self._get_lap_list(lap).append({"type": "DEFEND", "text": text, "data": df})

    def on_finish(self, info):
        lap = self.current_lap
        pos = info.get("position", 1)
        behind_s = (info.get("gap_behind_ms") / 1000.0) if info.get("gap_behind_ms") is not None else None
        ahead_s = (info.get("gap_ahead_ms") / 1000.0) if info.get("gap_ahead_ms") is not None else None
        pen = info.get("penalties_s", 0)
        text = f"P{pos} across line | Margin behind: {behind_s}s | Gap ahead: {ahead_s}s | Penalties: {pen}s"
        self._get_lap_list(lap).append({"type": "FINISH", "text": text, "data": info})

    def on_penalty(self, ev):
        lap = self.current_lap
        pname = packets.PENALTY_TYPES.get(ev.penalty_type, f"type_{ev.penalty_type}")
        iname = packets.INFRINGEMENT_TYPES.get(ev.infringement_type, f"infringement_{ev.infringement_type}")
        text = f"{pname} ({iname}), time={ev.penalty_time}s"
        self._get_lap_list(lap).append({"type": "PENALTY", "text": text, "data": ev})

    def on_invalidation(self):
        lap = self.current_lap
        self._get_lap_list(lap).append({"type": "INVALIDATION", "text": "Lap invalidated (track limits)"})

    def on_warning(self, count: int):
        lap = self.current_lap
        self._get_lap_list(lap).append({"type": "WARNING", "text": f"Track limits warning {count}"})

    def print_report(self, path: str):
        print("\n" + "=" * 80)
        print(f"EXPLAIN REPLAY REPORT: {path}")
        print("=" * 80)
        if self.resets:
            print("Session Resets:")
            for r in self.resets:
                print(f"  • {r}")
        print("\nPer-Lap Detected Events:")
        if not self.events_by_lap:
            print("  (No events detected in recording)")
        else:
            for lap in sorted(self.events_by_lap.keys()):
                print(f"\n--- LAP {lap} ---")
                for ev in self.events_by_lap[lap]:
                    tag = f"[{ev['type']}]"
                    print(f"  {tag:<14} {ev['text']}")
        print("\n" + "=" * 80 + "\n")


class Recorder:
    def __init__(self, path):
        self.f = open(path, "wb")
        self.t0 = time.monotonic()

    def write(self, buf):
        self.f.write(FRAME.pack(time.monotonic() - self.t0, len(buf)) + buf)

    def close(self):
        self.f.close()


class Engineer:
    def __init__(
        self,
        ptt_action: int = 1,
        recorder: Recorder | None = None,
        config: AppConfig | None = None,
        latency: LatencyTracker | None = None,
        stt: SpeechToText | None = None,
        audio_capture: AudioCapture | None = None,
        agent: RaceEngineerAgent | None = None,
        tts: TTSProvider | None = None,
        player: AudioPlayer | None = None,
        knowledge: KnowledgeBase | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
        broadcaster: Any | None = None,
        storage: SessionStorage | None = None,
        setups_storage: SetupsStorage | None = None,
        personality: PersonalityEngine | None = None,
        usage_guard: UsageGuard | None = None,
        config_path: str = "config.yaml",
    ):
        self.config = config or AppConfig()
        self.config_path = config_path
        self.usage_guard = usage_guard
        self.state = RaceState(ptt_action=ptt_action)
        self.callouts = Callouts(debrief_config=self.config.debrief)
        self.spotter = Spotter(self.config.spotter)
        self.spotter_audio: dict[str, tuple[np.ndarray, int]] = {}
        self.personality = personality or PersonalityEngine(config=self.config.persona)
        self.radio = RadioQueue()
        self.recorder = recorder
        self.latency = latency or LatencyTracker()
        self.stt = stt
        self.audio_capture = audio_capture
        self.agent = agent
        self.tts = tts
        self.player = player
        self.knowledge = knowledge or KnowledgeBase(config=self.config.knowledge)
        self.loop = loop
        self.broadcaster = broadcaster
        self.storage = storage or SessionStorage()
        self.setups_storage = setups_storage or SetupsStorage()
        self.trace_collector = LapTraceCollector(self.storage)
        self.pace = PaceAnalyzer(config=self.config, state=self.state, personality=self.personality, storage=self.storage)
        self._agent_task: asyncio.Task | None = None
        self._joystick_thread = None

        self.radio_check = RadioCheckManager(config=self.config.radio_check)
        self.quiet_mode: bool = False
        self.sound_test = SoundTestManager(
            tts=self.tts,
            player=self.player,
            capture=self.audio_capture,
            stt=self.stt,
            config=self.config,
            is_session_live_fn=self.is_session_live,
            config_path=self.config_path,
        )

        self.braking_zones = BrakingZoneDetector(knowledge_base=self.knowledge)
        self.track_model_storage = TrackModelStorage()
        self.track_model: TrackModel | None = None
        self.track_model_builder: TrackModelBuilder | None = None
        self._last_model_build_time: float = 0.0

        self.overtake_detector = OvertakeDetector(
            config=self.config.overtake,
            storage=self.storage,
            braking_zones=self.braking_zones,
            track_model=self.track_model,
        )
        self.defend_detector = DefendWarningDetector(
            config=self.config.defend,
            braking_zones=self.braking_zones,
            spotter=self.spotter,
            track_model=self.track_model,
        )
        self.tracker: ExplainTracker | None = None
        self.stopping: bool = False
        self.engine_error: str | None = None
        self._stop_event: asyncio.Event | None = None
        self.track_map = TrackMapManager()
        self.positions_broadcaster = PositionsBroadcaster()
        self._last_pos_time: float = 0.0
        self.replay_controller = ReplayController(self)
        self.seed_importer = SeedImporter()
        try:
            st = self.seed_importer.get_status()
            if st.get("setups_total", 0) == 0:
                log.info("Importing initial seed reference data (Prompt 25)...")
                self.seed_importer.import_all(reimport=False)
        except Exception as e:
            log.warning("Could not auto-import seed data at startup: %s", e)

        db_path = getattr(self.storage, "db_path", "storage.db") if self.storage else "storage.db"
        self.minisector_tracker = MiniSectorTracker(storage=MiniSectorStorage(db_path))

        voice_str = getattr(self.config.tts, "voice", "en-GB-RyanNeural")
        max_mb = getattr(self.config.cache, "audio_max_mb", 100.0) if hasattr(self.config, "cache") else 100.0
        self.audio_cache = getattr(self.tts, "cache_manager", None) or AudioCacheManager(
            voice=voice_str,
            max_mb=max_mb,
        )

        self.tyre_model = TyreDegradationModel()
        self.strategy_predictor = StrategyPredictor(deg_model=self.tyre_model)

        if self.config.stt.ptt_source == "joystick":
            self._joystick_thread = start_joystick_listener(
                self.on_ptt,
                joystick_index=self.config.stt.joystick_index,
                button_index=self.config.stt.joystick_button,
            )

    def request_shutdown(self):
        """Signals graceful shutdown across console and desktop GUI."""
        self.stopping = True
        if self._stop_event:
            if self.loop and self.loop.is_running():
                self.loop.call_soon_threadsafe(self._stop_event.set)
            else:
                self._stop_event.set()

    async def stop(self) -> None:
        """Stops and cleans up all Engineer-owned resources and background tasks."""
        self.stopping = True
        if self._agent_task and not self._agent_task.done():
            self._agent_task.cancel()
        if self.player:
            try:
                self.player.stop()
            except Exception:
                pass
        if self.audio_capture:
            try:
                self.audio_capture.stop()
            except Exception:
                pass
        if self.storage:
            try:
                self.storage.close()
            except Exception:
                pass
        if self.recorder:
            try:
                self.recorder.close()
            except Exception:
                pass

    def _reload_session_if_resumed(self):
        """Reload the current session from the database on app restart if the same session_uid resumes."""
        if not self.storage or not self.state.session_uid:
            return
        try:
            sess = self.storage.get_session_by_uid(self.state.session_uid)
            if sess:
                db_laps = self.storage.get_laps_for_session(sess["id"])
                if db_laps and not self.state.laps:
                    for dl in db_laps:
                        rec = LapRecord(
                            lap=dl["lap_num"],
                            lap_time_ms=int(round(dl["lap_time_s"] * 1000)),
                            invalid=not bool(dl["is_valid"]),
                            position=dl.get("position", 0),
                            ahead_idx=None,
                            gap_ahead_ms=None,
                            behind_idx=None,
                            gap_behind_ms=None,
                            fuel_kg=dl.get("fuel_end_kg"),
                            tyre_wear=None,
                            stint=dl.get("stint", 0),
                            sector1_ms=int(round((dl.get("sector1_s") or 0) * 1000)),
                            sector2_ms=int(round((dl.get("sector2_s") or 0) * 1000)),
                            sector3_ms=int(round((dl.get("sector3_s") or 0) * 1000)),
                        )
                        self.state.laps.append(rec)
                    log.info("Resumed session %s: reloaded %d laps from database", self.state.session_uid, len(db_laps))
                    self._update_lap_analysis()
                # Load reference lap into braking_zones
                if self.state.session and self.state.session.track_id is not None:
                    pb = self.storage.get_personal_best(self.state.session.track_id, self.state.session.formula)
                    if pb:
                        trace = self.storage.get_lap_trace(pb["id"])
                        if trace:
                            self.braking_zones.update_from_trace(self.state.session.track_id, pb["lap_time_s"], trace)
        except Exception as e:
            log.debug("Session resumption reload failed: %s", e)

    def is_session_live(self) -> bool:
        """Returns True if telemetry packets from an active session are arriving."""
        return bool(
            self.state.last_packet_time > 0
            and (time.monotonic() - self.state.last_packet_time) < 4.0
            and self.state.session is not None
        )

    def start_two_way_radio_check(self) -> str:
        """Initiates two-way in-game radio check."""
        return self.radio_check.start_two_way_check(say_fn=self.say)

    async def prerender_spotter(self):
        """Pre-renders spotter audio clips to bypass TTS latency during wheel-to-wheel battles."""
        if not self.tts:
            return
        log.info("Pre-rendering spotter phrases for instant zero-latency playback...")
        self.audio_cache.register_static_phrases(SPOTTER_PHRASES)
        for phrase in SPOTTER_PHRASES:
            cached = self.audio_cache.load(phrase)
            if cached is not None:
                self.spotter_audio[phrase] = cached
                continue
            try:
                audio, sr = await self.tts.synth(phrase)
                if self.config.tts.radio_fx:
                    audio = apply_radio_fx(audio, sr, enable_clicks=True)
                self.audio_cache.save(phrase, audio, sr)
                self.spotter_audio[phrase] = (audio, sr)
            except Exception as e:
                log.warning("Failed to pre-render spotter phrase %r: %s", phrase, e)
        log.info("Pre-rendered %d spotter phrases", len(self.spotter_audio))

    async def prerender_persona_lines(self):
        """Pre-renders static persona lines (no placeholders) to disk and memory cache for zero latency."""
        if not self.tts or not self.personality or not self.personality.lines:
            return
        log.info("Pre-rendering static persona lines...")
        static_texts = [
            line.text for lines in self.personality.lines.values()
            for line in lines if not line.has_placeholders
        ]
        self.audio_cache.register_static_phrases(static_texts)
        self.audio_cache.cleanup_orphans(static_texts)

        for text in static_texts:
            cached = self.audio_cache.load(text)
            if cached is not None:
                self.personality.prerendered_audio[text] = cached
                continue
            try:
                audio, sr = await self.tts.synth(text)
                if self.config.tts.radio_fx:
                    audio = apply_radio_fx(audio, sr, enable_clicks=True)
                self.audio_cache.save(text, audio, sr)
                self.personality.prerendered_audio[text] = (audio, sr)
            except Exception as e:
                log.warning("Failed to pre-render persona line %r: %s", text, e)
        log.info("Pre-rendered %d static persona lines", len(self.personality.prerendered_audio))

    def _play_spotter(self, phrase: str):
        if self.broadcaster:
            self.broadcaster.broadcast_radio("engineer", phrase)
        if phrase in self.spotter_audio and self.player:
            audio, sr = self.spotter_audio[phrase]
            eff_vol = getattr(self.config.volumes, "spotter", 1.0)
            if eff_vol != 1.0:
                audio = audio * eff_vol
            threading.Thread(target=self.player.play_direct, args=(audio, sr), daemon=True).start()
        elif self.player:
            self.radio.push(Callout(0, time.monotonic(), phrase, ttl=self.config.spotter.ttl_s))
        else:
            print(f"[SPOTTER] {phrase}", flush=True)

    def _on_final_classification(self, cars):
        """Builds session summary and triggers post-session agent message in persona."""
        p = self.state.player
        if not cars or p >= len(cars):
            return
        fc = cars[p]
        pos = fc.position
        grid_pos = 0
        if self.state.laps:
            grid_pos = self.state.laps[0].grid_position
        elif self.state.me:
            grid_pos = self.state.me.grid_position

        gained = (grid_pos - pos) if grid_pos > 0 else 0

        best_lap_s = None
        if self.state.personal_best:
            best_lap_s = self.state.personal_best.get("lap_time_s")

        is_practice = getattr(self.state, "is_practice", False)
        is_quali = getattr(self.state, "is_quali", False)
        session_type = "practice" if is_practice else ("qualifying" if is_quali else "race")
        points = self.state.get_points(pos) if hasattr(self.state, "get_points") else (
            F1_RACE_POINTS.get(pos, 0) if session_type == "race" else 0
        )

        summary = {
            "session_type": session_type,
            "session_name": SESSION_TYPES.get(getattr(self.state.session, "session_type", 0), session_type.title()),
            "finish_position": pos,
            "grid": grid_pos if session_type == "race" else None,
            "positions_gained": gained if session_type == "race" else None,
            "points": points,
            "fastest_lap": False,
            "penalties_s": self.state.me.penalties_s if self.state.me else 0,
            "best_lap": f"{best_lap_s:.2f}s" if best_lap_s else "N/A",
        }

        async def _run_post_race():
            msg = None
            if self.agent:
                try:
                    msg = await self.agent.generate_post_race_message(summary)
                except Exception as e:
                    log.warning("Agent post-session debrief failed: %s", e)
            if not msg and self.personality:
                if is_practice:
                    c_fallback = self.personality.trigger("practice_finish", self.state, {"pos": pos, "points": 0})
                elif is_quali:
                    evt = "quali_pole" if pos == 1 else "quali_finish"
                    c_fallback = self.personality.trigger(evt, self.state, {"pos": pos, "points": 0})
                else:
                    evt = "win" if pos == 1 else ("podium" if 2 <= pos <= 3 else ("points" if pos <= 10 else "no_points"))
                    c_fallback = self.personality.trigger(evt, self.state, {"pos": pos, "points": points})
                if c_fallback:
                    msg = c_fallback.text
            if not msg:
                if is_practice:
                    msg = f"P{pos} in practice. Good data gathered, bring it into the box."
                elif is_quali:
                    if pos == 1:
                        msg = "Pole position! Brilliant lap, you start on pole for the race!"
                    else:
                        msg = f"That's P{pos} for the grid. Solid qualifying session."
                else:
                    if pos == 1:
                        msg = "P1! You've won the race! 25 points, mega drive!"
                    elif 2 <= pos <= 3:
                        msg = f"P{pos} across the line! Podium finish, {points} points in the bag!"
                    elif pos <= 10:
                        pts_str = f"{points} points" if points != 1 else "1 point"
                        msg = f"P{pos} across the line. That's {pts_str} in the bag, solid drive."
                    else:
                        msg = f"P{pos} across the line. Outside the points, but good effort out there."

            self.radio.push(Callout(1, time.monotonic(), msg, ttl=15.0))

        if self.loop and self.loop.is_running():
            self.loop.create_task(_run_post_race())
        else:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(_run_post_race())
            except RuntimeError:
                asyncio.run(_run_post_race())

    def _on_lap_completed_debrief(self, rec):
        """Asynchronously calculates worst-corner analysis and queues debrief."""
        if not self.config.debrief.auto_lap_debrief or not self.storage or not self.state.session:
            return

        async def _run():
            try:
                s = self.state.session
                corners = self.knowledge.get_corner_names() if self.knowledge else []
                text = await asyncio.to_thread(
                    compute_lap_debrief,
                    self.storage,
                    self.state.session_uid,
                    s.track_id,
                    s.formula,
                    s.session_type,
                    rec.lap,
                    rec.lap_time_ms,
                    rec.invalid,
                    rec.stint,
                    corners,
                    self.config.debrief.reference_lap,
                    self.config.debrief.worst_corner_threshold_s,
                )
                self.callouts.queue_debrief(text, rec.lap)
                if self.personality and "that's your best lap" in text:
                    c_pb = self.personality.trigger("personal_best", self.state)
                    if c_pb:
                        self.radio.push(c_pb)
            except Exception as e:
                log.debug("Error computing lap debrief: %s", e)

        if self.loop and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(_run(), self.loop)
        else:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(_run())
            except RuntimeError:
                pass

    def _update_lap_analysis(self):
        """Updates personal_best and last_lap_vs_best in RaceState from storage."""
        if not self.storage or not self.state.session:
            return
        try:
            s = self.state.session
            corners = self.knowledge.get_corner_names() if self.knowledge else []
            sess = self.storage.get_session_by_uid(self.state.session_uid)
            laps = self.storage.get_laps_for_session(sess["id"]) if sess else []
            last_lap = laps[-1] if laps else None
            summary = get_lap_comparison_summary(
                self.storage,
                track_id=s.track_id,
                formula=s.formula,
                session_type=s.session_type,
                last_lap=last_lap,
                corner_names=corners,
            )
            self.state.personal_best = summary["personal_best"]
            self.state.last_lap_vs_best = summary["last_lap_vs_best"]
            self._update_minisector_analysis()
        except Exception as e:
            log.debug("Could not update lap analysis: %s", e)

    def _update_minisector_analysis(self):
        """Updates snapshot fields for mini-sectors, theoretical best, and biggest loss."""
        if not hasattr(self, "minisector_tracker") or not self.minisector_tracker:
            return
        if not self.state.session_uid or not self.state.session:
            return
        try:
            track_len = float(getattr(self.state, "track_length", 5000.0) or 5000.0)
            my_car = self.state.player
            uid = str(self.state.session_uid)

            # 1. Theoretical best for player
            ideal = self.minisector_tracker.get_theoretical_best(uid, my_car, track_len)
            self.state.my_theoretical_best = ideal

            # 2. Fastest sectors across session
            laps = self.minisector_tracker.storage.list_session_laps(uid, include_flagged=False)
            if laps:
                s_bests = {}
                for s_num, key in [(1, "s1_ms"), (2, "s2_ms"), (3, "s3_ms")]:
                    valid_s = [l for l in laps if l.get(key, 0) > 0]
                    if valid_s:
                        best_l = min(valid_s, key=lambda l: l[key])
                        driver_name = self.state.name(best_l["car_idx"])
                        s_bests[s_num] = {
                            "driver": driver_name,
                            "car_idx": best_l["car_idx"],
                            "time_ms": best_l[key],
                            "time_s": round(best_l[key] / 1000.0, 3),
                        }
                self.state.fastest_sectors = s_bests

            # 3. Biggest loss segment vs session fastest lap
            if laps:
                fastest_lap = min(laps, key=lambda l: l["lap_time_ms"])
                rec_fastest = self.minisector_tracker.storage.get_lap_timing(uid, fastest_lap["car_idx"], fastest_lap["lap_num"])
                my_laps = [l for l in laps if l["car_idx"] == my_car]
                if my_laps and rec_fastest:
                    my_best_lap = min(my_laps, key=lambda l: l["lap_time_ms"])
                    rec_my_best = self.minisector_tracker.storage.get_lap_timing(uid, my_car, my_best_lap["lap_num"])
                    if rec_my_best:
                        track_model = getattr(self, "track_model", None)
                        comp = self.minisector_tracker.compare_laps(
                            rec_my_best, rec_fastest, track_len, track_model=track_model,
                            driver_a_name=self.state.name(my_car),
                            driver_b_name=self.state.name(fastest_lap["car_idx"]),
                        )
                        if comp.get("top_losses"):
                            self.state.biggest_loss_segment = comp["top_losses"][0]
        except Exception as e:
            log.debug("Could not update minisector analysis: %s", e)

    def on_packet(self, buf: bytes):
        if self.stopping:
            return
        if self.recorder:
            self.recorder.write(buf)
        try:
            h, data = packets.parse(buf)
            if data is None:
                return
            for sig in self.state.update(h, data):
                now_ts = self.state.session_time if self.state.session_time > 0 else time.monotonic()
                if sig[0] == "ptt":
                    self.on_ptt(sig[1])
                elif sig[0] in ("new_session", "track_change"):
                    if sig[0] == "new_session":
                        self.pace.reset_session()
                        self.strategy_predictor.reset_session()
                        self._reload_session_if_resumed()
                        if self.tracker:
                            self.tracker.on_reset(self.state.session_uid, sig[1])
                        if self.state.my_setup:
                            self._on_setup_change(self.state.my_setup, self.state.current_setup_fingerprint)
                    t_id = sig[1] if sig[0] == "track_change" else (self.state.session.track_id if self.state.session else None)
                    if t_id is not None:
                        self._init_track_session(t_id)
                elif sig[0] == "setup_change":
                    self._on_setup_change(sig[1], sig[2])
                elif sig[0] == "lap":
                    rec = sig[1]
                    if self.tracker:
                        self.tracker.on_lap(rec)
                    if self.trace_collector:
                        self.trace_collector.on_lap_completed(rec, self.state)
                    self._update_lap_analysis()
                    if self.broadcaster:
                        self.broadcaster.broadcast("lap_minisectors_update", {
                            "car_idx": getattr(rec, "car_idx", self.state.player or 0),
                            "lap_num": rec.lap,
                            "theoretical_best": self.state.my_theoretical_best,
                            "biggest_loss": self.state.biggest_loss_segment,
                        })
                    self._on_lap_completed_debrief(rec)
                    self.pace.quiet_mode = self.quiet_mode
                    for c_pace in self.pace.on_lap(rec):
                        self.radio.push(c_pace)

                    # Strategy & Tyre Degradation simulation update (Prompt 24)
                    try:
                        s_sess = self.state.session
                        tot_l = s_sess.total_laps if s_sess and s_sess.total_laps else 52
                        c_stat = self.state.my_status
                        c_name = packets.COMPOUNDS.get(c_stat.visual_compound, "medium") if c_stat else "medium"
                        stint_laps = [r for r in self.state.laps if r.stint == rec.stint and not r.invalid]
                        if len(stint_laps) >= 3:
                            ages = [r.lap - stint_laps[0].lap + 1 for r in stint_laps]
                            times = [r.lap_time_s for r in stint_laps]
                            fuels = [r.fuel_kg for r in stint_laps if r.fuel_kg is not None]
                            wears = [max(r.tyre_wear) for r in stint_laps if r.tyre_wear]
                            self.tyre_model.feed_stint(
                                c_name, ages, times,
                                fuels if len(fuels) == len(times) else None,
                                wears if len(wears) == len(times) else None,
                            )

                        avail_comps = ["soft", "medium", "hard"]
                        if self.state.tyre_sets and hasattr(self.state.tyre_sets, "sets"):
                            avail_comps = [packets.COMPOUNDS.get(ts.visual_compound, "medium") for ts in self.state.tyre_sets.sets if getattr(ts, "available", False)] or ["medium", "hard"]

                        plans = self.strategy_predictor.enumerate_plans(
                            current_lap=rec.lap,
                            total_laps=tot_l,
                            current_compound=c_name,
                            current_tyre_age=c_stat.tyre_age_laps if c_stat else 1,
                            available_compounds=avail_comps,
                            base_lap_time=rec.lap_time_s or 90.0,
                            fuel_remaining_kg=c_stat.fuel_in_tank if c_stat else 40.0,
                            sc_status=s_sess.safety_car if s_sess else 0,
                        )
                        self.strategy_predictor.run_monte_carlo(plans, samples=200)

                        b = self.state.battle_summary(max_gap_s=4.0)
                        ahead = b.get("ahead")
                        behind = b.get("behind")
                        opp = self.strategy_predictor.evaluate_undercut_threat(
                            current_lap=rec.lap,
                            gap_ahead_s=ahead.get("gap_s") if ahead else None,
                            ahead_name=ahead.get("name") if ahead else None,
                            ahead_car_idx=ahead.get("car_idx") if ahead else None,
                            gap_behind_s=behind.get("gap_s") if behind else None,
                            behind_name=behind.get("name") if behind else None,
                            behind_car_idx=behind.get("car_idx") if behind else None,
                            current_position=self.state.me.position if self.state.me else 3,
                        )
                        if opp and opp.recommended_action == "box_now":
                            self.radio.push(Callout(0, time.monotonic(), opp.text, ttl=8.0))

                        # Evaluate proactive pit wall strategy calls (plan switches, window notices, box in-lap calls, cliff drop)
                        rival_behind_pitted = bool(behind and behind.get("is_pitting", False) and (behind.get("gap_s") or 99.0) <= 4.0)
                        proactive_alerts = self.strategy_predictor.evaluate_proactive_strategy(
                            current_lap=rec.lap,
                            total_laps=tot_l,
                            current_compound=c_name,
                            current_tyre_age=c_stat.tyre_age_laps if c_stat else 1,
                            tyre_wear=rec.tyre_wear,
                            ideal_lap=s_sess.pit_ideal_lap if s_sess else None,
                            latest_lap=s_sess.pit_latest_lap if s_sess else None,
                            rival_behind_pitted=rival_behind_pitted,
                            rival_behind_name=behind.get("name") if behind else None,
                            rival_behind_gap_s=behind.get("gap_s") if behind else None,
                        )
                        for alert in proactive_alerts:
                            call_text = alert["text"]
                            if self.personality:
                                pers_text = self.personality.get_reaction_text(
                                    alert["event"],
                                    alert.get("context", {}),
                                )
                                if pers_text:
                                    call_text = pers_text
                            self.radio.push(Callout(alert["priority"], time.monotonic(), call_text, ttl=10.0))

                        self.state.strategy_summary = self.strategy_predictor.summary()
                        if self.broadcaster:
                            self.broadcaster.broadcast("strategy_update", self.state.strategy_summary)
                    except Exception as e:
                        log.debug("Strategy calculation error on lap: %s", e)

                    # Update braking zones if clean reference lap
                    if not rec.invalid and self.storage and self.state.session and self.state.session.track_id is not None:
                        try:
                            sess = self.storage.get_session_by_uid(self.state.session_uid)
                            if sess:
                                laps_db = self.storage.get_laps_for_session(sess["id"])
                                if laps_db:
                                    last_l = laps_db[-1]
                                    trace = self.storage.get_lap_trace(last_l["id"])
                                    if trace:
                                        self.braking_zones.update_from_trace(self.state.session.track_id, rec.lap_time_s, trace)
                        except Exception as e:
                            log.debug("Failed updating braking zones from lap trace: %s", e)
                elif sig[0] == "flashback":
                    lap_num = sig[1]
                    if self.trace_collector:
                        self.trace_collector.on_flashback(lap_num, self.state.session_uid)
                elif sig[0] == "finished":
                    if self.tracker:
                        self.tracker.on_finish(sig[1])
                elif sig[0] == "penalty":
                    if self.tracker:
                        self.tracker.on_penalty(sig[1])
                elif sig[0] == "lap_invalidated":
                    if self.tracker:
                        self.tracker.on_invalidation()
                elif sig[0] == "track_limits_warning":
                    if self.tracker:
                        self.tracker.on_warning(sig[1])
                elif sig[0] == "event":
                    ev = sig[1]
                    if getattr(ev, "code", "") == "COLL":
                        v1 = getattr(ev, "vehicle_idx", None)
                        v2 = getattr(ev, "other_vehicle_idx", None)
                        if v1 is None: v1 = getattr(ev, "vehicle_1_idx", None)
                        if v2 is None: v2 = getattr(ev, "vehicle_2_idx", None)
                        if v1 is not None and v2 is not None:
                            self.overtake_detector.on_collision(v1, v2, now=now_ts)

                for c in self.callouts.process(sig, self.state):
                    self.radio.push(c)

                # Personality reaction check (overrides: never replaces neutral calls; no reaction on active spotter/PTT)
                if self.personality:
                    spotter_active = (self.spotter.state != SpotterState.CLEAR) if self.spotter else False
                    c_pers = self.personality.on_signal(sig, self.state, spotter_active=spotter_active, now=now_ts)
                    if c_pers:
                        self.radio.push(c_pers)

                # Post-race trigger on final classification
                if sig[0] == "final_classification":
                    self._on_final_classification(sig[1])
                    for c_pace in self.pace.on_final_classification(sig[1]):
                        self.radio.push(c_pace)

            # Check overtakes and defend warnings on motion and lap data updates
            if h.packet_id in (packets.PacketId.LAP_DATA, packets.PacketId.MOTION):
                now_ts = self.state.session_time if self.state.session_time > 0 else time.monotonic()

                if self.track_map:
                    self.track_map.update_from_state(self.state)

                # Broadcast live 20Hz binary positions frame
                wall_now = time.monotonic()
                if wall_now - self._last_pos_time >= 0.045:
                    self._last_pos_time = wall_now
                    if self.positions_broadcaster and self.positions_broadcaster.client_count > 0:
                        t_id = self.state.session.track_id if self.state.session else 0
                        seq = self.positions_broadcaster.next_seq()
                        ts_ms = int(now_ts * 1000) & 0xFFFFFFFF
                        outline = self.track_map.current_outline if self.track_map else None
                        cars = extract_car_positions(self.state, self.track_model, outline)
                        frame = encode_positions_frame(seq, ts_ms, t_id, cars)
                        self.positions_broadcaster.broadcast_frame(frame)

                # Ensure track session is initialized
                if self.track_model_builder is None and self.state.session and self.state.session.track_id is not None:
                    self._init_track_session(self.state.session.track_id)

                # Update track model from multi-car telemetry
                if self.track_model_builder:
                    self.track_model_builder.feed_race_state(self.state)
                    if wall_now - self._last_model_build_time >= 5.0:
                        self._last_model_build_time = wall_now
                        new_m = self.track_model_builder.build_model()
                        if new_m and (new_m.coverage_pct >= 20.0 or len(new_m.corners) > 0):
                            self.track_model = new_m
                            if self.track_map:
                                self.track_map.track_model = new_m
                            self.overtake_detector.set_track_model(new_m)
                            self.defend_detector.set_track_model(new_m)
                            if new_m.is_ready:
                                self.track_model_storage.save_model(new_m)

                # 1. Overtake detection
                ot_records = self.overtake_detector.update(self.state, now=now_ts)
                for ot in ot_records:
                    if ot["direction"] == "pass":
                        if self.spotter:
                            self.spotter.suppress(4.0, now=now_ts)
                        if self.player:
                            self.player.interrupt()
                    sig_name = "overtake_confirmed" if ot["direction"] == "pass" else "overtaken_confirmed"
                    sig_ot = (sig_name, ot)
                    if self.tracker:
                        self.tracker.on_overtake(ot)
                    if self.broadcaster:
                        self.broadcaster.broadcast("overtake", ot)
                    c_pers = None
                    if self.personality:
                        spotter_active = False if ot["direction"] == "pass" else ((self.spotter.state != SpotterState.CLEAR) if self.spotter else False)
                        c_pers = self.personality.on_signal(sig_ot, self.state, spotter_active=spotter_active, now=now_ts)
                        if c_pers:
                            self.radio.push(c_pers)
                    if not c_pers:
                        for c in self.callouts.process(sig_ot, self.state):
                            self.radio.push(c)

                # 2. Defend warning detection
                df_records = self.defend_detector.update(self.state, now=now_ts)
                for df in df_records:
                    sig_df = ("defend_warning", df)
                    if self.tracker:
                        self.tracker.on_defend(df)
                    for c in self.callouts.process(sig_df, self.state):
                        self.radio.push(c)
                    if self.personality:
                        spotter_active = (self.spotter.state != SpotterState.CLEAR) if self.spotter else False
                        c_pers = self.personality.on_signal(sig_df, self.state, spotter_active=spotter_active, now=now_ts)
                        if c_pers:
                            self.radio.push(c_pers)

            # Check spotter and personality spin on motion update
            if h.packet_id == packets.PacketId.MOTION:
                if self.spotter:
                    phrase = self.spotter.process(self.state, now=now_ts)
                    if phrase:
                        self._play_spotter(phrase)
                if self.personality:
                    spotter_active = (self.spotter.state != SpotterState.CLEAR) if self.spotter else False
                    if not spotter_active and not self.state.ptt_down:
                        if self.personality.check_spin(self.state):
                            c_spin = self.personality.trigger("spin", self.state)
                            if c_spin:
                                self.radio.push(c_spin)

            # Check debrief release on telemetry ticks
            for c in self.callouts.check_debrief_release(self.state):
                self.radio.push(c)

            # Live check if rival behind enters the pit lane
            if h.packet_id == packets.PacketId.LAP_DATA:
                try:
                    b = self.state.battle_summary(max_gap_s=4.0)
                    behind = b.get("behind")
                    if behind and behind.get("is_pitting", False) and (behind.get("gap_s") or 99.0) <= 4.0:
                        cur_l = self.state.me.lap_num if self.state.me else 1
                        tot_l = self.state.session.total_laps if self.state.session else 52
                        c_stat = self.state.my_status
                        c_name = packets.COMPOUNDS.get(c_stat.visual_compound, "medium") if c_stat else "medium"
                        p_alerts = self.strategy_predictor.evaluate_proactive_strategy(
                            current_lap=cur_l,
                            total_laps=tot_l,
                            current_compound=c_name,
                            current_tyre_age=c_stat.tyre_age_laps if c_stat else 1,
                            rival_behind_pitted=True,
                            rival_behind_name=behind.get("name"),
                            rival_behind_gap_s=behind.get("gap_s"),
                        )
                        for alert in p_alerts:
                            call_text = alert["text"]
                            if self.personality:
                                pers_text = self.personality.get_reaction_text(
                                    alert["event"],
                                    alert.get("context", {}),
                                )
                                if pers_text:
                                    call_text = pers_text
                            self.radio.push(Callout(alert["priority"], time.monotonic(), call_text, ttl=10.0))
                except Exception as e:
                    log.debug("Live rival pit check error: %s", e)

            # Check track loading & session ingestion on session packet
            if h.packet_id == packets.PacketId.SESSION and self.state.session:
                if self.state.session.track_id is not None:
                    self.knowledge.load_track(self.state.session.track_id)
                if self.trace_collector:
                    self.trace_collector.on_session_packet(self.state)
                if self.state.my_setup and not self.state.current_setup_id:
                    self._on_setup_change(self.state.my_setup, self.state.current_setup_fingerprint)

                # Safety Car cheap pit stop & Rain crossover check
                try:
                    sc_call = self.strategy_predictor.evaluate_sc_opportunity(
                        self.state.session.safety_car,
                        self.state.me.lap_num if self.state.me else 1,
                        self.state.session.total_laps or 52,
                        self.state.my_status.tyre_age_laps if self.state.my_status else 1,
                        packets.COMPOUNDS.get(self.state.my_status.visual_compound, "medium") if self.state.my_status else "medium",
                    )
                    if sc_call:
                        self.radio.push(Callout(0, time.monotonic(), sc_call, ttl=10.0))

                    rain_call = self.strategy_predictor.evaluate_rain_crossover(
                        self.state.session.forecast,
                        packets.COMPOUNDS.get(self.state.my_status.visual_compound, "medium") if self.state.my_status else "medium",
                    )
                    if rain_call:
                        self.radio.push(Callout(1, time.monotonic(), rain_call, ttl=15.0))
                except Exception as e:
                    log.debug("Session strategy evaluation error: %s", e)

            # Session start radio check: 3s after first packets of a new session
            if self.state.session_uid:
                t_name = ""
                if self.state.session:
                    t_name = TRACKS.get(self.state.session.track_id, f"track {self.state.session.track_id}")
                start_msg = self.radio_check.on_session_packet(
                    self.state.session_uid,
                    track_name=t_name,
                    quiet_mode=self.quiet_mode,
                )
                if start_msg:
                    self.say(start_msg)

            # Two-way check timeout evaluation
            to_msg = self.radio_check.check_timeout()
            if to_msg:
                self.say(to_msg)

            if self.trace_collector:
                self.trace_collector.on_tick(self.state)
            if hasattr(self, "minisector_tracker") and self.minisector_tracker:
                now_tick = self.state.session_time if self.state.session_time > 0 else time.monotonic()
                self.minisector_tracker.on_tick(self.state, now_tick)
        except Exception:
            log.exception("packet handling failed")

    def _on_setup_change(self, my_setup, fp: str | None = None):
        """Captures and stores car setup, linking it to the current session."""
        if not self.setups_storage or my_setup is None:
            return
        try:
            s = self.state.session
            formula = s.formula if s else 0
            team_id = self.state.my_team_id
            track_id = s.track_id if s else 0
            weather = s.weather if s else 0
            conditions = "wet" if weather in (3, 4, 5) else "dry"
            stype = s.session_type if s else 10
            session_type_str = "quali" if 5 <= stype <= 9 else ("race" if 10 <= stype <= 12 else "practice")

            setup_row = self.setups_storage.save_captured_setup(
                params=my_setup.to_dict(),
                car_class=formula,
                team_id=team_id,
                track_id=track_id,
                conditions=conditions,
                session_type=session_type_str,
                game_format=2025,
            )
            self.state.current_setup_id = setup_row["id"]
            self.state.current_setup_fingerprint = setup_row["fingerprint"]

            best = self.setups_storage.get_best_setup(
                self.storage, track_id=track_id, car_class=formula,
                conditions=conditions, session_type=session_type_str
            )
            if best and str(best.get("id")) != str(setup_row["id"]):
                self.state.best_setup_info = best
            else:
                self.state.best_setup_info = None
        except Exception as e:
            log.debug("Error in _on_setup_change: %s", e)

    def _init_track_session(self, track_id: int):
        """Initializes knowledge, track map, track model builder, and strategy predictor for current track."""
        if self.track_model_builder and self.track_model_builder.track_id == track_id:
            return
        t_len = self.state.track_length
        self.knowledge.load_track(track_id)
        if self.track_map:
            self.track_map.set_track(track_id)
        loaded_m = self.track_model_storage.load_model(track_id, t_len)
        if loaded_m:
            self.track_model = loaded_m
            if self.track_map:
                self.track_map.track_model = loaded_m
            self.overtake_detector.set_track_model(loaded_m)
            self.defend_detector.set_track_model(loaded_m)
        self.track_model_builder = TrackModelBuilder(track_id, t_len, knowledge_base=self.knowledge)
        tot_l = self.state.session.total_laps if self.state.session and self.state.session.total_laps else 52
        self.tyre_model = TyreDegradationModel(track_total_laps=tot_l)
        self.strategy_predictor = StrategyPredictor(deg_model=self.tyre_model)
        log.info("Initialized track session: track_id=%s, track_len=%.1fm", track_id, t_len)

    def on_ptt(self, down: bool):
        self.radio.ptt_down = down
        if hasattr(self, "radio_check"):
            self.radio_check.on_ptt(down)
        if down:
            # PTT pressed -> cancel any in-flight AI agent generation task
            if self._agent_task and not self._agent_task.done():
                self._agent_task.cancel()
                log.info("PTT pressed: cancelled in-flight AI agent generation")
        else:
            # PTT button released -> end of audio capture, start STT processing
            self.latency.start_transmission()
            self.latency.record("ptt_up")
        if self.audio_capture:
            self.audio_capture.on_ptt(down)
        log.info("push-to-talk %s", "DOWN" if down else "UP")

    def on_audio_recorded(self, audio: np.ndarray):
        """Called when AudioCapture finishes collecting a transmission."""
        if self.loop and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(self._process_driver_audio(audio), self.loop)
        else:
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    loop.create_task(self._process_driver_audio(audio))
                else:
                    loop.run_until_complete(self._process_driver_audio(audio))
            except RuntimeError:
                asyncio.run(self._process_driver_audio(audio))

    async def _process_driver_audio(self, audio: np.ndarray):
        # 1. If two-way radio check is waiting for driver response
        if hasattr(self, "radio_check") and (self.radio_check.state.value == "waiting_for_driver" or self.radio_check.driver_speaking):
            transcribe_fn = self.stt.transcribe if self.stt else (lambda a: "")
            result_msg = await asyncio.to_thread(self.radio_check.on_audio, audio, transcribe_fn)
            if result_msg:
                self.say(result_msg)
            return

        if self.stt is None:
            return
        prompt = build_initial_prompt(names=self.state.names, corner_names=self.get_corner_names())
        text = await asyncio.to_thread(self.stt.transcribe, audio, prompt)
        self.latency.record("stt_done")
        if not text:
            log.info("STT: empty or filler discarded")
            self.latency.cancel_active()
            return
        log.info("Driver: %r", text)
        if self.broadcaster:
            self.broadcaster.broadcast_radio("driver", text)
        self.ask(text)

    def get_corner_names(self) -> list[str]:
        """Returns corner names for current track for STT prompt biasing."""
        return self.knowledge.get_corner_names()

    def get_knowledge_text(self, question: str) -> str:
        """Returns relevant track and setup notes via keyword matching."""
        return self.knowledge.select_knowledge(question, max_tokens=self.config.knowledge.max_tokens)

    def ask(self, text: str):
        reply = route(text, self.state)
        self.latency.record("route_done")
        if reply.kind == "answer":
            if reply.intent == "radio_check":
                reply_text = RadioCheckManager.build_voice_reply(self.state, self.personality)
                stt_dur = 0.0
                curr = getattr(self.latency, "_current", None)
                if curr and "ptt_up" in curr.timestamps and "stt_done" in curr.timestamps:
                    stt_dur = (curr.timestamps["stt_done"] - curr.timestamps["ptt_up"]) * 1000.0
                log.info(
                    "[RADIO CHECK] Driver: %r -> Reply: %r | PTT up -> STT: %.1fms",
                    text, reply_text, stt_dur
                )
                self.say(reply_text)
            else:
                self.say(reply.text)
        else:
            # HOOK 2: send text + state snapshot + knowledge to the AI agent
            log.info("-> AI agent: %r", text)
            if self._agent_task and not self._agent_task.done():
                self._agent_task.cancel()

            snapshot = self.state.snapshot()
            knowledge_text = self.get_knowledge_text(text)

            if self.agent:
                coro = self.agent.answer(text, snapshot, knowledge_text)
                if self.loop and self.loop.is_running():
                    self._agent_task = self.loop.create_task(coro)
                else:
                    try:
                        loop = asyncio.get_running_loop()
                        self._agent_task = loop.create_task(coro)
                    except RuntimeError:
                        asyncio.run(coro)
            else:
                self.say("Copy, stand by.")

    def say(self, text: str):
        self.radio.push(Callout(-1, time.monotonic(), text))

    async def speaker(self):
        while not self.stopping:
            c = self.radio.pop()
            if c:
                if self.broadcaster:
                    latency_ms = None
                    if c.priority == -1:
                        avgs = self.latency.rolling_averages()
                        latency_ms = avgs.get("total_turnaround")
                    self.broadcaster.broadcast_radio("engineer", c.text, latency_ms=latency_ms)

                if self.tts and self.player:
                    try:
                        if self.personality and c.text in self.personality.prerendered_audio:
                            audio, sr = self.personality.prerendered_audio[c.text]
                        else:
                            audio, sr = await self.tts.synth(c.text)
                            if self.config.tts.radio_fx:
                                audio = apply_radio_fx(audio, sr, enable_clicks=True)

                        eff_vol = getattr(self.config.volumes, "engineer", 1.0)
                        if eff_vol != 1.0:
                            audio = audio * eff_vol

                        if c.priority == -1:
                            self.latency.record("tts_first_audio")

                        # Play audio and monitor for PTT interruption
                        completed = await asyncio.to_thread(
                            self.player.play, audio, sr, lambda: self.radio.ptt_down
                        )
                        if completed:
                            if c.priority == -1:
                                self.latency.record("playback_done")
                        else:
                            if c.priority == -1:
                                self.latency.cancel_active()
                    except Exception as e:
                        log.exception("TTS playback failed: %s", e)
                else:
                    # Fallback text printing when TTS is disabled/None
                    print(f"[RADIO] {c.text}", flush=True)
                    if c.priority == -1:
                        self.latency.record("playback_done")
            await asyncio.sleep(0.05)


def get_local_ip() -> str:
    """Returns local LAN IP address for setting up in-game UDP telemetry destination."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


class Listener(asyncio.DatagramProtocol):
    def __init__(self, on_packet):
        self.on_packet = on_packet
        self.packet_count = 0
        self.first_packet = True
        self.last_log_time = 0.0

    def datagram_received(self, data, addr):
        now = time.monotonic()
        self.packet_count += 1
        if self.first_packet:
            self.first_packet = False
            self.last_log_time = now
            print(f"\n[TELEMETRY] First UDP packet received from {addr[0]}:{addr[1]} ({len(data)} bytes)! F1 game is streaming data.\n", flush=True)
            log.info("First UDP packet received from %s:%s (%d bytes)", addr[0], addr[1], len(data))
        elif now - self.last_log_time >= 5.0:
            self.last_log_time = now
            print(f"[TELEMETRY] Receiving data: {self.packet_count} packets processed (from {addr[0]}:{addr[1]})", flush=True)

        self.on_packet(data)


def start_console(engineer: Engineer, loop):
    def run():
        for line in sys.stdin:
            msg = line.strip()
            if msg:
                if engineer.broadcaster:
                    engineer.broadcaster.broadcast_radio("driver", msg)
                loop.call_soon_threadsafe(engineer.ask, msg)
    threading.Thread(target=run, daemon=True).start()   # daemon so Ctrl+C exits cleanly


async def replay(path, engineer: Engineer, speed: float, explain: bool = False, tracker: ExplainTracker | None = None):
    start, first = time.monotonic(), None
    with open(path, "rb") as f:
        while hdr := f.read(FRAME.size):
            t, n = FRAME.unpack(hdr)
            buf = f.read(n)
            if not explain:
                first = t if first is None else first
                delay = (t - first) / speed - (time.monotonic() - start)
                if delay > 0:
                    await asyncio.sleep(delay)
            engineer.on_packet(buf)
    if explain and tracker:
        tracker.print_report(path)


async def publish(engineer: Engineer, url: str, hz: float):
    import redis.asyncio as redis
    client = redis.from_url(url)   # one client, reused for every write
    try:
        while True:
            await client.set("current_race_state", json.dumps(engineer.state.snapshot()))
            await asyncio.sleep(1 / hz)
    finally:
        await client.aclose()


async def selftest(engineer: Engineer):
    """Replays a synthetic sequence, tests deterministic & AI questions through LLM and TTS, and reports latencies."""
    from test_synthetic import (
        damage_packet, event, lap_packet, participants_packet,
        session_packet, status_packet, telemetry_packet,
    )

    print("\n======================= STARTING SELF-TEST =======================")
    print("1. Injecting synthetic F1 25 session (Silverstone, Lap 6, P2)...")
    engineer.on_packet(participants_packet({0: "Player", 1: "LECLERC", 2: "NORRIS"}))
    engineer.on_packet(session_packet())
    engineer.on_packet(telemetry_packet())
    engineer.on_packet(status_packet(40.2, -0.3))
    engineer.on_packet(damage_packet((40, 41, 55, 52), 0))
    engineer.on_packet(lap_packet(6, {1: (1, 0, 0), 0: (2, 1100, 91234), 2: (3, 900, 0)}))

    # Test 1: Deterministic Question
    print("\n2. Testing Deterministic Question: 'What's the gap ahead?'")
    engineer.ask("What's the gap ahead?")

    # Wait for radio queue playback
    for _ in range(60):
        await asyncio.sleep(0.1)
        if not engineer.radio._heap:
            break

    # Test 2: AI Question
    print("\n3. Testing AI Question: 'I'm washing out at Copse, front left is cooking'")
    engineer.ask("I'm washing out at Copse, front left is cooking")

    # Wait for AI agent and radio playback
    for _ in range(120):
        await asyncio.sleep(0.1)
        if not engineer.radio._heap and (engineer._agent_task is None or engineer._agent_task.done()):
            break

    # Latency Report
    print("\n==================== SELFTEST LATENCY REPORT ====================")
    averages = engineer.latency.rolling_averages()
    if averages:
        for stage, dur_ms in sorted(averages.items()):
            print(f"  {stage:<18}: {dur_ms:>7.1f} ms")
    else:
        print("  (No completed transmissions recorded)")
    print("=================================================================")
    print("Self-test completed successfully!\n")


async def amain(args, cfg: AppConfig, host: str, port: int, ptt_action: int, redis_url: str | None, redis_hz: float, stop_event: asyncio.Event | None = None):
    loop = asyncio.get_running_loop()
    recorder = Recorder(args.record) if args.record else None
    latency = LatencyTracker()

    if getattr(args, "explain", False):
        if not args.replay:
            print("Error: --explain requires --replay <file>", file=sys.stderr)
            return
        tracker = ExplainTracker()
        engineer = Engineer(
            ptt_action=ptt_action,
            recorder=recorder,
            config=cfg,
            latency=latency,
            stt=None,
            audio_capture=None,
            tts=None,
            player=None,
            knowledge=KnowledgeBase(config=cfg.knowledge),
            loop=loop,
            storage=SessionStorage(),
            config_path=args.config,
        )
        engineer.tracker = tracker
        await replay(args.replay, engineer, args.speed, explain=True, tracker=tracker)
        if engineer.storage:
            engineer.storage.close()
        return

    # HOOK 1: initialize STT and persistent audio capture (skipped in --selftest as queries are text-injected)
    if not args.selftest:
        stt = SpeechToText(config=cfg.stt, warmup=True)
        audio_capture = AudioCapture(
            sample_rate=cfg.audio.sample_rate,
            device=cfg.audio.input_device,
            pre_roll_ms=cfg.stt.pre_roll_ms,
            min_duration_s=cfg.stt.min_duration_s,
            max_duration_s=cfg.stt.max_duration_s,
            start_stream=True,
        )
    else:
        stt = None
        audio_capture = None


    usage_guard = UsageGuard(config=cfg.usage)
    if not args.selftest:
        usage_guard.run_preflight_check()

    # HOOK 2: initialize LLM client with warmup
    llm_client = LLMClient(config=cfg.llm, usage_guard=usage_guard)
    await llm_client.warmup()

    # HOOK 3: initialize TTS engine with warmup and persistent audio player
    tts_engine = create_tts_engine(cfg.tts, usage_guard=usage_guard)
    await tts_engine.warmup()
    player = AudioPlayer(
        device=cfg.audio.output_device,
        start_stream=not args.selftest,
        volume=cfg.volumes.engineer,
    )

    knowledge = KnowledgeBase(config=cfg.knowledge)

    engineer = Engineer(
        ptt_action=ptt_action,
        recorder=recorder,
        config=cfg,
        latency=latency,
        stt=stt,
        audio_capture=audio_capture,
        tts=tts_engine,
        player=player,
        knowledge=knowledge,
        loop=loop,
        usage_guard=usage_guard,
        config_path=args.config,
    )
    if audio_capture:
        audio_capture.on_audio_recorded = engineer.on_audio_recorded
    engineer.agent = RaceEngineerAgent(
        llm=llm_client,
        radio=engineer.radio,
        latency=latency,
        persona_name=cfg.persona.name,
        profanity=cfg.persona.profanity,
    )

    if tts_engine and not args.selftest:
        await engineer.prerender_spotter()
        await engineer.prerender_persona_lines()

    tasks = [asyncio.create_task(engineer.speaker())]

    dashboard_server = None
    if cfg.dashboard.enabled and not args.selftest:
        from dashboard.server import DashboardServer
        dashboard_server = DashboardServer(
            engineer.state,
            cfg,
            config_path=args.config,
            storage=engineer.storage,
            setups_storage=engineer.setups_storage,
            llm_client=llm_client,
            knowledge=engineer.knowledge,
            engineer=engineer,
            usage_guard=usage_guard,
        )
        server_task, bcast_task = dashboard_server.start(loop=loop)
        tasks.extend([server_task, bcast_task])
        engineer.broadcaster = dashboard_server.broadcaster

    if args.selftest:
        try:
            await selftest(engineer)
        finally:
            for t in tasks:
                t.cancel()
            if dashboard_server:
                await dashboard_server.stop()
            if audio_capture:
                audio_capture.stop()
            if player:
                player.stop()
            if llm_client:
                await llm_client.aclose()
            if engineer.storage:
                engineer.storage.close()
        return

    if redis_url:
        tasks.append(asyncio.create_task(publish(engineer, redis_url, redis_hz)))
    if not args.no_console:
        start_console(engineer, loop)

    # Play startup sound if enabled and session is not already live
    if engineer.sound_test and not engineer.is_session_live() and not args.selftest and not args.replay:
        await engineer.sound_test.play_startup_sound()

    if stop_event is None:
        stop_event = asyncio.Event()
    engineer._stop_event = stop_event

    # Install signal handlers on Python's main thread (SIGINT, SIGBREAK on Windows, SIGTERM)
    sig_count = 0

    def _sig_handler(sig, frame):
        nonlocal sig_count
        sig_count += 1
        if sig_count == 1:
            log.info("Received exit signal (%s); initiating graceful shutdown...", sig)
            loop.call_soon_threadsafe(stop_event.set)
        else:
            log.warning("Received repeated signal (%s); forcing immediate exit.", sig)
            os._exit(1)

    old_handlers = {}
    for sig_name in ("SIGINT", "SIGBREAK", "SIGTERM"):
        if hasattr(signal, sig_name):
            s = getattr(signal, sig_name)
            try:
                old_handlers[s] = signal.signal(s, _sig_handler)
            except (ValueError, OSError):
                pass

    # Log active ports in use at startup
    dash_port_str = f"{cfg.dashboard.host}:{cfg.dashboard.port}" if cfg.dashboard.enabled and not args.selftest else "disabled"
    log.info("Active ports: UDP telemetry=%d, Dashboard HTTP=%s", port, dash_port_str)
    print(f"  Ports in use: UDP Telemetry={port} | Dashboard HTTP={dash_port_str}")

    transport = None
    try:
        if args.replay:
            replay_task = asyncio.create_task(replay(args.replay, engineer, args.speed))
            stop_task = asyncio.create_task(stop_event.wait())
            done, _ = await asyncio.wait([replay_task, stop_task], return_when=asyncio.FIRST_COMPLETED)
            if replay_task in done:
                await asyncio.sleep(0.5)
                print(json.dumps(engineer.state.snapshot(), indent=2))
            else:
                replay_task.cancel()
        else:
            listener_instance = Listener(engineer.on_packet)
            try:
                transport, _ = await loop.create_datagram_endpoint(
                    lambda: listener_instance, local_addr=(host, port))
            except OSError as e:
                is_addr_in_use = (
                    (getattr(e, "errno", None) in (48, 98, 10048))
                    or ("Address already in use" in str(e))
                    or ("WinError 10048" in str(e))
                )
                if is_addr_in_use:
                    conflict_msg = format_port_conflict_message(port, proto="udp")
                    log.error("UDP Port conflict: %s", conflict_msg)
                    print(f"\n❌ Port Conflict: {conflict_msg}")
                    print(f"   Another instance of Race Engineer (or another telemetry app) is running.")
                    print(f"   To fix this:")
                    print(f"     • Close the conflicting application")
                    print(f"     • Or start with a different port: python main.py --port {port + 1} (or --ask-port)\n", flush=True)
                    engineer.state.engine_error = conflict_msg
                    engineer.engine_error = conflict_msg
                    if dashboard_server:
                        print("   Web dashboard remains active to display engine error status.")
                        await stop_event.wait()
                    return
                raise

            local_ip = get_local_ip()
            log.info("listening on %s:%d (PTT = UDP Action %d)", host, port, ptt_action)
            print(f"\n==============================================================")
            print(f"  🏎️  F1 25 TELEMETRY LISTENER ACTIVE")
            print(f"  Listening on UDP: {host}:{port}")
            print(f"  In-Game Target IP: {local_ip}:{port} (or 127.0.0.1 if on same PC)")
            print(f"  Status: Waiting for F1 25 packets...")
            print(f"==============================================================\n", flush=True)

            async def _telemetry_watchdog():
                await asyncio.sleep(10)
                if listener_instance.packet_count == 0:
                    print("\n" + "="*62)
                    print("  ⚠️  NO F1 25 TELEMETRY PACKETS RECEIVED YET")
                    print("="*62)
                    print(f"  Race Engineer is listening on UDP port {port}, but 0 packets")
                    print(f"  have arrived from F1.")
                    print(f"  Please verify your F1 25 in-game settings:")
                    print(f"    1. Go to Game Options > Settings > Telemetry Settings")
                    print(f"    2. UDP Telemetry: ON")
                    print(f"    3. UDP IP Address: {local_ip} (or 127.0.0.1 if on same PC)")
                    print(f"    4. UDP Port: {port}")
                    print(f"    5. UDP Packet Format: 2025")
                    print(f"    6. UDP Send Rate: 20Hz (or 60Hz)")
                    print("="*62 + "\n", flush=True)

            watchdog_task = asyncio.create_task(_telemetry_watchdog())
            tasks.append(watchdog_task)

            # Await stop_event
            await stop_event.wait()

    finally:
        # Ordered shutdown with hang diagnosis and 5-second watchdog
        log.info("Shutting down Race Engineer...")
        try:
            faulthandler.dump_traceback_later(8, exit=False)
        except Exception:
            pass

        def _watchdog_timeout():
            log.warning("Shutdown watchdog expired (5s timeout). Forcing process exit.")
            pending_threads = [t.name for t in threading.enumerate() if not t.daemon]
            log.warning("Remaining non-daemon threads: %s", pending_threads)
            os._exit(0)

        watchdog_handle = loop.call_later(5.0, _watchdog_timeout)

        try:
            # 1. Stop accepting new radio/LLM work
            engineer.stopping = True

            # 2. Cancel in-flight agent tasks & background tasks
            if engineer._agent_task and not engineer._agent_task.done():
                engineer._agent_task.cancel()
            for t in tasks:
                if not t.done():
                    t.cancel()

            # 3. Close the UDP transport
            if transport:
                try:
                    transport.close()
                except Exception as e:
                    log.debug("Error closing UDP transport: %s", e)

            # 4. Stop uvicorn and await its task
            if dashboard_server:
                try:
                    await dashboard_server.stop()
                except Exception as e:
                    log.debug("Error stopping dashboard server: %s", e)

            # 5. Stop and close audio streams
            if audio_capture:
                try:
                    audio_capture.stop()
                except Exception as e:
                    log.debug("Error stopping audio capture: %s", e)
            if player:
                try:
                    player.stop()
                except Exception as e:
                    log.debug("Error stopping player: %s", e)

            # 6. Flush and close SQLite and recordings
            if recorder:
                try:
                    recorder.close()
                except Exception as e:
                    log.debug("Error closing recorder: %s", e)
            if engineer.storage:
                try:
                    engineer.storage.close()
                except Exception as e:
                    log.debug("Error closing storage: %s", e)

            # 7. Close httpx clients
            if llm_client:
                try:
                    await llm_client.aclose()
                except Exception as e:
                    log.debug("Error closing LLM client: %s", e)

            # Brief grace period for cancelled tasks
            pending = [t for t in tasks if not t.done()]
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

        finally:
            watchdog_handle.cancel()
            try:
                faulthandler.cancel_dump_traceback_later()
            except Exception:
                pass
            for s, h in old_handlers.items():
                try:
                    signal.signal(s, h)
                except Exception:
                    pass
            log.info("Shutdown completed cleanly.")


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Voice Race Engineer for F1 25")
    p.add_argument("--config", default="config.yaml", help="path to config.yaml (default: config.yaml)")
    p.add_argument("--host", default=None, help="UDP host (overrides config)")
    p.add_argument("--port", type=int, default=None, help="UDP port (overrides config)")
    p.add_argument("--ptt-action", type=int, default=None, help="in-game 'UDP Action' number (overrides config)")
    p.add_argument("--record", help="save raw packets to this file")
    p.add_argument("--replay", help="replay a recorded file instead of listening")
    p.add_argument("--explain", action="store_true", help="replay a recorded file and print per-lap detected events without audio")
    p.add_argument("--speed", type=float, default=1.0)
    p.add_argument("--redis", help="redis URL e.g. redis://localhost:6379/0 (overrides config)")
    p.add_argument("--redis-hz", type=float, default=None, help="redis publish rate in Hz (overrides config)")
    p.add_argument("--no-dashboard", action="store_true", help="disable live web dashboard")
    p.add_argument("--dashboard-port", type=int, default=None, help="dashboard port (overrides config)")
    p.add_argument("--dashboard-host", default=None, help="dashboard host (overrides config)")
    p.add_argument("--no-console", action="store_true", help="disable stdin question console")
    p.add_argument("--list-devices", action="store_true", help="print audio input/output devices and exit")
    p.add_argument("--selftest", action="store_true", help="run synthetic end-to-end self test with TTS and LLM")
    p.add_argument("--print-setup-ranges", action="store_true", help="print observed setup parameter ranges from captured setups and exit")
    p.add_argument("--import-setup", help="import setup from JSON or CSV file path and save to library")
    p.add_argument("--export-setup", help="export setup by ID or fingerprint (prints sheet or JSON)")
    p.add_argument("--export-format", choices=["sheet", "json"], default="sheet", help="format for --export-setup (default: sheet)")
    p.add_argument("--usage", action="store_true", help="print today's cloud AI usage and limits and exit")
    p.add_argument("--sound-test", action="store_true", help="run offline speaker sound test and exit")
    p.add_argument("--ask-port", action="store_true", default=None, help="ask interactively for UDP and dashboard ports on startup")
    p.add_argument("--clear-cache", action="store_true", help="clear static TTS audio cache and exit")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    if args.clear_cache:
        mgr = AudioCacheManager()
        count, freed = mgr.clear()
        print(f"TTS Audio Cache cleared: {count} files removed ({freed / (1024 * 1024):.2f} MB freed).")
        sys.exit(0)

    if args.sound_test:
        cfg = load_config(args.config)
        tts = create_tts_engine(cfg.tts, cache_config=getattr(cfg, "cache", None))
        player = AudioPlayer(device=cfg.audio.output_device, volume=cfg.volumes.engineer)
        st_mgr = SoundTestManager(tts=tts, player=player, config=cfg)
        print("Running Speaker Sound Test...")
        res = asyncio.run(st_mgr.play_speaker_test(with_radio_fx=True))
        print("Result:", res)
        sys.exit(0)

    if args.usage:
        cfg = load_config(args.config)
        guard = UsageGuard(config=cfg.usage)
        guard.print_usage_summary()
        sys.exit(0)

    if args.list_devices:
        print_audio_devices()
        sys.exit(0)

    if args.print_setup_ranges:
        storage = SetupsStorage("setups.db")
        print(storage.print_observed_ranges())
        sys.exit(0)

    if args.import_setup:
        storage = SetupsStorage("setups.db")
        p = Path(args.import_setup)
        if not p.is_file():
            print(f"Error: file not found: {args.import_setup}", file=sys.stderr)
            sys.exit(1)
        content = p.read_text(encoding="utf-8")
        if p.suffix.lower() == ".json":
            parsed = import_from_json(content)
        else:
            p_dict = import_from_csv(content)
            parsed = {"name": p.stem, "car_class": 0, "track_id": 0, "conditions": "dry", "session_type": "race", "params": p_dict}

        val = validate_setup_params(parsed.get("params", {}), car_class=parsed.get("car_class", 0))
        print("Parsed Setup:")
        print(f"  Name: {parsed.get('name')}")
        print(f"  Parameters: {len(val['params'])} detected")
        if val["out_of_range"]:
            print(f"  Out of range warnings: {val['out_of_range']}")
        if val["missing"]:
            print(f"  Missing parameters (left empty): {len(val['missing'])}")
        res = storage.save_imported_setup(
            params=val["params"],
            car_class=parsed.get("car_class", 0),
            track_id=parsed.get("track_id", 0),
            conditions=parsed.get("conditions", "dry"),
            session_type=parsed.get("session_type", "race"),
            source_note=f"Imported from {p.name}",
            name=parsed.get("name", p.stem),
        )
        print(f"Saved setup ID {res['id']} (fingerprint {res['fingerprint']})")
        sys.exit(0)

    if args.export_setup:
        storage = SetupsStorage("setups.db")
        s = storage.get_setup(args.export_setup)
        if not s:
            print(f"Setup {args.export_setup} not found in library", file=sys.stderr)
            sys.exit(1)
        if args.export_format == "json":
            print(export_as_json(s))
        else:
            print(export_as_sheet(s))
        sys.exit(0)

    cfg = load_config(args.config)

    # Interactively ask for ports if requested via --ask-port flag or config.telemetry.ask_port
    should_ask_port = args.ask_port if args.ask_port is not None else getattr(cfg.telemetry, "ask_port", False)
    if should_ask_port:
        curr_udp = args.port if args.port is not None else cfg.telemetry.port
        curr_dash = args.dashboard_port if args.dashboard_port is not None else cfg.dashboard.port
        try:
            print("\n==============================================================")
            print("                 PORT CONFIGURATION SELECTION")
            print("==============================================================")
            print("Press Enter to keep the default port, or type a new port number.\n")
            udp_in = input(f"  F1 25 UDP Telemetry Port [default {curr_udp}]: ").strip()
            if udp_in:
                try:
                    p_val = int(udp_in)
                    if 1 <= p_val <= 65535:
                        args.port = p_val
                    else:
                        print(f"  Invalid port {p_val}, keeping {curr_udp}")
                except ValueError:
                    print(f"  Invalid input '{udp_in}', keeping {curr_udp}")

            dash_in = input(f"  Web Dashboard Port       [default {curr_dash}]: ").strip()
            if dash_in:
                try:
                    d_val = int(dash_in)
                    if 1 <= d_val <= 65535:
                        args.dashboard_port = d_val
                    else:
                        print(f"  Invalid port {d_val}, keeping {curr_dash}")
                except ValueError:
                    print(f"  Invalid input '{dash_in}', keeping {curr_dash}")
            print("==============================================================\n")
        except (EOFError, KeyboardInterrupt):
            print("\nPort selection skipped, using defaults.")

    # CLI flags override config
    host = args.host if args.host is not None else cfg.telemetry.host
    port = args.port if args.port is not None else cfg.telemetry.port
    ptt_action = args.ptt_action if args.ptt_action is not None else cfg.telemetry.ptt_action
    redis_url = args.redis if args.redis is not None else (cfg.redis.url if cfg.redis.enabled else None)
    redis_hz = args.redis_hz if args.redis_hz is not None else cfg.redis.hz

    if args.no_dashboard:
        cfg.dashboard.enabled = False
    if args.dashboard_port is not None:
        cfg.dashboard.port = args.dashboard_port
    if args.dashboard_host is not None:
        cfg.dashboard.host = args.dashboard_host

    log_level = getattr(logging, cfg.logging.level.upper(), logging.INFO)
    logging.basicConfig(level=log_level, format=cfg.logging.format)
    if getattr(cfg.logging, "detectors", False):
        det_log = logging.getLogger("engineer.detectors")
        det_log.setLevel(logging.DEBUG)
        if log_level > logging.DEBUG and not det_log.handlers:
            ch = logging.StreamHandler()
            ch.setLevel(logging.DEBUG)
            ch.setFormatter(logging.Formatter(cfg.logging.format))
            det_log.addHandler(ch)
            det_log.propagate = False

    # Single-instance lock: covers main.py, run.bat, app.py
    skip_lock = (
        args.selftest
        or args.list_devices
        or args.print_setup_ranges
        or args.sound_test
        or args.usage
        or args.clear_cache
        or bool(args.import_setup)
        or bool(args.export_setup)
    )

    instance_lock = None
    if not skip_lock:
        instance_lock = SingleInstanceLock()
        acquired, running_pid = instance_lock.acquire()
        if not acquired:
            print(f"Race Engineer is already running (PID {running_pid})")
            sys.exit(1)

    try:
        asyncio.run(amain(args, cfg, host, port, ptt_action, redis_url, redis_hz))
    except KeyboardInterrupt:
        pass
    finally:
        if instance_lock:
            instance_lock.release()


if __name__ == "__main__":
    main()


