# Build prompts for Antigravity

Paste these into the Antigravity agent **one at a time, in order**. After each one, review the diff
and run `python test_synthetic.py` plus any new tests before moving on. Prompt 0 sets the ground
rules; keep it at the top of the conversation (or in the agent's rules/memory if you use that).

---

## Prompt 0: Project context and rules

```
You are working on "race_engineer", a voice race engineer for the F1 25 game on Windows.

What already exists (read every file before changing anything):
- f1telemetry/packets.py: parsers for the F1 25 UDP format (packetFormat 2025). Packet layouts are
  verified against EA's spec. Do NOT change struct layouts or offsets unless I give you the spec.
- f1telemetry/state.py: RaceState. Builds live state, lap records, gaps, trends, fuel/wear rates,
  and snapshot() which returns a compact JSON-safe dict.
- f1telemetry/deterministic.py: route() answers lookup questions (gaps, fuel, tyres, weather,
  damage, position, laps, pit window) from telemetry and returns kind="ai" for open-ended ones.
  Callouts produces proactive radio messages. RadioQueue is a priority queue that holds while
  push-to-talk is down and drops stale messages.
- main.py: asyncio runtime. UDP listener on 127.0.0.1:20777, record/replay, optional Redis
  publisher, a stdin console for typing questions, and three marked hooks:
  HOOK 1 (push-to-talk -> STT), HOOK 2 (AI agent), HOOK 3 (TTS playback).
- test_synthetic.py: end-to-end test with hand-built packets. It must keep passing.

Hard constraints:
- Target: Windows 11, Python 3.11+, laptop with an RTX 2050 (4 GB VRAM). The game uses almost all
  of the GPU, so ALL local AI (STT, TTS, local LLM) must run on CPU by default. GPU use must be
  opt-in via config, never the default.
- Push-to-talk comes from the game itself: the wheel button is bound to "UDP Action 1" in-game and
  arrives as ("ptt", True/False) signals from RaceState. Keep that as the primary PTT source.
- Numbers spoken to the driver must come from telemetry (the snapshot), never be invented by an LLM.
- Keep the deterministic fast path: route() runs first; only kind="ai" goes to an LLM.
- No LangGraph/LangChain and no vector database. A single LLM call with injected context is enough.
- Never block the asyncio loop: run STT, TTS synthesis and blocking I/O via asyncio.to_thread or
  a dedicated thread.
- All settings go in config.yaml (loaded once at startup into a typed dataclass), with sensible
  defaults so the app runs with no config file present.
- Add a requirements.txt. Pin exact versions you have verified install on Windows with Python 3.11.
- Every new module gets unit tests that run without a microphone, speakers, game, network or GPU
  (use fakes/mocks). Put them in tests/ and make `python -m pytest` run everything, including
  test_synthetic.py.

Reply with a short plan for the whole project before writing code. Then wait for my next prompt.
```

---

## Prompt 1: Config, logging, latency metrics

```
Add the foundation:
1. config.py + config.yaml with sections: telemetry (host, port, ptt_action), audio (input/output
   device names or indices, sample rate), stt, tts, llm, knowledge, redis, logging.
   Unknown keys should produce a warning, not a crash.
2. Wire main.py to read config.yaml (CLI flags override config).
3. A small latency tracker: for each driver transmission record timestamps for ptt_up,
   stt_done, route_done, llm_first_token, llm_done, tts_first_audio, playback_done. Log one line
   per transmission with the stage durations in ms, and keep a rolling average.
4. A `--list-devices` flag that prints audio input/output devices and exits.
Tests for config loading/merging and the latency tracker.
```

---

## Prompt 2: Push-to-talk audio capture and speech-to-text

```
Implement HOOK 1 in a new module audio_in.py and stt.py.

Capture (audio_in.py):
- Open one sounddevice.InputStream at startup (16 kHz, mono, float32) and keep it open for the
  whole session; opening a device per transmission adds latency.
- Keep a ~300 ms ring buffer at all times so the first word isn't clipped (pre-roll).
- On ("ptt", True): start collecting (pre-roll + new audio). On ("ptt", False): stop and hand the
  numpy array to STT. Ignore presses shorter than 250 ms. Cap a transmission at 15 s.
- Keep audio in memory as a numpy array. No .wav files.
- Optional fallback PTT source via pygame joystick (config: ptt.source = "udp" | "joystick",
  joystick index + button). Set SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS=1 before pygame init so it
  works while the game has focus. Default stays "udp".

STT (stt.py):
- faster-whisper on CPU, compute_type="int8". Default model "small.en"; allow "base.en",
  "distil-small.en" via config. Load once at startup and run one warm-up transcription on silence.
- transcribe(audio) runs in a worker thread. Use language="en", beam_size=1,
  condition_on_previous_text=False, vad_filter=False (PTT already bounds the speech).
- Build initial_prompt dynamically to bias recognition toward radio vocabulary: a fixed list
  ("box, tyres, understeer, oversteer, DRS, ERS, front wing, brake bias, diff, lift and coast,
  push, copy, safety car, VSC, softs, mediums, hards, inters, wets") plus driver surnames from
  RaceState.names and the current track's corner names from the knowledge notes (Prompt 5).
- Drop transcripts that are empty or only filler ("you", "thank you", ".") – a known Whisper
  hallucination on near-silence.
- When transcribed, call Engineer.ask(text) on the event loop.

Tests: feed a synthetic numpy array to a mocked model; check PTT start/stop, pre-roll, min
duration and the hallucination filter.
```

---

## Prompt 3: LLM agent with Groq AND local/open-source models

```
Implement HOOK 2 in llm.py and agent.py. The LLM backend must be swappable from config.yaml.

Providers (llm.py), one interface:
    class LLMProvider(Protocol):
        async def stream(self, system: str, user: str, max_tokens: int, timeout_s: float) -> AsyncIterator[str]
        async def warmup(self) -> None

1. GroqProvider: OpenAI-compatible endpoint (base_url https://api.groq.com/openai/v1), API key
   from env var GROQ_API_KEY. Model from config, default "llama-3.1-8b-instant". Check Groq's
   current model list and make sure the default model still exists.
2. OllamaProvider (local, free, open source): use Ollama's NATIVE API (POST /api/chat with
   "stream": true), not the /v1 endpoint, because only the native API accepts "options".
   Send options {"num_gpu": 0 (CPU only, configurable), "num_ctx": 2048,
   "num_predict": max_tokens, "temperature": 0.3} and "keep_alive": "60m" so the model stays
   loaded. Default model "qwen2.5:3b-instruct"; also document "llama3.2:3b" and
   "qwen2.5:1.5b-instruct" as faster options for weak CPUs.
3. OpenAICompatibleProvider (local): any OpenAI-compatible server, e.g. llama.cpp's
   llama-server or LM Studio (default base_url http://localhost:8080/v1, no key).

Config:
    llm:
      primary: groq            # groq | ollama | openai_compatible
      fallback: ollama         # used if primary errors, times out, or no internet; or null
      timeout_s: 4
      max_tokens: 60
      groq: {model: llama-3.1-8b-instant}
      ollama: {base_url: http://localhost:11434, model: qwen2.5:3b-instruct, num_gpu: 0}
      openai_compatible: {base_url: http://localhost:8080/v1, model: local}

Behavior:
- Call warmup() for primary and fallback at startup (tiny prompt) so the first real request
  isn't a cold start. Log which providers are ready.
- If primary fails or doesn't produce a first token within timeout_s, retry once on fallback.
  If both fail, say "Radio check, say again." via the deterministic path.
- Use httpx.AsyncClient (one client per provider, reused) for all HTTP calls.

Agent (agent.py):
- build_prompt(question, snapshot, knowledge_text) -> (system, user). The user message contains
  STATE as compact JSON (state.snapshot(), with None fields removed), then TRACK NOTES if any,
  then DRIVER: <question>.
- Use the system prompt in prompts/engineer_system.txt (create it with the text below).
- Stream tokens; split into sentences on . ! ? and push each finished sentence to the radio
  queue as priority -1 immediately, so TTS starts before the whole reply is done.
- Clean output for speech: strip markdown, emojis, brackets; expand "P3" → "P3" is fine,
  "0.3s" → "0.3". Hard cap at 2 sentences.
- Guardrail: if the reply contains a number that doesn't appear in the snapshot JSON or the
  question, and isn't a small setup step (1–3 clicks/steps), log a warning with the reply.
  Don't block it, just log, so we can tune the prompt.
- Engineer.ask(): route() first; on kind="ai", run the agent as an asyncio task. If the driver
  presses PTT again, cancel the in-flight agent task.

Contents of prompts/engineer_system.txt:
<<<
You are the driver's race engineer on the pit wall during an F1 25 race, speaking over team radio.

Rules:
- Reply in at most 2 short sentences, under 30 words total. Radio style: direct, calm, no filler,
  no greetings, no "as an AI".
- Only use numbers that appear in STATE. If what you need isn't in STATE, say you don't have it.
- Give one clear action when the driver asks for help. Prefer things the driver can change NOW
  from the car: brake bias, differential (on-throttle), ERS mode, driving lines and inputs
  (earlier/later braking, lift points, smoother throttle). Front wing angle can only change at a
  pit stop. Other setup items are locked in a race.
- For setup advice, give direction and size in clicks or steps ("one click more front wing at the
  stop", "bias two forward"), never absolute setup values.
- Wheel order in STATE is rear left, rear right, front left, front right.
- If the driver reports a problem, acknowledge it in 1–3 words, then advise ("Copy. ...").
- Strategy questions: use pit_window, tyre wear and wear_pct_per_lap, fuel laps_delta, gaps and
  weather.next_rain. Commit to a recommendation; don't list options.
- Never use markdown, lists, emojis or symbols. Write numbers the way an engineer says them
  ("gap one point two" is fine as "gap 1.2").
>>>

Tests with a fake provider: streaming → sentence chunks, fallback on timeout, fallback on error,
cancellation on new PTT, speech cleaning, the number guardrail.

Also add docs/LOCAL_LLM.md: how to install Ollama on Windows, `ollama pull qwen2.5:3b-instruct`,
how to run it CPU-only, how to point the app at llama.cpp/LM Studio instead, and an honest note:
on a laptop CPU a 3B model takes roughly 1–3 s for a short reply, so Groq is the faster primary and
the local model is the offline/free fallback. Running the local model on the RTX 2050 while the
game runs will likely cause stutter.
```

---

## Prompt 4: Text-to-speech and radio effect

```
Implement HOOK 3 in tts.py and radio_fx.py.

TTS (tts.py), swappable from config:
- PiperTTS (default, CPU, local): piper-tts package with a British voice by default
  (en_GB-alan-medium). Download the voice files on first run into models/piper/. Check the
  current piper-tts Python API before writing code; it changed in recent versions.
- KokoroTTS (optional, CPU): kokoro-onnx.
- EdgeTTS (optional, cloud): edge-tts, off by default.
- All engines expose: async synth(text) -> (np.ndarray float32, sample_rate), run in a thread.
- Warm up the engine at startup with one short phrase.

Radio effect (radio_fx.py), all in numpy/scipy, configurable on/off:
- Band-pass 300–3400 Hz, light compression/soft clipping, low-level static noise.
- A generated "click" (short noise burst) before and a quieter "squelch" after each message.

Playback:
- One persistent sounddevice OutputStream on the configured output device (the headset).
- Engineer.speaker(): pop from RadioQueue, synth, apply FX, play, and await completion before the
  next message so messages never overlap.
- If the driver presses PTT while the engineer is talking, stop playback immediately (like a real
  radio) and leave remaining queued messages to RadioQueue's TTL logic.
- While synthesizing sentence 1 of an AI reply, sentence 2 may arrive; queue it, don't drop it.

Tests: FX output shape/dtype and no clipping above 1.0; playback interruption logic with a fake
output stream.
```

---

## Prompt 5: Track knowledge (replaces the vector DB)

```
Add knowledge.py and a knowledge/ folder.

- knowledge/tracks/<track>.md, one file per track, named to match TRACKS in state.py
  (e.g. silverstone.md, spa.md). Sections: "## Corners" (a numbered list with names, e.g.
  "Copse (T9): fast right, ..."), "## Overtaking", "## Tyres", "## Setup notes".
- knowledge/general.md for car-wide notes (e.g. what brake bias and diff changes do).
- Create silverstone.md and general.md as examples with clearly-marked placeholder content I'll
  replace; don't invent facts presented as verified.
- On session start, load the current track's file (track from state.session.track_id).
- For each AI question, pick sections by simple keyword overlap with the question (corner names,
  "tyre", "setup", "overtake"), always including the matching corner line if a corner is named.
  Cap injected knowledge at ~400 tokens (estimate 4 chars per token).
- Expose corner names to stt.py for the initial_prompt (Prompt 2).
Tests: section selection and the token cap.
```

---

## Prompt 6: Run scripts, README, real-game verification

```
1. run.bat that activates the venv and starts main.py; setup.bat that creates the venv and
   installs requirements.txt.
2. README.md: install steps, in-game settings (UDP Telemetry On, IP 127.0.0.1, port 20777,
   UDP Format 2025, send rate 20 Hz, bind wheel button to "UDP Action 1"), config reference,
   how to choose Groq vs Ollama vs llama.cpp, how to record and replay a session, troubleshooting
   (no packets → firewall/port; wrong device → --list-devices; stutter → make sure everything AI is
   on CPU).
3. A `--selftest` flag: replays a built-in synthetic sequence (reuse test_synthetic's builders),
   runs one deterministic question and one AI question through the configured LLM, speaks both
   through TTS, and prints the latency report.
4. Run the full test suite and fix anything failing. Then give me a checklist for the first real
   session: record a race with --record, replay it with --replay, and check the log for any
   "packet ... expected car stride" warnings (these mean a game patch or the 2026 season update
   changed a packet layout).
```
