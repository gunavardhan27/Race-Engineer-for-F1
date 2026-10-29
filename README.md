# Race Engineer for F1 25

An intelligent, low-latency voice race engineer for EA Sports **F1 25** on Windows 11.

---

## Key Highlights

- **Dual-Path Routing**: Instant telemetry lookups (gaps, fuel delta, tyre wear, damage, weather forecast, pit window) are answered deterministically in $<1\text{ ms}$; open-ended race questions (handling, setup adjustments, race strategy) are streamed through an LLM.
- **Strict Data Grounding**: Numbers spoken to the driver always originate directly from telemetry packets, never hallucinated by an LLM.
- **Zero In-Game Stutter (RTX 2050 4 GB VRAM Safe)**: All local AI (Faster-Whisper STT, Piper TTS, local LLM) runs strictly on CPU by default, keeping the laptop's GPU VRAM 100% dedicated to F1 25.
- **Genuine Team Radio Acoustics**: Digital signal processing via SciPy (300–3400 Hz band-pass filter, soft compression saturation, low-level static noise, mic click burst, and trailing squelch).
- **Instant PTT Interruption**: If the driver keys the radio while the engineer is speaking, playback terminates immediately within 40 ms.

---

## 1. Quick Installation (Windows 11)

### Option A: Automatic Setup (Recommended)
1. Double-click `setup.bat`. This automatically:
   - Creates a Python virtual environment in `.venv`
   - Activates the environment
   - Installs pinned dependencies from `requirements.txt`
2. Run `run.bat` to launch the race engineer.

### Option B: Manual Setup
```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

---

## 2. In-Game F1 25 Telemetry Settings

Navigate to **Game Options** $\rightarrow$ **Settings** $\rightarrow$ **Telemetry Settings**:

| Setting | Required Value |
|---|---|
| **UDP Telemetry** | **On** |
| **UDP IP Address** | **127.0.0.1** |
| **UDP Port** | **20777** |
| **UDP Send Rate** | **20 Hz** |
| **UDP Format** | **2025** |

### Push-to-Talk (PTT) Wheel Binding
1. Go to **Controls, Calibration & Vibration** in F1 25.
2. Select your steering wheel or gamepad profile $\rightarrow$ **Edit Controls**.
3. Under the **UDP Actions** section, bind **UDP Action 1** to your desired radio button on the steering wheel.
4. Holding this button down records speech; releasing it triggers transcription and response.

---

## 3. Audio Configuration & Discovery

To list your microphone and headset device IDs:
```powershell
run.bat --list-devices
```
Sample output:
```
INPUT DEVICES (Microphones):
   1 | Microphone (Realtek Audio)         | Windows WASAPI   |     2 |  48000 Hz (DEFAULT)

OUTPUT DEVICES (Headsets / Speakers):
   2 | Headphones (HyperX Cloud Alpha)    | Windows WASAPI   |     2 |  48000 Hz (DEFAULT)
```
Update `config.yaml` with your preferred device names or indices:
```yaml
audio:
  input_device: 1       # Or microphone name
  output_device: 2      # Or headset name
  sample_rate: 16000
```

---

## 4. LLM Backend Selection (Groq vs Ollama vs llama.cpp)

Configure your preferred LLM provider in `config.yaml`:

```yaml
llm:
  primary: groq               # "groq" | "ollama" | "openai_compatible"
  fallback: ollama            # "ollama" | "openai_compatible" | null
  timeout_s: 4.0
  max_tokens: 60
```

### Recommendation Guide
1. **Groq (`llama-3.1-8b-instant`) [Recommended Primary]**:
   - Blazing fast cloud inference (~200–300 ms time-to-first-token).
   - Set environment variable: `set GROQ_API_KEY=your_key_here`.
2. **Ollama (`qwen2.5:3b-instruct`) [Recommended Local / Offline Fallback]**:
   - 100% offline and free.
   - Run `ollama pull qwen2.5:3b-instruct`.
   - Set `num_gpu: 0` in `config.yaml` to guarantee CPU execution.
   - Generates replies in ~1–2 seconds on a modern laptop CPU.
3. **llama.cpp / LM Studio (`openai_compatible`)**:
   - Any local OpenAI-compatible endpoint running at `http://localhost:8080/v1`.
   - See [docs/LOCAL_LLM.md](docs/LOCAL_LLM.md) for step-by-step setup instructions.

---

## 5. End-to-End Self-Test (`--selftest`)

Before starting the game, run the built-in end-to-end self-test:
```powershell
run.bat --selftest
```
This replays a synthetic Silverstone race sequence, tests a deterministic lookup (`"What's the gap ahead?"`), runs an AI question (`"I'm washing out at Copse, front left is cooking"`), synthesizes radio audio through your headphones, and prints a stage-by-stage latency report.

---

## 6. Live Web Dashboard (Phone / Tablet / Browser HUD)

A responsive, high-contrast, offline-first sim-racing web dashboard runs on FastAPI at `http://localhost:8765`:

- **Gauges & Telemetry**: Position, Speed (KPH), Gear, DRS, Lap, Personal Best Last Lap.
- **Interval Gaps**: Ahead and behind with per-lap trend arrows ($\Delta$ s/lap).
- **Four-Corner Tyre Widget**: Wear % and surface temperatures (°C) color-coded for thermal degradation.
- **Fuel & ERS**: Fuel in tank (kg), fuel laps delta, burn rate (kg/lap), and live 0–100% ERS battery bar.
- **Live Track Map**: Real-time circuit outline reconstructed automatically from clean lap samples (~5 m intervals) and cached in SQLite (`tracks.db`). Visualizes circuit ribbon, start/finish line, sector 2/3 boundaries, all cars as dots with position numbers, player highlighted in cyan, ahead/behind rivals in amber, and pitting cars greyed out. Includes one-click outline rebuilding.
- **G-Force Friction Circle**: Real-time lateral, longitudinal, and vertical G-load visualizer with concentric 1G/2G/3G thresholds and fading acceleration trail.
- **Timing Tower**: Full race leaderboard ordered by position with car name, team, gaps, tyre compound/age, stops, and penalties.
- **Team Radio Feed**: Live transcripts of driver questions and engineer callouts with stage latency tags.
- **Laps & Telemetry Analysis View**:
  - Compare any lap to your personal best or reference lap on a shared 5 m distance axis.
  - Responsive charts: Speed overlay, Throttle & Brake inputs, and Delta-to-best ($\Delta t$) trace.
  - Delta-colored track map: visually displays where you are gaining (green) or losing (red) lap time.
  - Corner breakdown table: apex minimum speed, braking point, throttle pickup, and time delta per turn.
  - Stint analysis: fitted pace degradation slope (s/lap) and tyre wear rate (%/lap).
- **Persistent Storage (`sessions.db`)**: SQLite with WAL mode and dedicated non-blocking writer thread. Full rollback on flashbacks; traces compressed as compact NumPy zlib blobs.

### Opening on Phone or Tablet
On startup, a secure random token is generated and an ASCII QR code is printed directly into your terminal. Point your phone camera at the terminal screen to open the dashboard over your local Wi-Fi network.

### Windows Firewall Configuration (Private Networks Only)
To access the live dashboard on your phone or secondary screen, allow incoming TCP traffic on port `8765` for **Private networks only** (never on Public networks):

**Using PowerShell (Run as Administrator):**
```powershell
New-NetFirewallRule -DisplayName "Race Engineer Dashboard" -Direction Inbound -LocalPort 8765 -Protocol TCP -Action Allow -Profile Private
```

**Using Windows Defender Firewall GUI:**
1. Open **Windows Defender Firewall with Advanced Security**.
2. Click **Inbound Rules** $\rightarrow$ **New Rule...**
3. Select **Port** $\rightarrow$ Next $\rightarrow$ **TCP**, Specific local ports: `8765`.
4. Select **Allow the connection** $\rightarrow$ Next.
5. In the Profile window, **check Private only** (uncheck Domain and Public) $\rightarrow$ Next.
6. Name the rule `Race Engineer Dashboard` and click Finish.

---

## 7. Recording & Replaying Sessions

### Record a Live Session
To record live telemetry packets while driving for offline testing and tuning:
```powershell
run.bat --record session_silverstone.bin
```

### Replay a Recorded Session
To replay telemetry packets at 4x speed without launching the game:
```powershell
run.bat --replay session_silverstone.bin --speed 4.0
```

---

## 8. Troubleshooting

- **No telemetry packets received**:
  1. Check that F1 25 UDP Telemetry is enabled and set to `127.0.0.1` port `20777`.
  2. Ensure Windows Defender Firewall allows incoming UDP traffic on port `20777`.
- **Dashboard unreachable from phone / tablet**:
  1. Ensure both the PC and phone are connected to the same Wi-Fi/LAN router.
  2. Verify that Windows Firewall allows port `8765` on Private networks (see Section 6 above).
  3. Ensure your Windows network profile is set to **Private Network** (Settings $\rightarrow$ Network & Internet $\rightarrow$ Wi-Fi/Ethernet $\rightarrow$ Network profile type: Private).
- **Audio input or output errors**:
  Run `run.bat --list-devices` and confirm the device indices in `config.yaml`.
- **In-game frame rate drops or stutter**:
  Ensure all local AI is running on CPU. Verify in `config.yaml` that:
  - `stt.device: "cpu"`
  - `tts.device: "cpu"`
  - `llm.ollama.num_gpu: 0`
  Do not offload models to the RTX 2050 GPU while the game is running.

---

## 9. First Real Session Verification Checklist

When connecting to F1 25 for the first time:
- [ ] Run `run.bat --list-devices` to verify headset and microphone device IDs.
- [ ] Run `run.bat --selftest` to ensure TTS and LLM pipeline are functional.
- [ ] Launch F1 25 and confirm UDP Telemetry is set to **Format 2025**, **20 Hz**, **127.0.0.1:20777**.
- [ ] Start `run.bat --record session_test.bin`.
- [ ] Scan the terminal QR code to open the dashboard on your phone.
- [ ] Drive 2 laps in Time Trial or Grand Prix mode.
- [ ] Test the steering wheel PTT button ("UDP Action 1"). Verify `push-to-talk DOWN` and `push-to-talk UP` appear in console.
- [ ] Stop the app and replay the session: `run.bat --replay session_test.bin`.
- [ ] **Check terminal logs for packet warnings**:
  Ensure there are no `"packet ... expected car stride"` warnings. If this warning appears, a game update or seasonal patch changed the packet structure.

