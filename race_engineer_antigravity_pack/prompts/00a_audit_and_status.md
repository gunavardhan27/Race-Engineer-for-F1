# Prompt 00a: Audit the existing project (no feature work yet)

```
Audit the current codebase against the full feature list below and write
antigravity_pack/FEATURE_STATUS.md. Do NOT change application code in this step, except copying
antigravity_pack/reference/docs/udp_spec_2026.h to docs/ if it's missing.

1. Map the project: write antigravity_pack/ARCHITECTURE_MAP.md with every module, what it does, the
   data flow (UDP → parser → state → detectors/callouts → radio queue → TTS; state → storage;
   state → dashboard), the database tables, the dashboard pages and API/WebSocket endpoints, the
   config keys and the entry points (main.py, app.py, run/build scripts).

2. Run everything you can: the full test suite; the parser against the reference cross-check (feed
   synthetic packets of the exact 2026 sizes from docs/udp_spec_2026.h through both the project's
   parser and antigravity_pack/reference/f1telemetry/packets.py and compare every field for all 24
   cars); inspect_telemetry and `--replay ... --explain` on any recordings in data/ if they exist.

3. For each item below, record: status (Done / Partial / Missing / Broken / Unknown), evidence (file
   paths, functions, tests, or how you verified), and what's left. Be strict: "Done" means it exists,
   is wired in, and is tested.

Features by prompt file:
  01 config.yaml + latency tracker + --list-devices
  02 PTT audio capture (pre-roll, min length), faster-whisper on CPU, vocabulary prompt
  03 LLM providers (Groq, Ollama native API, OpenAI-compatible), fallback, streaming to sentences,
     system prompt file, number guardrail
  04 TTS engines (Piper/Kokoro/Edge), radio FX, playback with interruption
  05 track knowledge loading + section selection
  06 run scripts, README, --selftest
  07 live dashboard (WebSocket, timing tower, token, QR, reconnect)
  08 Motion packet + track map
  09 storage (sessions, laps, traces) + lap analysis + Laps page
  10 setup library (CarSetups capture, ranking, import, Setups page)
  11 manual entry screens (setup form from YAML, track notes editor, REST API)
  12 personality system (lines, profanity levels, triggers, cooldowns)
  13 usage guard (Groq LLM, Groq STT, edge-tts limits, fallbacks, Usage panel)
  14 raw recording + ML-ready data + data report + parquet export
  15 command system (UDP Action buttons, tap/hold, radio menu, touch MFD, hotkeys)
  16 desktop app packaging (pywebview, tray, single instance, installer)
  17 radio check (in game) + sound test (in app)
  18 pace judgment (slower laps with reasons, quali gaps, Time Trial, expected performance)
  19 finish detection, PENA decoding, data persistence on reconnect, --explain
  20 2026 packet format everywhere (24 cars, new layouts, CarTelemetry2 use)
  21 track model from all cars + overtake detection v2 + defend warnings
  22 sector / mini-sector comparison on the track map
  23 live map with smooth car movement + replay player
  24 tyre degradation model + strategy predictor
  25 seed data import (setups, track notes, track/team id learning)

Known issues reported by the user — check each specifically, with a test or a replay if possible:
  K1  Per-car arrays assume 22 cars instead of 24 (2026) → wrong data for other cars
  K2  Participant names/teams read from the wrong offsets (2026 uses uint16 ids, name at byte 10)
  K3  State resets when packets arrive with session_uid 0 → data cleared mid-session
  K4  Win/podium message doesn't fire, or fires more than once; no victory margin
  K5  No overtake messages; no clean/brave/divebomb classification
  K6  No warning when a car is close behind approaching a corner
  K7  "250-second penalty" (PENA time field 255 spoken) on track limits, also in Time Trial
  K8  Dashboard data clears on page refresh
  K9  Ctrl+C doesn't stop run.bat; a leftover instance keeps UDP 20777 bound → "connecting" forever
  K10 TTS cache (cache/audio/*.npz) grows without limit; dynamic text cached
  K11 Dashboard shows "connecting" when the engine failed to start, instead of a clear error

4. End FEATURE_STATUS.md with a build plan: the prompt files to run, in dependency order, skipping
   items that are Done, and listing which parts of each prompt are actually needed. Fixes for K1–K3
   (data correctness) come first, because other features depend on correct data.

Then stop and show me the plan.
```
