# Prompt 25: Import the seed data (setups, track notes, track and team ids)

Before starting: read antigravity_pack/FEATURE_STATUS.md and antigravity_pack/data/DATA_REVIEW.md.
Requires the setup library (prompt 10/11) and track knowledge (prompt 5); if those are missing,
build the minimum storage they need first.

```
Import the seed data in antigravity_pack/data/ into the app. It is imported, untested data from the
user's spreadsheets: label it that way everywhere and never present it as verified.

1. Tracks and ids (data/tracks.json):
   - Load the 25 tracks with names, slugs and aliases into a tracks table / config.
   - Ids marked "verify" are a starting guess. When a Session packet arrives, look up its track_id;
     if it's unknown or the mapping is unconfirmed, show a one-time prompt on the dashboard
     ("Which track is track id N?" with the 25 names) and store the confirmed mapping. Mark the
     id as confirmed after the first session where the track length and the corner count from the
     track model look right. Madrid has no id yet: learn it this way.
   - Team ids: same approach. The first time a session starts, if the player's Participants team_id
     isn't mapped, ask once "Which team are you driving?" and store team_id → team name. Map other
     cars' team ids the same way only when needed (e.g. for team colours, use the livery colours
     from the Participants packet instead).

2. Setups (data/setups_seed.json, 275 setups):
   - Import into the setup library with source "imported", the given source_note, car class
     "F1 26 (2026 regulations)" (Session m_formula 13), conditions dry, session type any.
   - Keys already match the CarSetups packet field names. Missing params (engine_braking, ballast,
     fuel_load) stay empty.
   - Run the normal import validation against setup_param_ranges. Values outside a confirmed range
     are flagged, not changed, not rejected. DATA_REVIEW.md notes that many tyre pressures may be
     below the game's minimum: show those flags clearly in the UI.
   - Per track, compute a "base" setup (median per parameter across the 11 teams) and show team
     variants as small diffs from the base. Mark the base as derived.
   - Idempotent: re-running the import doesn't duplicate (fingerprint dedupe); a --reimport flag
     replaces only rows whose source_note matches this file.
   - Ranking (prompt 10) treats these as "untested" until I drive laps with a matching setup;
     a captured setup matches an imported one when all present params are equal.
   - Engineer: the "setup" intent can answer "what's the sheet setup for here?" from the matching
     track + my team (fall back to the base), always saying it's from the imported sheet and
     untested, and comparing it to what's on the car in clicks.

3. Track notes (data/knowledge/tracks/*.md and data/track_turns.json):
   - Copy the 25 markdown files into the knowledge folder used by prompt 5, but never overwrite a
     file I've already edited: if a file exists, merge only corners/sections that are missing and
     keep my text.
   - Import track_turns.json as structured corner data (turn numbers, names, sector, speed profile,
     corner type, notes).
   - Match these corners to the track model's corners (prompt 21) automatically, using: same sector
     (from the Session sector 2/3 start distances), lap order, low-speed features ↔ field braking
     zones / low apex speeds, and the turn-number span. Show the suggested matches on the Tracks
     page for me to confirm or fix by clicking; store confirmations.
   - Notes that mention DRS: keep the text, but for 2026 cars the engineer talks about active aero
     straights / Overtake Mode instead.
   - Feed corner names into the STT vocabulary (prompt 2) and use them in every corner reference
     (debrief, overtakes, defend warnings, sector map).

4. Persona lines: compare antigravity_pack/data/lines/cocky.yaml with the project's lines file and
   add any missing events/lines; keep the project's edits.

5. Dashboard: a "Seed data" section in Settings showing import status (counts, flags, last import),
   unconfirmed track/team mappings, and buttons to re-run the import and to confirm mappings.

Tests: idempotent import, validation flags without rejecting, base setup computation, merge that
preserves user-edited notes, corner matching on a synthetic track model, unknown track id prompt,
team id mapping persistence.
```
