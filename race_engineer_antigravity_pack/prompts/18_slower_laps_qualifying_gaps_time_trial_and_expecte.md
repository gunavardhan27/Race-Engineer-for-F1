# Prompt 18: Slower laps, qualifying gaps, Time Trial, and expected vs actual performance

Before starting: read antigravity_pack/FEATURE_STATUS.md (from prompt 00a) and only build what is missing, partial or broken for this prompt. Extend existing code; don't rebuild what works. Packet layouts: docs/udp_spec_2026.h.

> **Note:** TimeTrial layout: docs/udp_spec_2026.h (team id is uint16; 104-byte packet).

```
Add pace analysis so the engineer reacts to slower laps, qualifying gaps and performance versus what
the car should achieve. Everything is deterministic and based on recorded data; the persona lines
for these events are already in lines/cocky.yaml (Part "Pace and expectation"). Never judge
the driver on missing or unfair data.

1. Lap-to-lap regression (race, practice, time trial):
   - After each completed valid lap, compare to the previous valid lap.
   - Before calling it the driver's fault, check for reasons using the lap flags from Prompt 14:
     traffic (car within 1.5 s ahead for >30% of the lap), safety car or VSC, in/out lap,
     new damage, rain or weather change, a mistake event (spin, collision, off), tyre wear above
     the stint's normal degradation slope. If any apply, use slower_lap_explained with {reason}
     ("dirty air behind <name>", "safety car", "damage", "tyre deg", "rain", "the moment at <corner>").
   - Only use slower_lap (mocking) when slower by more than config pace.slower_lap_threshold_s
     (default 0.5) with no reason found. Include the worst corner from analysis.py if available.
   - Expected lap-to-lap change from fuel burn: adjust by config pace.fuel_effect_s_per_kg
     (default 0.03) before comparing, so a lighter car's expected gain is accounted for.

2. Qualifying:
   - After each flying lap: position on the timesheet, gap to pole (session best of all cars),
     gap to teammate, and sector comparison vs the session's best sectors (purple sector calls
     use LapData sector times and SessionHistory best sectors).
   - Elimination zone: compute from the number of running cars and session type (Q1, Q2, Q3,
     short quali, one-shot). Don't hardcode the cut-off: config quali.eliminated_per_segment
     with a default derived from the car count (verify against the current F1 format for 22 cars).
     Use quali_danger_zone / quali_safe near the end of a segment and after each lap.
   - quali_gap_large when the gap to pole exceeds config quali.large_gap_s (default 1.0).

3. Time Trial:
   - Parse the TimeTrial packet (id 14): player session best, personal best and rival data sets
     (lap and sector times, assists, equal car performance flag, valid flag). Verify the layout
     against EA's F1 25 spec, and use the _stride-style size check.
   - Speak gap to PB and to rival, which sector the time went in (tt_slower / tt_personal_best).
   - If equal car performance is OFF, note that car comparisons with the rival aren't like for like.

4. Expected performance (the "car is fast, you're slow" judgment):
   - Primary benchmark: the teammate (same team id, same car). Expected position = where I would
     be with my teammate's best lap (quali) or median race pace (race). Only judge when the teammate
     has a valid comparable lap (same session, same compound in quali, no flags).
   - Fallback benchmark: historical team pace from my own database (Prompt 14) at this track and
     car class, as gap to pole or median pace rank across past sessions at the same AI difficulty.
     Require at least 3 past sessions; otherwise make no judgment.
   - Also store and use speed trap data (LapData speed trap, all cars) as a straight-line speed hint
     for the AI agent ("car's quick on the straights, you're losing it in the corners").
   - Never use real-world team rankings or hardcoded car performance values.
   - Events: below_expectation when actual position is >= config pace.expectation_margin (default 3)
     places worse than expected; above_expectation when >= 3 places better; beat_teammate /
     lost_to_teammate after quali and at race end with the gap.
   - AI difficulty hint: if I'm consistently > 1.0 s per lap faster than the AI teammate over 2+
     sessions at the same difficulty, use ai_too_easy; if consistently > 1.0 s slower, ai_too_hard.
     Read AI difficulty from the Session packet. Once per session at most.

5. Add these to the snapshot for the AI agent: gap_to_pole, quali_position, elimination_cutoff,
   teammate {name, best_lap, gap}, expected_position with its basis ("teammate" or "history")
   and confidence, last_lap_vs_previous with reasons.

6. Deterministic intents: "gap to pole", "how far off pole", "am I through", "where's my
   teammate", "how am I doing vs teammate", "what's the car capable of". Answer from data; say
   "Not enough data yet" when the benchmark requirements aren't met.

7. Rate limits: at most one pace remark per lap; big moments (quali result, race result) always
   speak; follow the persona cooldowns from Prompt 12 and quiet mode from Prompt 15.

Tests: regression with and without each excuse type, fuel adjustment, quali cut-off for 20 and 22
cars, purple sector detection, TimeTrial packet parsing, expected position from teammate,
history fallback with < 3 and >= 3 sessions, no judgment without a valid teammate lap,
AI difficulty hint, one-remark-per-lap limit.
```
