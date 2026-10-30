# Race Engineer: Antigravity pack

Everything Antigravity needs to bring your existing race_engineer project up to date: prompts,
the official F1 26 telemetry spec, a verified reference parser, and your spreadsheets converted
into importable data.

## Setup (once)

1. Unzip this pack **inside your project folder**, so the path is
   `race_engineer/antigravity_pack/`. The prompts refer to it by that name.
2. Open the project in Antigravity.

## Order

| Step | File | What it does |
|---|---|---|
| 1 | `prompts/00_project_rules.md` | Rules for the agent. Paste first; keep it in the agent's rules if Antigravity supports that. |
| 2 | `prompts/00a_audit_and_status.md` | The agent audits your actual code against every feature and every known bug, and writes `FEATURE_STATUS.md` with a build plan. No code changes yet. |
| 3 | Fix the data first | Run **20** (2026 format: 24 cars, new layouts) and the fixes in **19** that the audit marks broken. Wrong data breaks everything after it. |
| 4 | **25** | Imports your setups, track notes and the track/team id mapping. |
| 5 | The rest, as the audit plan says | Typically 21 → 22 → 23 → 24, then anything else marked missing. |

Each prompt file starts with a line telling the agent to check `FEATURE_STATUS.md` and only build
what's missing, so pasting a prompt for something half-built won't rebuild it from scratch.

After every prompt: run the tests, then check in the game or with
`python main.py --replay <recording> --explain`.

## What supersedes what

- Packet layouts: `reference/docs/udp_spec_2026.h` overrides every "F1 25 spec" mention in older prompts.
- Prompt 8's motion layout → replaced by 20; its track outline → replaced by 21's track model.
- Prompt 12's overtake triggers and prompt 19 steps 3–5 → replaced by 21.
- Persona lines: `data/lines/cocky.yaml` is the newest version (merged by 25).

## Contents

- `prompts/`: 00, 00a, 01–25 (one file per prompt, ready to paste).
- `reference/docs/udp_spec_2026.h`: EA's official 2026 Season Pack UDP spec (packet format 2026).
- `reference/f1telemetry/`: parser and state code, checked field by field against the spec for all
  24 cars, plus the session-reset fix. Antigravity merges it into your project.
- `reference/inspect_telemetry.py`: shows what your game is sending (packet types, rates, sizes vs
  the spec, track id, car class, your team id). Run it with the app closed:
  `python inspect_telemetry.py` (live) or `--file recording.bin`.
- `data/`: your spreadsheets converted: `setups_seed.json` (275 setups), `track_turns.json`
  (181 key corners), `knowledge/tracks/*.md` (25 track notes), `tracks.json` (name → game track id),
  `lines/cocky.yaml`. Read `data/DATA_REVIEW.md` for the issues found.

## Before you start

- Run `reference/inspect_telemetry.py` on each track you race once, to confirm the track ids in
  `data/tracks.json` (marked "verify") and to find Madrid's id and your team id.
- The imported setups are untested. The app will rank them by your own lap times as you drive them.
