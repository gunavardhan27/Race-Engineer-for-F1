# Prompt 10: Setup library

Before starting: read antigravity_pack/FEATURE_STATUS.md (from prompt 00a) and only build what is missing, partial or broken for this prompt. Extend existing code; don't rebuild what works. Packet layouts: docs/udp_spec_2026.h.

> **Note:** CarSetups layout: use docs/udp_spec_2026.h (50 bytes per car, 24 cars, trailing m_nextFrontWingValue). Team ids are uint16 in 2026. Seed setups are imported by Prompt 25.

```
Add a setup library that captures, stores, ranks and shows car setups per car class, team, track
and conditions. IMPORTANT: never invent setup values, and do not scrape setup websites. Setups only
come from (a) the game's own CarSetups packet, or (b) setups I import myself.

Parser:
- Add PacketId.CAR_SETUPS = 5 and parse_car_setups. Per-car CarSetupData in F1 25 is 50 bytes:
  uint8 frontWing, rearWing, onThrottle, offThrottle; float frontCamber, rearCamber, frontToe,
  rearToe; uint8 frontSuspension, rearSuspension, frontAntiRollBar, rearAntiRollBar,
  frontSuspensionHeight, rearSuspensionHeight, brakePressure, brakeBias, engineBraking;
  float rearLeftTyrePressure, rearRightTyrePressure, frontLeftTyrePressure,
  frontRightTyrePressure; uint8 ballast; float fuelLoad. After the 22 cars there is a trailing
  float nextFrontWingValue. Verify against EA's F1 25 spec first and use the _stride() check.
  Only the player's setup is reliable; in multiplayer other cars' setups are blank.

Schema (setups.py + SQLite tables):
- setups: id, fingerprint (hash of the values, so identical setups are stored once), car_class
  (from m_formula), team_id, track_id, conditions (dry/wet), session_type (quali/race),
  game_format, source ("captured" | "imported"), source_note (e.g. where I got it), name,
  created_at, and a JSON "params" dict. Use a dict, not fixed columns, because the 2026 season
  update may add or change setup parameters. Store a schema_version.
- setup_param_ranges: valid min/max/step per parameter and car class. Fill these ONLY from values
  observed in captured packets plus ranges I confirm; ship the file with ranges empty and a
  command that prints observed min/max so I can fill it in.

Capture:
- When a session starts and when the setup changes (fingerprint changes), save the player's
  setup automatically with the session's car class, team, track, weather and session type.
- Link every completed lap (Prompt 9) to the setup in use.

Import:
- An import form on the dashboard and a CLI command accepting: a JSON file in the library format,
  a CSV template (generate one), or pasted free text (e.g. copied from a video description).
- For free text, use the configured LLM provider (Prompt 3) to convert it into the params dict,
  with a strict JSON-only system prompt. Then validate every value against setup_param_ranges,
  show me the parsed result with anything out of range or missing highlighted, and save only
  after I confirm. Never fill missing values with guesses; leave them empty.

Ranking:
- For each track + car class + conditions + session type, rank setups by my best valid lap and
  my median valid lap with that setup (minimum 3 laps for the median), showing lap counts so a
  single lucky lap doesn't dominate. Imported setups with no laps yet are listed as "untested".

Dashboard "Setups" page:
- Auto-filters to the current session's car class, team, track, conditions and session type, with
  manual filters to browse others.
- Shows ranked setups, a side-by-side diff of any two setups (highlight changed values), and the
  setup currently on the car.
- Export a setup as JSON or as a plain-text sheet I can follow while typing values into the game.

Engineer integration:
- Add the current setup params and the best-known setup for this track/conditions (if different)
  to the AI agent's context, so setup advice is relative to what's actually on the car
  ("one more click of front wing than you're running").
- Add a deterministic intent "setup" for "what setup am I on" / "best setup here" that answers
  from the library without the LLM.

Tests: CarSetups parsing with synthetic packets, fingerprint dedupe, change detection, ranking with
the minimum-lap rule, import validation with out-of-range and missing values, LLM import with a
fake provider.
```
