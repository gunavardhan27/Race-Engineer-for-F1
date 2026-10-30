# Prompt 00: Project rules (paste first, keep in the agent's rules/memory)

```
You are working on "race_engineer", a voice race engineer + dashboard for the F1 25 game with the
2026 Season Pack ("F1 26"), UDP packet format 2026. The project already exists and has been built
over many iterations; much of it works. A folder antigravity_pack/ has been added with prompts,
reference code, the official telemetry spec and seed data.

Source of truth and references:
- docs/udp_spec_2026.h (copy from antigravity_pack/reference/docs/ if missing): EA's official
  "F1 25: 2026 Season Pack" UDP spec. Every packet layout must follow it. Key facts: 24 cars in
  every per-car array; 17 packet types (0–16) including CarTelemetry2 (active aero, Overtake Mode).
  Its appendices (team ids, penalty types, surface types, track ids) are NOT in that file: never
  hardcode guesses for those; learn them from data or ask me once in the UI, and store the answer.
- antigravity_pack/reference/f1telemetry/: a reference parser cross-checked field by field against
  the spec (packets.py, state.py, deterministic.py). Use it to fix or verify the project's parser;
  merge, don't blindly overwrite files that have been extended.
- antigravity_pack/data/: seed data (setups, track notes, track id mapping, persona lines) with a
  review in DATA_REVIEW.md. It is imported, untested data: label it that way in the UI.

How to work:
- Before implementing any prompt, read antigravity_pack/FEATURE_STATUS.md (created by prompt 00a)
  and only build what is missing, partial or broken. Extend existing code; don't rebuild what works,
  don't rename working modules, don't change behaviour I haven't asked to change.
- After each prompt: run the full test suite, fix failures, update FEATURE_STATUS.md, and give me
  a short summary of what changed and how to test it in the game.
- When something in a prompt conflicts with how the project is actually built, follow the project's
  structure and tell me what you adapted.

Hard constraints:
- Platforms: Windows 11 runs the game and the app (Python 3.11+, RTX 2050 laptop, 4 GB VRAM).
  macOS is used for development and to view the dashboard over the network. Code must run on both.
- All local AI (STT, TTS, local LLM) runs on CPU by default; GPU only if enabled in config.
- Numbers spoken to the driver come from telemetry or stored data, never invented by an LLM.
- Deterministic first: fixed rules and data answer anything they can; the LLM only handles
  open-ended questions and the post-race message. Race events never depend on the LLM.
- No fabricated data anywhere: no invented setup values, corner names, team ids or track ids.
  Missing stays empty and is shown as missing.
- Never block the asyncio loop. Background threads are daemon threads with timeouts.
- Settings in config.yaml with defaults; secrets (API keys) in the OS keychain via keyring.
- Every new module has tests that run without the game, mic, speakers, network or GPU.
- Free tiers only (usage guard from prompt 13); no paid APIs.

Reply with a one-paragraph confirmation, then wait for prompt 00a.
```
