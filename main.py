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
import hashlib
import json
import logging
import struct
import sys
import threading
import time
from pathlib import Path

import numpy as np

from agent import RaceEngineerAgent
from audio_devices import print_audio_devices
from audio_in import AudioCapture, start_joystick_listener
from audio_player import AudioPlayer
from config import AppConfig, load_config
from f1telemetry import packets
from f1telemetry.deterministic import Callout, Callouts, RadioQueue, route
from f1telemetry.spotter import Spotter, SPOTTER_PHRASES, SpotterState
from f1telemetry.state import RaceState
from knowledge import KnowledgeBase
from latency import LatencyTracker
from llm import LLMClient
from personality import PersonalityEngine
from radio_fx import apply_radio_fx
from stt import SpeechToText, build_initial_prompt
from tts import TTSProvider, create_tts_engine
from storage import SessionStorage, LapTraceCollector
from analysis import get_lap_comparison_summary, compute_lap_debrief
from setups import SetupsStorage, import_from_json, import_from_csv, export_as_sheet, export_as_json, validate_setup_params

log = logging.getLogger("engineer")
FRAME = struct.Struct("<dI")   # recording: seconds since start, payload length


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
    ):
        self.config = config or AppConfig()
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
        self._agent_task: asyncio.Task | None = None
        self._joystick_thread = None

        if self.config.stt.ptt_source == "joystick":
            self._joystick_thread = start_joystick_listener(
                self.on_ptt,
                joystick_index=self.config.stt.joystick_index,
                button_index=self.config.stt.joystick_button,
            )

    async def prerender_spotter(self):
        """Pre-renders spotter audio clips to bypass TTS latency during wheel-to-wheel battles."""
        if not self.tts:
            return
        log.info("Pre-rendering spotter phrases for instant zero-latency playback...")
        for phrase in SPOTTER_PHRASES:
            try:
                audio, sr = await self.tts.synth(phrase)
                if self.config.tts.radio_fx:
                    audio = apply_radio_fx(audio, sr, enable_clicks=True)
                self.spotter_audio[phrase] = (audio, sr)
            except Exception as e:
                log.warning("Failed to pre-render spotter phrase %r: %s", phrase, e)
        log.info("Pre-rendered %d spotter phrases", len(self.spotter_audio))

    async def prerender_persona_lines(self):
        """Pre-renders static persona lines (no placeholders) to disk and memory cache for zero latency."""
        if not self.tts or not self.personality or not self.personality.lines:
            return
        log.info("Pre-rendering static persona lines...")
        voice = getattr(self.config.tts, "voice_name", "default")
        cache_dir = Path("cache/audio")
        cache_dir.mkdir(parents=True, exist_ok=True)

        for event_name, lines in self.personality.lines.items():
            for line in lines:
                if line.has_placeholders:
                    continue
                text = line.text
                key = hashlib.sha256(f"{voice}:{text}".encode("utf-8")).hexdigest()
                cache_file = cache_dir / f"{key}.npz"
                if cache_file.is_file():
                    try:
                        data = np.load(cache_file)
                        self.personality.prerendered_audio[text] = (data["audio"], int(data["sr"]))
                        continue
                    except Exception as e:
                        log.debug("Cache read error for %r: %s", text, e)
                try:
                    audio, sr = await self.tts.synth(text)
                    if self.config.tts.radio_fx:
                        audio = apply_radio_fx(audio, sr, enable_clicks=True)
                    np.savez_compressed(cache_file, audio=audio, sr=sr)
                    self.personality.prerendered_audio[text] = (audio, sr)
                except Exception as e:
                    log.warning("Failed to pre-render persona line %r: %s", text, e)
        log.info("Pre-rendered %d static persona lines", len(self.personality.prerendered_audio))

    def _play_spotter(self, phrase: str):
        if self.broadcaster:
            self.broadcaster.broadcast_radio("engineer", phrase)
        if phrase in self.spotter_audio and self.player:
            audio, sr = self.spotter_audio[phrase]
            threading.Thread(target=self.player.play_direct, args=(audio, sr), daemon=True).start()
        elif self.player:
            self.radio.push(Callout(0, time.monotonic(), phrase, ttl=self.config.spotter.ttl_s))
        else:
            print(f"[SPOTTER] {phrase}", flush=True)

    def _on_final_classification(self, cars):
        """Builds race summary and triggers post-race agent message in persona."""
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
        blunders = len(self.personality.blunders) if self.personality else 0

        best_lap_s = None
        if self.state.personal_best:
            best_lap_s = self.state.personal_best.get("lap_time_s")

        summary = {
            "grid": grid_pos,
            "finish_position": pos,
            "positions_gained": gained,
            "fastest_lap": False,
            "penalties_s": self.state.me.penalties_s if self.state.me else 0,
            "blunder_count": blunders,
            "best_lap": f"{best_lap_s:.2f}s" if best_lap_s else "N/A",
        }

        async def _run_post_race():
            msg = None
            if self.agent:
                try:
                    msg = await self.agent.generate_post_race_message(summary)
                except Exception as e:
                    log.warning("Agent post-race debrief failed: %s", e)
            if not msg and self.personality:
                evt = "win" if pos == 1 else ("podium" if 2 <= pos <= 3 else ("points" if pos <= 10 else "no_points"))
                c_fallback = self.personality.trigger(evt, self.state, {"pos": pos})
                if c_fallback:
                    msg = c_fallback.text
            if not msg:
                msg = f"P{pos} across the line. That's the chequered flag."

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
        except Exception as e:
            log.debug("Could not update lap analysis: %s", e)

    def on_packet(self, buf: bytes):
        if self.recorder:
            self.recorder.write(buf)
        try:
            h, data = packets.parse(buf)
            if data is None:
                return
            for sig in self.state.update(h, data):
                if sig[0] == "ptt":
                    self.on_ptt(sig[1])
                elif sig[0] == "new_session":
                    if self.state.my_setup:
                        self._on_setup_change(self.state.my_setup, self.state.current_setup_fingerprint)
                    if self.state.session and self.state.session.track_id is not None:
                        self.knowledge.load_track(self.state.session.track_id)
                elif sig[0] == "setup_change":
                    self._on_setup_change(sig[1], sig[2])
                elif sig[0] == "lap":
                    rec = sig[1]
                    if self.trace_collector:
                        self.trace_collector.on_lap_completed(rec, self.state)
                    self._update_lap_analysis()
                    self._on_lap_completed_debrief(rec)
                elif sig[0] == "flashback":
                    lap_num = sig[1]
                    if self.trace_collector:
                        self.trace_collector.on_flashback(lap_num, self.state.session_uid)
                for c in self.callouts.process(sig, self.state):
                    self.radio.push(c)

                # Personality reaction check (overrides: never replaces neutral calls; no reaction on active spotter/PTT)
                if self.personality:
                    spotter_active = (self.spotter.state != SpotterState.CLEAR) if self.spotter else False
                    c_pers = self.personality.on_signal(sig, self.state, spotter_active=spotter_active)
                    if c_pers:
                        self.radio.push(c_pers)

                # Post-race trigger on final classification
                if sig[0] == "final_classification":
                    self._on_final_classification(sig[1])

            # Check spotter and personality spin on motion update
            if h.packet_id == packets.PacketId.MOTION:
                if self.spotter:
                    phrase = self.spotter.process(self.state)
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

            # Check track loading & session ingestion on session packet
            if h.packet_id == packets.PacketId.SESSION and self.state.session:
                if self.state.session.track_id is not None:
                    self.knowledge.load_track(self.state.session.track_id)
                if self.trace_collector:
                    self.trace_collector.on_session_packet(self.state)
                if self.state.my_setup and not self.state.current_setup_id:
                    self._on_setup_change(self.state.my_setup, self.state.current_setup_fingerprint)

            if self.trace_collector:
                self.trace_collector.on_tick(self.state)
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

    def on_ptt(self, down: bool):
        self.radio.ptt_down = down
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
        while True:
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


class Listener(asyncio.DatagramProtocol):
    def __init__(self, on_packet):
        self.on_packet = on_packet

    def datagram_received(self, data, addr):
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


async def replay(path, engineer: Engineer, speed: float):
    start, first = time.monotonic(), None
    with open(path, "rb") as f:
        while hdr := f.read(FRAME.size):
            t, n = FRAME.unpack(hdr)
            buf = f.read(n)
            first = t if first is None else first
            delay = (t - first) / speed - (time.monotonic() - start)
            if delay > 0:
                await asyncio.sleep(delay)
            engineer.on_packet(buf)


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


async def amain(args, cfg: AppConfig, host: str, port: int, ptt_action: int, redis_url: str | None, redis_hz: float):
    loop = asyncio.get_running_loop()
    recorder = Recorder(args.record) if args.record else None
    latency = LatencyTracker()

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


    # HOOK 2: initialize LLM client with warmup
    llm_client = LLMClient(config=cfg.llm)
    await llm_client.warmup()

    # HOOK 3: initialize TTS engine with warmup and persistent audio player
    tts_engine = create_tts_engine(cfg.tts)
    await tts_engine.warmup()
    player = AudioPlayer(device=cfg.audio.output_device, start_stream=not args.selftest)

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
    try:
        if args.replay:
            await replay(args.replay, engineer, args.speed)
            await asyncio.sleep(1)
            print(json.dumps(engineer.state.snapshot(), indent=2))
        else:
            transport, _ = await loop.create_datagram_endpoint(
                lambda: Listener(engineer.on_packet), local_addr=(host, port))
            log.info("listening on %s:%d (PTT = UDP Action %d)", host, port, ptt_action)
            try:
                await asyncio.Event().wait()
            finally:
                transport.close()
    finally:
        for t in tasks:
            t.cancel()
        if dashboard_server:
            await dashboard_server.stop()
        if recorder:
            recorder.close()
        if audio_capture:
            audio_capture.stop()
        if player:
            player.stop()
        if llm_client:
            await llm_client.aclose()
        if engineer.storage:
            engineer.storage.close()


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Voice Race Engineer for F1 25")
    p.add_argument("--config", default="config.yaml", help="path to config.yaml (default: config.yaml)")
    p.add_argument("--host", default=None, help="UDP host (overrides config)")
    p.add_argument("--port", type=int, default=None, help="UDP port (overrides config)")
    p.add_argument("--ptt-action", type=int, default=None, help="in-game 'UDP Action' number (overrides config)")
    p.add_argument("--record", help="save raw packets to this file")
    p.add_argument("--replay", help="replay a recorded file instead of listening")
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
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
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

    try:
        asyncio.run(amain(args, cfg, host, port, ptt_action, redis_url, redis_hz))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

