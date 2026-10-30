# Race Engineer: Architecture Map

## 1. System Overview

`race_engineer` is an AI-powered voice race engineer and real-time OLED pit wall dashboard for EA Sports F1 (F1 25 and the 2026 Season Pack, UDP format 2026). It ingests high-frequency UDP telemetry, maintains deterministic race state, runs rule-based and predictive detectors, speaks tactical engineering callouts with low latency (<500 ms target), responds to driver queries via local/cloud LLMs with factual guardrails, and serves an interactive web dashboard over WebSockets and REST.

---

## 2. Codebase Modules & Responsibilities

| Module | Purpose & Core Functions |
|---|---|
| `main.py` | Main CLI entry point and orchestration engine. Manages async event loop, UDP socket listener (port 20777), thread lifecycles, signal dispatching, recorder, replay with `--explain`, selftest runner, and graceful shutdown. |
| `config.py` | Hierarchical configuration loader (`config.yaml`). Validates YAML schema with dataclasses, applies defaults, and securely accesses OS keychain for API tokens. |
| `f1telemetry/packets.py` | Binary packet parsing for EA F1 UDP specification. Decodes packet headers and per-car telemetry structs across all packet IDs. |
| `f1telemetry/state.py` | Live `RaceState` maintaining running race conditions, driver index, session progress, fuel delta, tire wear/temps, stint tracker, gap trends, and session UID lifecycle. |
| `f1telemetry/deterministic.py` | Fast-path deterministic rule engine (`Callouts`, `route`). Immediately answers fuel, tyre, weather, gap, and pit window queries without invoking LLM. Houses priority-based `RadioQueue`. |
| `f1telemetry/spotter.py` | Proximity spotter (`Spotter`, `SpotterState`). Tracks cars alongside (left, right, 3-wide) using lateral distance and relative orientation. |
| `f1telemetry/overtakes.py` | `BrakingZoneDetector`, `OvertakeDetector`, and `DefendWarningDetector`. Analyzes speed traces, corner entries, inside looks, and switchbacks. |
| `agent.py` | `RaceEngineerAgent`. Integrates intent routing: evaluates driver questions deterministically first, falling back to local or cloud LLMs with strict number grounding. |
| `llm.py` | Multi-provider LLM client (`GroqProvider`, `OllamaProvider`, `OpenAICompatibleProvider`). Features fallback chains, streaming sentence chunker (`SentenceSplitter`), and numerical hallucination guardrails. |
| `stt.py` | Speech-To-Text client (`SpeechToText`). Runs local faster-whisper on CPU or falls back to Groq STT; utilizes vocabulary prompting with racing terminology. |
| `tts.py` | Text-To-Speech pipeline (`TTSProvider`, `PiperTTS`, `KokoroTTS`, `EdgeTTSProvider`). Synthesizes voice lines, disk-caches reusable static clips, and respects usage caps. |
| `radio_fx.py` | Vintage motorsports radio DSP filter. Applies 300Hz–3400Hz bandpass filtering, clipping distortion, opening mic clicks, and squelch noise bursts. |
| `audio_in.py` | Microphone audio capture (`AudioCapture`). Uses pre-roll ring buffer, energy/silence VAD, and background joystick/wheel button polling. |
| `audio_player.py` | Real-time audio playback worker (`AudioPlayer`). Handles sounddevice output with instant queue clearance and playback interruption on high-priority callouts. |
| `audio_devices.py` | Audio input and output hardware enumeration, device index resolution, and validation. |
| `knowledge.py` | Local circuit intelligence (`KnowledgeBase`). Parses markdown track notes and key corner characteristics without external vector database dependencies. |
| `latency.py` | End-to-end latency measurement (`LatencyTracker`). Tracks timing from UDP packet receipt to radio output and STT to TTS response. |
| `pace.py` | `PaceAnalyzer`. Computes fuel-corrected pace, evaluates slower laps with causal factors (traffic, deg, mistakes), compares qualifying gaps, and projects finishing positions. |
| `personality.py` | `PersonalityEngine`. Applies conversational persona layers (clean, mild, cocky) with cooldown gates, escalation counters, and swearing controls. |
| `radio_check.py` | Two-way in-game radio diagnostic tool (`RadioCheckManager`). Tests microphone transcription and speaker output in a guided sequence. |
| `sound_test.py` | In-app audio hardware diagnostic utility (`SoundTestManager`). Plays test tones and confirms hardware readiness. |
| `storage.py` | Persistent SQLite storage (`SessionStorage`, `LapTraceCollector`). Saves sessions, laps, telemetry traces (`.npz`), overtakes, and teammate deltas. |
| `analysis.py` | Post-lap and telemetry trace analysis (`compute_lap_debrief`, `get_lap_comparison_summary`). Calculates corner apex speeds, braking points, and delta-t curves. |
| `setups.py` | Car setup library (`SetupsStorage`). Captures setup packets, computes setup fingerprints, ranks setups by driver lap times, and handles CSV/JSON import/export. |
| `trackmap.py` | 2D track coordinate mapper (`TrackMapManager`). Normalizes world coordinates into SVG viewBox paths with sector boundaries. |
| `usage.py` | API quota safeguard (`UsageGuard`). Tracks cloud requests, token consumption, and edge-tts characters with daily caps, fallback triggers, and usage persistence. |
| `dashboard/server.py` | FastAPI web server and WebSocket broadcaster. Serves REST endpoints for settings, setups, tracks, usage, audio diagnostics, and live WebSocket telemetry at `/ws/live`. |
| `dashboard/static/` | Frontend OLED pit-wall UI (`index.html`, `style.css`, `app.js`). Implements live timing tower, telemetry traces, setups manager, track notes editor, usage monitor, and settings. |

---

## 3. Data Flow Pipelines

### 3.1 Live Telemetry & Voice Callout Pipeline
```
F1 25 / 26 Game (UDP Port 20777)
               │
               ▼
       `f1telemetry/packets.py` (Binary unpack header + payload)
               │
               ▼
        `f1telemetry/state.py` (RaceState update: session, player, laps, deltas)
               │
       ┌───────┴─────────────────────────────────────────────┐
       ▼                                                     ▼
Signals emitted                                   Detectors evaluated:
 - 'lap', 'damage', 'finished',                    - Spotter (left/right/clear)
 - 'penalty', 'track_limits_warning',              - Overtake & Defend detector
 - 'flashback', 'new_session'                      - Pace analyzer & Personality
       │                                                     │
       └───────────────────────┬─────────────────────────────┘
                               ▼
              `deterministic.py` (Callouts & route)
                               │
                               ▼
               `RadioQueue` (Priority push & drop)
                               │
                               ▼
        `TTSProvider` (Piper / Kokoro / Edge-TTS fallback)
                               │
                               ▼
            `radio_fx.py` (Bandpass, distortion, squelch)
                               │
                               ▼
            `audio_player.py` (Sounddevice audio output)
```

### 3.2 Voice Command & PTT Pipeline
```
Steering Wheel / Keyboard / Mic
               │
               ▼
         `audio_in.py` (Push-To-Talk / Energy VAD / Pre-roll buffer)
               │
               ▼
            `stt.py` (faster-whisper CPU / Groq STT fallback)
               │
               ▼  Text transcribed: "How's the fuel?"
           `agent.py`
          ┌────┴─────────────────────────────┐
          │ (Deterministic Intent Match)     │ (Open-ended query)
          ▼                                  ▼
 `deterministic.py` (Exact numbers)   `llm.py` (Groq / Ollama / OpenAI-compatible)
          │                                  │
          │                                  ▼
          │                   `check_number_grounding()` (Guardrail)
          └──────────────────┬───────────────┘
                             ▼
                        `RadioQueue`
```

### 3.3 Storage Pipeline
```
`RaceState` events & completed laps
               │
               ▼
   `storage.py` (`SessionStorage`, `LapTraceCollector`)
   ┌───────────┴───────────────────────────────┐
   ▼                                           ▼
SQLite (`storage.db` / `sessions.db`):      Compressed `.npz` traces:
 - `sessions`                                - 100Hz distance, speed,
 - `laps`                                    - throttle, brake, gear,
 - `lap_traces` (metadata)                   - steer, lateral/lon g-force
 - `overtakes`
 - `session_timeline`
```

### 3.4 Dashboard & WebSocket Pipeline
```
`RaceState` + `TrackMapManager`
               │
               ▼
`build_live_payload()` (Sampled at 10Hz - 30Hz)
               │
               ▼
`dashboard/server.py` (`WebSocketBroadcaster` at `/ws/live`)
               │
               ▼  JSON WebSocket frame
Browser Client (`dashboard/static/app.js`)
   ├── Live HUD (Speed, gear, RPM, fuel delta, tyre temps/wear)
   ├── Timing Tower (Positions, intervals, pit status, lap times)
   ├── SVG Telemetry Traces (Speed, throttle, brake vs distance)
   └── Radio Log & Connection Status
```

---

## 4. Database Schema

### Database: `storage.db` / `sessions.db`
- **`sessions`**: `session_uid` (PK), `track_id`, `track_name`, `session_type`, `session_name`, `car_class`, `weather`, `air_temp`, `track_temp`, `total_laps`, `started_at`, `ended_at`, `laps_completed`, `best_lap_ms`.
- **`laps`**: `session_uid`, `lap_number`, `car_index`, `driver_name`, `team_name`, `lap_time_ms`, `sector1_ms`, `sector2_ms`, `sector3_ms`, `is_valid`, `pit_status`, `compound`, `tyre_age_laps`, `wear_fl`, `wear_fr`, `wear_rl`, `wear_rr`, `fuel_remaining_kg`, `fuel_delta_laps`.
- **`lap_traces`**: `session_uid`, `lap_number`, `driver_index`, `lap_time_ms`, `trace_data` (BLOB / pointer to `.npz`), `created_at`.
- **`overtakes`**: `id` (PK AUTOINCREMENT), `session_uid`, `lap_number`, `timestamp`, `overtaking_driver`, `overtaken_driver`, `corner_number`, `classification` (`clean`, `brave`, `divebomb`, `divebomb_contact`, `switchback`, `passed_stricken_car`), `delta_m`, `notes`.
- **`session_timeline`**: `session_uid`, `timestamp`, `event_type`, `details`.
- **`session_teammate_deltas`**: `session_uid`, `lap_number`, `sector`, `delta_ms`.
- **`usage_records`**: `id` (PK AUTOINCREMENT), `timestamp`, `provider` (`groq_llm`, `groq_stt`, `edge_tts`), `requests`, `tokens_prompt`, `tokens_completion`, `characters`, `estimated_cost_usd`, `status_code`.

### Database: `setups.db`
- **`setups`**: `id` (PK AUTOINCREMENT), `track_id`, `track_name`, `car_class`, `team_id`, `setup_fingerprint` (UNIQUE), `setup_name`, `front_wing`, `rear_wing`, `diff_on_throttle`, `diff_off_throttle`, `front_camber`, `rear_camber`, `front_toe`, `rear_toe`, `front_suspension`, `rear_suspension`, `front_anti_roll_bar`, `rear_anti_roll_bar`, `front_ride_height`, `rear_ride_height`, `brake_pressure`, `brake_bias`, `front_left_pressure`, `front_right_pressure`, `rear_left_pressure`, `rear_right_pressure`, `ballast`, `fuel_load`, `best_lap_ms`, `driver_rating`, `notes`, `source`, `created_at`.

---

## 5. Dashboard Pages, REST & WebSocket Endpoints

### Single Page Application (Hash Routing)
- `#live`: Live telemetry HUD, digital dashboard, timing tower, and real-time SVG telemetry traces.
- `#laps`: Session lap history, sector time deltas, stint tyre breakdown, and lap trace comparison.
- `#setups`: Saved car setups, active setup parameters, CSV/JSON import, and setup ranking.
- `#tracks`: Circuit guide, turn notes editor, telemetry braking points, and session records.
- `#usage`: Cloud API usage counters, token/character limits, quota guards, and provider status.
- `#settings`: Audio hardware selection, volume sliders, network configuration, and hotkeys.

### WebSocket Endpoints
- `GET /ws/live?token=<token>`: High-frequency telemetry stream, initial state snapshot, radio messages, and timing tower.

### REST API Endpoints
- `GET /api/status`: Health check, telemetry stream state, and current session info.
- `GET /api/token`: Authenticates and retrieves server session token.
- `GET /api/settings/audio`: Lists active input and output audio hardware.
- `POST /api/settings/audio`: Selects microphone or speaker output devices.
- `GET /api/settings/volumes` & `POST /api/settings/volumes`: Reads/writes engineer, spotter, and menu volume levels.
- `POST /api/settings/test_sound`: Triggers test sound on selected speaker output.
- `POST /api/radio_check`: Initiates interactive 2-way radio check sequence.
- `GET /api/settings/ports`: Inspects telemetry UDP and dashboard HTTP ports.
- `GET /api/setups` & `POST /api/setups`: Lists setups or saves new setup configuration.
- `GET /api/setups/{id}`: Retrieves specific setup details.
- `POST /api/setups/import`: Uploads setup from JSON or CSV.
- `GET /api/setups/export`: Downloads setups as JSON or CSV.
- `GET /api/tracks/notes` & `POST /api/tracks/notes`: Fetches and saves circuit notes.
- `GET /api/usage`: Returns current daily usage counters and rate limit status.

---

## 6. Configuration Keys (`config.yaml`)

```yaml
network:
  udp_ip: "0.0.0.0"
  udp_port: 20777
  dashboard_host: "0.0.0.0"
  dashboard_port: 8765
  token: ""

audio:
  input_device: null
  output_device: null
  sample_rate: 16000
  channels: 1
  energy_threshold: 1000
  ptt_button: 1
  ptt_device: "joystick"
  pre_roll_ms: 300
  min_length_ms: 500

stt:
  provider: "faster_whisper"    # faster_whisper | groq
  model: "base.en"
  device: "cpu"
  compute_type: "int8"
  groq_model: "whisper-large-v3"

llm:
  primary: "groq"               # groq | ollama | openai_compatible
  groq:
    model: "llama-3.3-70b-versatile"
    temperature: 0.3
  ollama:
    base_url: "http://localhost:11434"
    model: "llama3:8b"
  openai_compatible:
    base_url: "http://localhost:8080/v1"
    model: "local"

tts:
  engine: "edge_tts"            # piper | kokoro | edge_tts
  edge_voice: "en-GB-RyanNeural"
  piper_model: "models/en_GB-alan-medium.onnx"
  kokoro_voice: "bm_george"
  radio_fx: true

volumes:
  engineer: 1.0
  spotter: 1.0
  menu_beeps: 0.8

personality:
  persona: "cocky"              # clean | mild | cocky
  profanity_level: 2            # 0 = none, 1 = mild, 2 = uncensored
  callout_frequency: "normal"

usage:
  groq_llm_daily_tokens: 500000
  groq_stt_daily_minutes: 120
  edge_tts_daily_chars: 100000
```

---

## 7. Entry Points & Runtime Scripts

- **`python main.py`**: Primary runtime daemon.
  - `--config <path>`: Specifies custom YAML config path.
  - `--record <file>`: Saves incoming UDP packets into binary replay file.
  - `--replay <file>`: Replays recorded session file.
  - `--speed <N>`: Multiplies replay playback speed.
  - `--explain`: Replays recording and outputs per-lap event logs to stdout without audio.
  - `--selftest`: Runs synthetic end-to-end selftest without requiring game or audio hardware.
  - `--list-devices`: Displays enumerated audio input and output devices and exits.
- **`run.bat`**: Windows quickstart launcher activating `.venv` and launching `main.py`.
- **`setup.bat`**: Windows initial installer creating Python virtual environment and installing `requirements.txt`.
