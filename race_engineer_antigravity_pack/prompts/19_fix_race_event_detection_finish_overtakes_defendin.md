# Prompt 19: Fix race-event detection (finish, overtakes, defending) and dashboard data loss

Before starting: read antigravity_pack/FEATURE_STATUS.md (from prompt 00a) and only build what is missing, partial or broken for this prompt. Extend existing code; don't rebuild what works. Packet layouts: docs/udp_spec_2026.h.

> **Note:** Steps 3, 4 and 5 (overtake detection, classification, defend warnings) are SUPERSEDED by Prompt 21. Steps 0, 1, 2, 6, 7 and 8 still apply.

```
Several detectors don't work. Working: track limits, flashbacks, penalties, collisions (these come
straight from game events). Broken: win/podium message, overtakes, car-behind warnings, and
dashboard data clearing. None of these use the LLM: they are deterministic telemetry logic.
New persona lines are already in lines/cocky.yaml ("Finish and overtakes" section).

0. Diagnose first:
   - Add a "detectors" debug log channel (config logging.detectors: true) that logs every
     candidate and decision with its numbers (e.g. "overtake candidate car 7, confirmed=False,
     reason=pit_lane").
   - Add `python main.py --replay <file> --explain`: replays a recording and prints a per-lap
     list of detected events (finish, overtakes with class and features, defend warnings,
     session resets) without audio. Run it on my recorded races and report what currently fires.

1. Session reset bug (in RaceState.update):
   - Ignore packets with session_uid == 0 entirely (menus/results screens send 0); they must
     never reset state.
   - Only reset on a new non-zero session_uid. Log every reset with old and new uid.

2. Finish detection, one message only:
   - Detect the finish when the player's LapData result_status changes from 2 (active) to 3
     (finished). At that instant record: position, my gap to the car ahead
     (me.gap_to_car_ahead_ms) and the car behind's delta to me (its gap_to_car_ahead_ms) as the
     margin. Emit ("finished", info).
   - Pick one line: P1 → win_photo_finish (< 0.5 s), win_close (0.5–3 s), win_comfortable
     (3–10 s), win_dominant (> 10 s); P2/P3 → podium_held (car behind < 1 s),
     podium_missed_higher (car ahead < 1 s, {higher} = pos - 1), else the normal podium line;
     P4 within 2 s of P3 → missed_podium; else points / no_points. Margin thresholds in config.
   - If I have unserved/unapplied time penalties (LapData penalties_s > 0) say the result is
     provisional ("pending the five-second penalty").
   - Once per session (key: session_uid). Suppress all other persona lines for 8 s after the finish.
   - When FinalClassification (id 8) arrives, speak a correction only if my classified position
     differs from the finish-line position. CHQF and RCWN events must not trigger a second message.

3. Overtake detection (replace the OVTK-only approach):
   - Primary signal: LapData total_distance for every car (add m_totalDistance = f[11] to LapData
     if not exposed). For cars within 50 m of me, track the sign of (my_total - their_total).
     A sign change is a candidate pass/loss. Confirm only if it holds for 2.0 s (config) and the
     gap is still < 50 m (so side-by-side flicker doesn't count).
   - Ignore: other car in pit lane (pit_status != 0) or just exited; result_status not active;
     other car's speed < 60% of mine for the previous 3 s → passed_stricken_car instead of praise;
     lapped/lapping (|total distance difference| close to a full track length rather than near 0);
     safety car/VSC periods; the first 200 m after the race start (grid chaos handled by
     good_start/bad_start).
   - Use the OVTK event as confirmation/tie-break only, never as the only source.
   - Lost positions get the same logic in reverse (overtaken).

4. Overtake classification (clean / brave / divebomb):
   - Braking zones: from my reference lap trace (Prompt 9) find braking zones (brake > 0.2) per
     track: start distance, apex (min speed) distance, reference braking start, reference min
     speed. Corner direction (left/right) from the track outline heading change. Corner names
     from knowledge notes if present, else "turn N". Store per track; recompute when a faster
     clean reference lap appears. Until a reference lap exists, classify only as "clean" unless
     there was contact.
   - Features at a pass that happens inside a braking zone (from 150 m before braking start
     to 100 m after the apex):
       braking_delta_m: my braking start vs reference (positive = later)
       start_gap_m: the other car's d_forward at my braking start (spotter math, Motion packet)
       overlap_at_turn_in: |d_forward| < 3 m and |d_right| > 1 m at the turn-in point
       ran_deep: my min speed > ref + 15 kph and exit speed (100 m after apex) < ref - 10 kph
       contact: COLL event with that car within ±2 s
       forced_off: that car's corner-cutting warnings or lap-invalid flag changed within 3 s, or its
                   speed at exit dropped > 25 kph below its own speed 1 s before the apex
       switchback: that car re-passes me within 5 s
   - Classes (thresholds in config):
       clean: pass outside a braking zone, or overlap established, braking_delta < 10 m,
              no contact, not forced_off
       brave: braking_delta 10–25 m or start_gap between -8 and -4 m, no contact, not forced_off
       divebomb: start_gap < -8 m or braking_delta > 25 m, combined with ran_deep or forced_off;
                 with contact → divebomb_contact
       switchback overrides the praise: overtake_switchback
   - If a PENA event for me arrives within 10 s after a divebomb → told_you (merge with the
     neutral penalty call).
   - Log all features for every overtake to the database (overtakes table) for later tuning and
     ML; show them on the dashboard Laps page.
   - Don't rely on DRS for anything (the 2026 rules replace DRS).

5. Car behind near corners (defend warnings) — new feature, separate from the spotter:
   - Using braking zones from step 4: when I'm 300–150 m before a braking zone and a car is
     behind within 15 m (Motion d_forward) or < 0.4 s, and closing (gap shrank over the last
     second), say defend_warning with the corner name.
   - If that car's d_right moves toward the inside of the upcoming corner (corner direction) by
     more than 1 m while within 10 m → inside_look instead.
   - Max one defend call per corner per lap and per 20 s overall; never while the spotter has
     an active overlap (spotter wins); these are short neutral calls (priority 1, TTL 1.5 s),
     pre-rendered for instant playback (pre-render generic variants without names).

6. Dashboard data persistence:
   - On every WebSocket (re)connect, the server immediately sends the full current snapshot plus
     history: laps so far this session, the radio log, timing tower, overtakes. The browser never
     builds history on its own; charts load from REST endpoints backed by the database.
   - Store the live snapshot at 1 Hz into a session_timeline table (compressed JSON per 10 s
     chunk) so the dashboard and future ML have a full time series even after an app restart.
     Reload the current session from the database on app restart if the same session_uid
     resumes.

7. Penalty decoding bug ("250/255-second penalty" after cutting a corner, also in Time Trial):
   - Likely cause: the PENA event is sent for warnings, lap invalidations and other notices, not
     only time penalties, and its time field is 255 ("not applicable") when no time applies.
     The code is probably treating every PENA as a time penalty and speaking that field.
     Confirm by logging the raw PENA payload bytes in the detectors log and by --replay --explain
     on a recording where this happened.
   - Decode PENA fully (verify every field and both enum tables against EA's F1 25 spec):
     penaltyType, infringementType, vehicleIdx, otherVehicleIdx, time, lapNum, placesGained.
     Map penaltyType and infringementType to names from the spec's appendix tables.
   - Speak by penalty type, never by the raw time field:
       time penalty → "{time}-second time penalty", only if 1 <= time <= 60
       drive-through / stop-go → "Drive-through penalty" / "Stop-go penalty"
       warning → handled by the track-limits warning logic (no "penalty" wording)
       lap invalidated (all the "this lap / next lap / previous lap invalidated" types) → handled
         by the lap-invalidated logic, merged with the warning call, never as a penalty
       anything else → log it, don't speak it unless it's in an explicit allow-list
   - Treat time == 255 as "not applicable". Any time value > 60 s is an anomaly: log it with the
     raw bytes and don't speak it.
   - Time Trial (session type for time trial): never speak penalty calls; only "Lap invalidated"
     (once per lap). Practice and qualifying: no time-penalty wording either unless the penalty
     type is a real time penalty.
   - Deduplicate: a PENA lap-invalidation and the LapData lap_invalid flip within 1 s are one
     event; a PENA warning and the corner_cutting_warnings increment within 1 s are one event.
   - The persona "penalty" and "told_you" lines only trigger for real penalties (time,
     drive-through, stop-go), not warnings or invalidations.

8. Tests: uid 0 doesn't reset; finish detection on status 2→3 with margin buckets and the
   provisional penalty case; only one finish message despite CHQF/RCWN/FinalClassification;
   overtake confirmation vs side-by-side flicker; pit/lapped/stricken filters; each overtake
   class with synthetic features; switchback; told_you timing; defend warning trigger, inside
   look, and spotter priority; dashboard sends full state on reconnect; PENA decoding for each
   penalty type with time 255 never spoken; time > 60 s treated as anomaly; Time Trial never
   speaks penalties; PENA + LapData dedupe for invalidations and warnings.
```
