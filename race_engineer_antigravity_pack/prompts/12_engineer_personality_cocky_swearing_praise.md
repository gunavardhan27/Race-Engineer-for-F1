# Prompt 12: Engineer personality (cocky, swearing, praise)

Before starting: read antigravity_pack/FEATURE_STATUS.md (from prompt 00a) and only build what is missing, partial or broken for this prompt. Extend existing code; don't rebuild what works. Packet layouts: docs/udp_spec_2026.h.

> **Note:** Overtake triggers here are superseded by Prompt 21 (overtake detection v2). Collision severity comes from Prompt 20. Lines file: antigravity_pack/data/lines/cocky.yaml is the newest version.

```
Add a personality system: the engineer reacts to my good and bad moments with lines from
lines/<persona>.yaml (already written; read it first), and the AI agent speaks in the same persona.

Config (config.yaml):
    persona:
      name: cocky                # file in lines/ and prompts/personas/; "professional" = no reactions
      profanity: mild            # none | mild | strong — max line level used
      reaction_frequency: normal # low | normal | high (scales cooldowns)
      min_gap_s: 20              # minimum time between personality reactions

Triggers (personality.py), all deterministic. Verify every event payload layout against EA's F1 25 spec:
- overtake_made / overtaken: Event "OVTK" (uint8 overtakingVehicleIdx, beingOvertakenVehicleIdx)
  where one of them is the player. Ignore if either car is in the pit lane (pit_status != 0) or
  if the other car is a lap down or up (compare lap numbers): lapping traffic isn't an overtake.
- defended: car behind within 1.0 s for >= 3 consecutive lap records and still behind.
- good_start / bad_start: position at end of lap 1 vs grid position (LapData m_gridPosition, f[24]);
  only if the change is >= 2 places.
- fastest_lap: Event "FTLP" for the player. personal_best: from the lap debrief logic.
- win / podium / points / no_points: Event "CHQF", then the FinalClassification packet (id 8) for
  the player's final position. Also handle Event "RCWN" (race winner).
- spin: heuristic from Motion data. Angle between forward direction and velocity > 60° at
  speed > 50 kph, followed by speed < 30 kph within 3 s. Make thresholds configurable and test
  against a recorded session.
- collision: Event "COLL" (vehicle1Idx, vehicle2Idx) involving the player.
- flashback: Event "FLBK".
- track_limits / penalty: from the signals in the track-limits feature.
- lost_positions: lost >= 2 places in one lap.
- escalation: 3+ blunders (spin, collision, flashback, penalty, lost_positions) within the last
  5 laps: use an "escalation" line instead of the normal one, with {count}.
- clean_stint: 10 consecutive valid laps with no blunder.
- retirement: Event "RTMT" for the player.

Line selection:
- Filter by profanity level (none = untagged only; mild = untagged + mild; strong = all).
- Shuffle-bag per event so lines don't repeat until all have been used.
- Fill placeholders from state; if a placeholder can't be filled, pick another line.
- Cooldowns per event type plus the global min_gap_s. Big moments (win, podium, retirement)
  ignore cooldowns. Never queue a reaction older than 5 s.
- Pre-render every line variant with no placeholders to audio at startup (cache on disk keyed by
  text + voice) so reactions play instantly; lines with placeholders use normal TTS.

Rules that override the persona:
- Spotter, safety car, VSC, damage and penalty-details calls stay neutral and short. A reaction
  may follow them but never replaces them.
- No reactions while the spotter has an active overlap or PTT is held.
- Reactions are about my driving only.

AI agent:
- prompts/personas/cocky.txt: a short persona description (confident, sarcastic, British pit-wall
  banter, praises good driving loudly, roasts mistakes) appended to the engineer system prompt,
  plus a profanity instruction built from config ("Do not swear." / "Mild swearing allowed." /
  "Swearing allowed, keep it natural, not every sentence."). All numbers still come from STATE.
- If the model output is empty, refuses or apologizes, fall back to the deterministic answer or a
  line from the pool.
- Post-race: after the final classification, the agent writes a 2–3 sentence radio message in
  persona from a race summary (grid, finish, positions gained, fastest lap, penalties, blunder
  count, best lap). Fall back to the win/podium/points/no_points line if the LLM fails.

Dashboard: persona and profanity selectors on a settings page (applies live), and reactions show
in the radio log.

Tests: profanity filtering, shuffle-bag no-repeat, placeholder fallback, cooldowns and big-moment
override, OVTK lapping filter, escalation counting, spin heuristic on synthetic motion data,
refusal fallback with a fake LLM.
```
