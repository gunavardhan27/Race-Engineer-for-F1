# Prompt 4: Text-to-speech and radio effect

Before starting: read antigravity_pack/FEATURE_STATUS.md (from prompt 00a) and only build what is missing, partial or broken for this prompt. Extend existing code; don't rebuild what works. Packet layouts: docs/udp_spec_2026.h.

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
