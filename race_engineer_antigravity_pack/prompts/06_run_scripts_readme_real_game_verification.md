# Prompt 6: Run scripts, README, real-game verification

Before starting: read antigravity_pack/FEATURE_STATUS.md (from prompt 00a) and only build what is missing, partial or broken for this prompt. Extend existing code; don't rebuild what works. Packet layouts: docs/udp_spec_2026.h.

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
