# Feature Status & Audit Report

**Date of Audit:** October 31, 2026  
**Audited Against:** Prompt Pack (00–25), EA F1 2026 Season Pack UDP Specification (`docs/udp_spec_2026.h`), Reference Parser (`antigravity_pack/reference/f1telemetry/`), and Reported Issues (K1–K11).  
**Test Suite Baseline:** 194 passing unit tests (`python -m pytest tests/`).

---

## 1. Feature Audit Matrix (Prompts 01–25)

| Prompt | Feature Description | Status | Evidence (Files, Functions, Tests) | What's Left / Gaps |
|---|---|---|---|---|
| **01** | `config.yaml` + latency tracker + `--list-devices` | **Done** | `config.py` (`load_config`), `latency.py` (`LatencyTracker`), `audio_devices.py` (`print_audio_devices`), `main.py`. Tests: `test_config.py`, `test_latency.py`, `test_audio_devices.py`. | Fully implemented and tested. |
| **02** | PTT audio capture (pre-roll, min length), faster-whisper on CPU, vocabulary prompt | **Done** | `audio_in.py` (`AudioCapture`, ring buffer, VAD, joystick listener), `stt.py` (`SpeechToText`, faster-whisper CPU, `build_initial_prompt`). Tests: `test_audio_in.py`, `test_stt.py`. | Fully implemented and tested. |
| **03** | LLM providers (Groq, Ollama, OpenAI-compatible), fallback, streaming sentences, system prompt file, number guardrail | **Done** | `llm.py` (`GroqProvider`, `OllamaProvider`, `OpenAICompatibleProvider`, `SentenceSplitter`, `check_number_grounding`), `prompts/system.md`, `agent.py`. Tests: `test_llm.py`, `test_agent.py`. | Fully implemented and tested. |
| **04** | TTS engines (Piper, Kokoro, Edge), radio FX, playback with interruption | **Done** | `tts.py` (`PiperTTS`, `KokoroTTS`, `EdgeTTSProvider`), `radio_fx.py` (`apply_radio_fx`, squelch, clicks), `audio_player.py` (`AudioPlayer.interrupt`). Tests: `test_tts.py`, `test_radio_fx.py`. | Edge-TTS disk cache needs cleanup bounds (see K10). |
| **05** | Track knowledge loading + section selection | **Done** | `knowledge.py` (`KnowledgeBase.get_section`, `get_corner_names`). Tests: `test_knowledge.py`. | 25 seed markdown files in `data/knowledge/tracks/` ready for import (Prompt 25). |
| **06** | Run scripts, README, `--selftest` | **Done** | `run.bat`, `setup.bat`, `README.md`, `main.py` (`selftest`). Tests: `test_selftest.py`. | `run.bat` needs port kill/cleanup to prevent leftover instances (see K9). |
| **07** | Live dashboard (WebSocket, timing tower, token, QR, reconnect) | **Done** | `dashboard/server.py` (`FastAPI`, `/ws/live`, `check_token`), `dashboard/static/` (`index.html`, `style.css`, `app.js`). Tests: `test_dashboard.py`. | Fully wired in with OLED theme. |
| **08** | Motion packet + track map | **Partial** | `trackmap.py` (`TrackMapManager`, SVG outline generation). Tests: `test_trackmap.py`. | Motion packet currently decodes 2025 float g-forces and 22 cars. Needs 2026 int16 g-forces (Prompt 20) and track model overhaul (Prompt 21). |
| **09** | Storage (sessions, laps, traces) + lap analysis + Laps page | **Done** | `storage.py` (`SessionStorage`, `LapTraceCollector`), `analysis.py` (`get_lap_comparison_summary`, `compute_lap_debrief`), `#laps` in dashboard. Tests: `test_storage.py`, `test_analysis.py`. | Fully implemented and tested. |
| **10** | Setup library (CarSetups capture, ranking, import, Setups page) | **Done** | `setups.py` (`SetupsStorage`, `compute_setup_fingerprint`, `import_from_json`, `export_as_sheet`), `#setups` UI. Tests: `test_setups.py`. | Seed setups (275 setups) in `data/setups_seed.json` ready for import (Prompt 25). |
| **11** | Manual entry screens (setup form from YAML, track notes editor, REST API) | **Done** | `setup_params.yaml`, `dashboard/server.py` (`/api/setups`, `/api/tracks/notes`), `#tracks` and `#setups` editors. Tests: `test_setup_inputs.py`. | Fully implemented and tested. |
| **12** | Personality system (lines, profanity levels, triggers, cooldowns) | **Done** | `personality.py` (`PersonalityEngine`), `lines/cocky.yaml`, profanity filter. Tests: `test_personality.py`. | Overtakes v2 trigger integration in Prompt 21; new lines in `data/lines/cocky.yaml` to merge in Prompt 25. |
| **13** | Usage guard (Groq LLM, Groq STT, edge-tts limits, fallbacks, Usage panel) | **Done** | `usage.py` (`UsageGuard`), `#usage` dashboard panel. Tests: `test_usage.py`. | Fully implemented and tested. |
| **14** | Raw recording + ML-ready data + data report + parquet export | **Partial** | `main.py` (`Recorder` basic binary recording), `storage.py` (laps & sessions tables). | Missing: background zstandard compression (`.bin.zst`), 20GB disk rotation, `SessionHistory` packet 11, expanded `lap_features` / corner metrics, `radio_log` table, `--export parquet`, and `--data-report`. |
| **15** | Command system (UDP Action buttons, tap/hold, radio menu, touch MFD, hotkeys) | **Partial** | `commands.yaml` has stub for `radio_check`. | Missing: full commands dictionary (gaps, tyres, fuel, weather, pit_window, damage, etc.), BUTN tap (<300ms) vs hold PTT logic, 3-button radio MFD, hotkeys, touch MFD on dashboard. |
| **16** | Windows app packaging (pywebview, tray, single instance, installer) | **Missing** | Intentionally left for the very end per instructions. | Missing: PyInstaller spec, pywebview desktop wrapper, tray icon, single instance mutex, Inno Setup script. |
| **17** | Radio check (in game) + sound test (in app) | **Done** | `radio_check.py` (`RadioCheckManager`), `sound_test.py` (`SoundTestManager`), dashboard settings integration. Tests: `test_radio_check.py`, `test_sound_test.py`. | Fully implemented and tested. |
| **18** | Pace judgment (slower laps with reasons, quali gaps, Time Trial, expected performance) | **Done** | `pace.py` (`PaceAnalyzer`, slower laps reasons: traffic, damage, deg, weather, mistakes; quali teammate deltas; expected finish). Tests: `test_pace.py`. | Fully implemented and tested. |
| **19** | Event fixes (finish detection, PENA decoding, data persistence on reconnect, `--explain`) | **Done** | `f1telemetry/state.py` (session_uid 0 guard, finish detection), `personality.py` (margin bucketing, provisional penalty, 8s suppression, FinalClassification correction), `f1telemetry/deterministic.py` (PENA decoding, 255 N/A, >60s anomaly guard, TT suppression, deduplication), `dashboard/server.py` (reconnect full state), `storage.py` (session_timeline compressed chunks, resumption), `main.py` (`ExplainTracker`, `--replay <file> --explain`). Tests: `tests/test_detectors.py`, `tests/test_events_and_explain.py` (28/28 passed). | Fully implemented and tested. (Overtake detection & defend warnings superseded by Prompt 21). |
| **20** | 2026 packet format everywhere (24 cars, new layouts, CarTelemetry2 use) | **Done** | `f1telemetry/packets.py`, `f1telemetry/state.py`, `f1telemetry/deterministic.py`, `personality.py`, `dashboard/server.py`, `dashboard/static/app.js`. Tests: `tests/test_2026_packet_format.py` (7/7 passed). | Fully implemented: 24 cars dynamically, 2026 structs, CarTelemetry2 active aero/overtake mode, wrong way P0 call, COLL severity, uint16 team IDs, startup size verification. |
| **21** | Track model from all cars + overtake detection v2 + defend warnings | **Done** | `trackmodel.py` (`TrackModel`, `TrackModelBuilder`, `TrackModelStorage`), `f1telemetry/overtakes.py` (24 cars tracking, 80m/2s confirmation, flicker rejection, classes, lost passes, defend warnings), `dashboard/server.py` (`/api/track_model*`). Tests: `tests/test_trackmodel_and_overtakes_v2.py`, `tests/test_detectors.py`. | Fully implemented and tested. Fixes K5 & K6. |
| **22** | Sector / mini-sector comparison on the track map | **Done** | `f1telemetry/packets.py` (Packet 11 SessionHistory, Participants livery colours/telemetry), `minisectors.py` (10m grid interpolation, compressed storage in `lap_timing`, 25 mini-sectors, `compare_laps`, `get_fastest_by_minisector`, `get_theoretical_best`, `get_f1_sector_colors`), `dashboard/server.py` (`/api/laps/minisector_comparison`, `/api/laps/fastest_minisectors`, `/api/laps/theoretical_best`, `/api/laps/f1_colors`), `dashboard/static/index.html` & `app.js` (4 comparison modes, interactive track map canvas, 25-block delta strip, hover popovers with loss diagnoses, theoretical best, F1 colored sectors), `main.py` & `f1telemetry/deterministic.py` (snapshot fields, deterministic voice intents). Tests: `tests/test_minisectors.py` (9/9 passed). | Fully implemented and tested. |
| **23** | Live map with smooth car movement + replay player | **Done** | `positions.py` (binary protocol 10-byte header + 15 bytes/car for up to 24 cars, linear position & shortest-angle heading interpolation, velocity extrapolation up to 200ms, fallback placement by lap distance, non-blocking 20Hz broadcaster), `replay_manager.py` (replay sessions, transparent `.zst` decompression, keyframe index every ~1.5s, timeline markers for SC, overtakes, pit stops, incidents, instant seeking, speed multipliers 1x/2x/4x/8x), `dashboard/server.py` (`/ws/positions`, `/api/replay/*`), `dashboard/static/index.html` & `app.js` (two-layer 60fps canvas, offscreen static layer with ribbon/centreline/sectors/corners/aero, dynamic layer with battle links <1s, yellow marshal zones, safety car/VSC banner, gap bubbles, overtake beacon pulses, tap to follow car + popover, replay controls). Tests: `tests/test_positions_and_replay.py` (6/6 passed). | Fully implemented and verified in browser. |
| **24** | Tyre degradation model + strategy predictor | **Missing** | Only linear wear rate currently computed in `RaceState`. | Implement non-linear tyre deg curve fitting, pit loss simulation, undercut/overcut strategy engine. |
| **25** | Seed data import (setups, track notes, track/team id learning) | **Done** | `seed_import.py` (`SeedImporter`: 275 setups + 25 derived base setups, 25 track notes non-destructive merge, 181 turns + track model matching, persona merge), `dashboard/server.py` (`/api/seed/*`, `/api/tracks/confirm_mapping`, `/api/teams/confirm_mapping`), `#sec-seed` in Settings. Tests: `tests/test_seed_import.py` (8/8 passed). | Fully implemented and tested. Seed data loaded into `setups.db`, `tracks.db`, and `knowledge/tracks/`. |
| **26** | Reliability fixes: shutdown, single instance, startup errors, audio cache (K9, K10, K11) | **Done** | `single_instance.py`, `port_checker.py`, `audio_cache.py`, `config.py`, `tts.py`, `main.py`, `dashboard/server.py`, `app.js`. Tests: `tests/test_reliability.py` (8/8 passed). | Fully implemented and tested. |

---

## 2. Audit of Known Reported Issues (K1–K11)

| Issue ID | Description | Codebase Status | Findings & Diagnosis | Required Fix |
|---|---|---|---|---|
| **K1** | Per-car arrays assume 22 cars instead of 24 (2026) | **Fixed / Done** | `f1telemetry/packets.py`: `num_cars(h)` returns 22 for <=2025, 24 for 2026+. `RaceState._mine()` checks `len(cars)`. Car 23 works end-to-end. | Fixed in Prompt 20. Verified in `tests/test_2026_packet_format.py`. |
| **K2** | Participant names/teams read from wrong offsets in 2026 | **Fixed / Done** | `parse_participants` reads `team_id` at offset 5 (`<H`), `race_number` at offset 8, `m_name` at offset 10 for format 2026. Backward-compatible with 2025. | Fixed in Prompt 20. Verified in `tests/test_2026_packet_format.py`. |
| **K3** | State resets when packets arrive with `session_uid == 0` | **Fixed / Done** | `f1telemetry/state.py` lines 120–122 explicitly checks `if h.session_uid == 0: return signals`. | Keep guard in place and ensure no secondary handlers reset on 0. |
| **K4** | Win/podium message doesn't fire, or fires more than once | **Fixed / Done** | Finish detection in `f1telemetry/state.py` on `result_status` 2->3 with gap ahead/behind margins. Margin bucketing, unserved provisional penalties, 8s suppression window, and single-fire per session_uid in `personality.py`. FinalClassification correction only when position differs. | Verified in `tests/test_detectors.py` and `tests/test_events_and_explain.py`. |
| **K5** | No overtake messages; no clean/brave/divebomb classification | **Fixed / Done** | Multi-car track model builds centreline, width, corners and braking zones from all 24 cars from lap 1. Order tracking for 24 cars within 60m, confirmed on 2.0s or 80m, flicker rejection, clean/brave/divebomb/divebomb_contact/around_the_outside/switchback/overtake_mode_pass and lost passes. | Fixed in Prompt 21. Verified in `tests/test_trackmodel_and_overtakes_v2.py`. |
| **K6** | No warning when a car is close behind approaching a corner | **Fixed / Done** | `DefendWarningDetector` uses `TrackModel` braking zones with inside-look detection (lateral offset > 1.2m to inside) and spotter priority. | Fixed in Prompt 21. Verified in `tests/test_trackmodel_and_overtakes_v2.py`. |
| **K7** | "250-second penalty" spoken on track limits (PENA time 255) | **Fixed / Done** | `f1telemetry/deterministic.py` lines 470–475 checks `sec == 255` (treated as N/A) and ignores anomalies `sec > 60`. Time Trial suppresses penalties entirely. | Verified; will ensure unit tests specifically assert PENA 255 produces no spoken penalty. |
| **K8** | Dashboard data clears on page refresh | **Fixed / Done** | `dashboard/server.py` lines 1398–1423 immediately sends full current snapshot (`snap`), `laps_history`, `radio_history`, and `overtakes` upon WebSocket connection. | Verified; refreshed client restores full timing tower, telemetry HUD, and radio messages. |
| **K9** | Ctrl+C doesn't stop `run.bat`; leftover instance keeps UDP 20777 bound | **Fixed / Done** | Implemented `SingleInstanceLock` in `single_instance.py`, cross-platform signal handling with SIGINT/SIGBREAK in `main.py`, 5s watchdog with `os._exit(0)`, uvicorn signal hijacking disabled, all background threads daemonized. Port conflicts diagnosed via `port_checker.py`. | Fixed in Prompt 26. Verified in `tests/test_reliability.py`. |
| **K10** | TTS cache (`cache/audio/*.npz`) grows without limit | **Fixed / Done** | Implemented `AudioCacheManager` in `audio_cache.py`: only static phrases without digits/placeholders cached; stored as int16 (50% disk savings); orphan cleanup at startup; LRU eviction with `cache.audio_max_mb` (default 100MB); `--clear-cache` CLI and Settings UI. | Fixed in Prompt 26. Verified in `tests/test_reliability.py`. |
| **K11** | Dashboard shows "connecting" when engine failed to start | **Fixed / Done** | Added 4 distinct states: Engine Offline, Engine Error (with banner & exact error message e.g. port conflict), Standby (waiting for game), and Live. "Connecting" strictly reserved for active browser WebSocket handshake. | Fixed in Prompt 26. Verified in `tests/test_reliability.py`. |

---

## 3. Recommended Build Plan

In accordance with Prompt 00 and 00a instructions, data correctness (K1–K3) must precede all downstream telemetry analytics.

```
                  ┌─────────────────────────────────────────────────────────┐
                  │ 20: 2026 Packet Format (24 Cars, 2026 Layouts, Fixes K1/K2)│
                  └───────────────────────────┬─────────────────────────────┘
                                              │
                                              ▼
                  ┌─────────────────────────────────────────────────────────┐
                  │ 19: Event Fixes (PENA, Finish K4, State Resets, --explain)│
                  │ [Milestone Check: Replay with --explain]                │
                  └───────────────────────────┬─────────────────────────────┘
                                              │
                                              ▼
                  ┌─────────────────────────────────────────────────────────┐
                  │ 21: Track Model & Overtakes v2 (Fixes K5, K6)           │
                  │ [Milestone Check: Test race / Replay overtake detection]│
                  └───────────────────────────┬─────────────────────────────┘
                                              │
                                              ▼
                  ┌─────────────────────────────────────────────────────────┐
                  │ 25: Seed Reference Data Import (Setups, Tracks, Notes)  │
                  └───────────────────────────┬─────────────────────────────┘
                                              │
                                              ▼
                  ┌─────────────────────────────────────────────────────────┐
                  │ 22: Sector & Mini-Sector Comparison Map                 │
                  └───────────────────────────┬─────────────────────────────┘
                                              │
                                              ▼
                  ┌─────────────────────────────────────────────────────────┐
                  │ 23: Live Map with Moving Cars & Replay Player           │
                  └───────────────────────────┬─────────────────────────────┘
                                              │
                                              ▼
                  ┌─────────────────────────────────────────────────────────┐
                  │ 24: Tyre Degradation Model & Strategy Prediction        │
                  └───────────────────────────┬─────────────────────────────┘
                                              │
                                              ▼
                  ┌─────────────────────────────────────────────────────────┐
                  │ 14: ML-Ready Storage, Parquet Export & Data Report      │
                  │ 15: Command System (Radio Menu, MFD Buttons, Tap/Hold)  │
                  │ Quality Fixes: K9 (run.bat), K10 (TTS cache), K11 (UI)  │
                  └───────────────────────────┬─────────────────────────────┘
                                              │
                                              ▼
                  ┌─────────────────────────────────────────────────────────┐
                  │ 16: Windows Packaging & Installer (Final Step)          │
                  └─────────────────────────────────────────────────────────┘
```

### Execution Steps Breakdown:

1. **Step 1: Prompt 20 – 2026 Packet Format Everywhere (Fixes K1, K2)**
   - Merge `antigravity_pack/reference/f1telemetry/packets.py` into `f1telemetry/packets.py`:
     - Dynamically support 24 cars for 2026 packets (`NUM_CARS = 24`), 22 for 2025.
     - Implement 2026 struct formats: `TEL_2026`, `STATUS_2026`, `MOTION_2026`, `TEL2_2026` (`PacketId.CAR_TELEMETRY2 = 16`).
     - Fix participant offsets: `team_id` at offset 5 (`uint16`), `m_name` at offset 10 (`32s`).
     - Preserve custom project packet definitions (`CAR_SETUPS`, `FINAL_CLASSIFICATION`, `TIME_TRIAL`).
   - Run unit tests and packet cross-check test.

2. **Step 2: Prompt 19 – Race Event Detection & Replay Fixes (Fixes K4, K7, K8)**
   - Verify PENA decoding rules and time 255 guard.
   - Refine win/podium detection (K4) and ensure victory margin calculation is resilient.
   - Run `--explain` replay check on recorded data or synthetic stream.

3. **Step 3: Prompt 21 – Track Model & Overtakes v2 (Fixes K5, K6)**
   - Build real-time track model from all 24 cars motion packets (no reliance on prior player lap trace).
   - Implement robust overtake confirmation and classification (`clean`, `brave`, `divebomb`, `switchback`).
   - Implement corner defend warning detector with inside-look detection.
   - Validate with replay check.

4. **Step 4: Prompt 25 – Import Reference Data**
   - Import 275 seed setups from `data/setups_seed.json` into `setups.db`.
   - Import 181 key corners from `data/track_turns.json` and 25 track notes into `knowledge/tracks/`.
   - Align corners to the track model from Prompt 21.

5. **Step 5: Prompt 22 – Sector & Mini-Sector Comparison**
   - Compute mini-sector deltas along track distance.
   - Render mini-sector comparison visualizer on the dashboard track map.

6. **Step 6: Prompt 23 – Live Track Map with Moving Cars & Replay**
   - Render real-time interpolated positions for all 24 cars on SVG track map.
   - Add replay scrubber player in dashboard.

7. **Step 7: Prompt 24 – Tyre Degradation & Strategy Predictor**
   - Implement non-linear tyre deg model and pit window prediction.
   - Add strategy recommendation to radio queue and dashboard.

8. **Step 8: Missing Core Features from 01–18 + Polish**
   - **Prompt 14**: Add Parquet export (`--export parquet`) and data report (`--data-report`).
   - **Prompt 15**: Complete commands system (radio menu, tap/hold, MFD buttons in UI).
   - **K9 / K10 / K11 Fixes**: Port cleanup in `run.bat`, TTS cache bounds, dashboard engine status error modal.

9. **Step 9: Prompt 16 – Windows Packaging (PyInstaller & Inno Setup)**
   - Package application into installable Windows bundle at the very end.
