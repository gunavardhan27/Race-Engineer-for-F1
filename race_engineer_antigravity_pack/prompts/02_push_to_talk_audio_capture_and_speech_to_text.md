# Prompt 2: Push-to-talk audio capture and speech-to-text

Before starting: read antigravity_pack/FEATURE_STATUS.md (from prompt 00a) and only build what is missing, partial or broken for this prompt. Extend existing code; don't rebuild what works. Packet layouts: docs/udp_spec_2026.h.

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
