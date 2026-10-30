# Prompt 5: Track knowledge (replaces the vector DB)

Before starting: read antigravity_pack/FEATURE_STATUS.md (from prompt 00a) and only build what is missing, partial or broken for this prompt. Extend existing code; don't rebuild what works. Packet layouts: docs/udp_spec_2026.h.

> **Note:** Track notes for all 25 tracks are provided in antigravity_pack/data/knowledge/tracks/ (imported by Prompt 25). Don't create placeholder content for those tracks.

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
