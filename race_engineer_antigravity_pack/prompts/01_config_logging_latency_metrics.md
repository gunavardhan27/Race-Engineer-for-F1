# Prompt 1: Config, logging, latency metrics

Before starting: read antigravity_pack/FEATURE_STATUS.md (from prompt 00a) and only build what is missing, partial or broken for this prompt. Extend existing code; don't rebuild what works. Packet layouts: docs/udp_spec_2026.h.

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
