# Prompt 21: Track model from telemetry + overtake detection v2

Before starting: read antigravity_pack/FEATURE_STATUS.md (from prompt 00a) and only build what is missing, partial or broken for this prompt. Extend existing code; don't rebuild what works. Packet layouts: docs/udp_spec_2026.h.

```
Telemetry has no track geometry (no centreline, edges, corner list or names). It does have track id,
track length, sector 2/3 start distances, active aero zones (≈ straights), DRS zones and marshal zones
(fractions of a lap), plus every car's world position (Motion, 24 cars) and every car's inputs
(CarTelemetry: speed, throttle, brake, steer, surface type per wheel). Build a track model from the
field's data and use it for a better overtake system. This supersedes Prompt 19 steps 3–5 (overtake
detection, classification, defend warnings) wherever they differ; keep Prompt 19's other steps.
Layouts: docs/udp_spec_2026.h. Use packets.num_cars(header) (24 in 2026) everywhere.

PART A — Track model (trackmodel.py), built automatically per track_id:
1. Sampling: for every running car not in the pit lane (pit_status 0, result_status active),
   record (lap_distance, x, y, z, speed, brake, throttle, steer) from LapData + Motion + CarTelemetry,
   binned every 2 m of lap distance. Skip the first 200 m of a race (grid) and laps under SC/VSC.
2. Centreline: per bin, the median x/y/z across all samples. Smooth (moving average over ~10 m).
   Heading per bin from the smoothed path; curvature = heading change per metre.
3. Usable width: per bin, the lateral offset of each sample from the centreline (dot product with the
   path's right vector); store the 2nd and 98th percentiles as left/right "used edges". Label it
   "approximate used width", not true track edges.
4. Corners: contiguous bins where |curvature| exceeds a threshold (config), merging gaps < 30 m.
   For each: start, apex (max |curvature|, cross-checked with the field's min-speed point),
   end, direction (left/right from curvature sign), field median min speed, and a corner number
   in lap order. Active aero zones (Session packet) mark straights and must not contain corners.
5. Braking zones: from ALL cars' brake input: per corner, the distribution of brake-start lap
   distances (brake > 0.2 after a period < 0.05) → median and p10/p90, plus median entry speed.
   Available from lap 1; refine as more laps arrive. My own reference values from Prompt 9 are
   stored separately for comparison.
6. Confidence: ready when every 2 m bin has samples from >= 3 laps (any cars). Until ready, the
   overtake classifier falls back to "clean unless contact".
7. Persist per track_id + layout (track length) in SQLite with a version; keep improving across
   sessions; rebuild button on the dashboard Tracks page.
8. Corner names: the Tracks page shows the model on a map with numbered corners; I click a corner and
   type its name. Names are stored in the knowledge notes keyed by corner number + apex distance,
   so they survive model rebuilds (match to the nearest apex within 50 m).
9. Draw the dashboard track map from this model (centreline + used edges), replacing the
   player-only outline from Prompt 8.

PART B — Overtake detection v2 (overtakes.py):
1. Order tracking: total_distance (LapData) for all cars; for each car within 60 m of me, track the
   sign of (mine - theirs). Candidate on a sign change; confirmed after 2.0 s or 80 m travelled
   with the sign held and the cars still within 60 m. Emit both made and lost passes.
2. Filters (no praise/roast): the other car pitting or just out of the pits; not active; lapped or
   lapping (difference near a full track length); SC/VSC/formation; first 200 m of the race; the
   other car crawling (speed < 60% of the field median at that bin for 3 s) → passed_stricken_car;
   the other car driving the wrong way (CarTelemetry2) → ignore.
3. Context at the pass (from the track model):
   - location: straight / braking zone / mid-corner / exit (from corner start/apex/end bins)
   - side: inside or outside relative to the corner direction (my lateral offset vs theirs)
   - overtake_mode: my CarTelemetry2 overtake_active during the 5 s before the pass
   - slipstream: I was within 1.0 s behind for >= 3 s on the preceding straight
4. Features (logged to an overtakes table for every pass, made and lost):
   - brake_vs_field_m: my brake start minus the field's median brake start for that corner
     (positive = later); brake_vs_mine_m: vs my own reference (Prompt 9) if available
   - start_gap_m: total_distance difference to that car at my brake start (negative = behind)
   - overlap_at_turn_in: total_distance difference within ±3 m at the corner's turn-in bin, with
     lateral separation > 1.2 m (both from the track model)
   - entry_speed_vs_field_kph and apex_speed_vs_field_kph for me
   - ran_deep: my lateral offset at apex beyond the used edge on the outside, or my exit speed
     well below the field median, or my wheels off tarmac/kerb (surface type) after the apex
   - victim_reaction: the other car's brake spike or throttle lift (> 0.5 drop) or steering swerve
     (|Δsteer| > 0.3) within 1.5 s while I was alongside, not explained by the corner itself
     (compare with that car's own inputs at the same bin on its previous lap if available)
   - forced_off: the other car's wheels on grass/gravel/sand (surface type, verify the surface
     type values in EA's appendix; treat tarmac, rumble strips and concrete as on-track), or its
     corner-cutting warning/lap-invalid change, within 3 s
   - contact and severity: COLL event with that car within ±2 s (severity 0/1/2)
   - switchback: that car re-passes within 5 s
5. Classes (thresholds in config, tuned with --replay --explain on my recordings):
   - overtake_mode_pass: straight-line pass with Overtake Mode active, no contact → its own
     praise line ("Overtake mode did its job")
   - clean: on a straight, or in a corner with overlap established, brake_vs_field < 10 m, no
     victim_reaction, no forced_off, contact none or severity 0
   - brave: brake_vs_field 10–25 m or start_gap between -10 and -5 m, no forced_off, contact none
     or severity 0, didn't run deep
   - divebomb: start_gap < -10 m or brake_vs_field > 25 m, combined with ran_deep or
     victim_reaction or forced_off
   - divebomb_contact: divebomb + contact severity >= 1
   - around_the_outside: clean or brave pass on the outside of a corner → extra praise line
   - switchback overrides praise; told_you if a penalty follows within 10 s (Prompt 19)
   - Lost positions get the same analysis from the other car's point of view, so the engineer
     can say "he divebombed you, stewards should look at that" or "fair move, he was alongside".
6. Defend warnings: use the track model's braking zones (from Part A, available from lap 1)
   instead of my reference lap; inside_look uses the attacker's lateral offset vs the corner direction.

PART C — Persona lines: add to lines/cocky.yaml (clean, mild and strong variants, same style as the
existing ones): overtake_mode_pass, around_the_outside, divebombed_by (someone lunged at me),
fair_pass_against (lost a place fairly), forced_off_by (someone pushed me off).

Tests: track model from synthetic multi-car laps on a known shape (circle + straight): centreline
error < 1 m, corner count and direction correct, braking zone medians; overtake confirmation and
flicker rejection with 24 cars; each class from synthetic feature sets; lost-pass classification;
victim_reaction excludes normal corner braking; corner-name matching after a rebuild.
```
