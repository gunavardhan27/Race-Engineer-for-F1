# Prompt 9: Session storage and lap analysis

Before starting: read antigravity_pack/FEATURE_STATUS.md (from prompt 00a) and only build what is missing, partial or broken for this prompt. Extend existing code; don't rebuild what works. Packet layouts: docs/udp_spec_2026.h.

```
Add persistent session storage and lap analysis.

Storage (storage.py), SQLite with WAL mode, one writer thread fed by a queue so the asyncio loop
never blocks on disk:
- sessions: id, session_uid, started_at, game_format, formula (car class from Session packet
  m_formula), track_id, session_type, team_id, weather at start.
- laps: session_id, lap number, lap time, sector times, valid flag, position, tyre compound and
  age, fuel at start/end, tyre wear at start/end, setup_id (link to Prompt 10, nullable).
- lap_traces: for the player only, resampled every 5 m of lapDistance: speed, throttle, brake,
  steer, gear, rpm, and world X/Z. Store each lap's trace as one compressed blob (numpy arrays,
  zlib) to keep the DB small.
- Write a lap only when it completes. Discard partial laps on flashback/restart (RaceState
  already handles flashbacks for lap records; reuse that logic).
- Add the m_formula field to the Session parser if it isn't already exposed (it sits right after
  m_trackId in the session header; it's already unpacked as s[7] in parse_session).

Analysis (analysis.py):
- Personal best per track + car class + session type.
- Delta-to-best trace: time delta vs distance between any lap and a reference lap.
- Corner breakdown: split the lap at speed minima (or at corners from the knowledge notes if
  present) and show time gained/lost per corner vs the reference lap, with min speed, braking
  point and throttle pickup point for each.
- Stint view: lap time and tyre wear per lap across a stint, with a fitted degradation slope.

Dashboard "Laps" page:
- List sessions and laps with filters (track, car class, session type, valid only).
- Overlay two laps: speed, throttle, brake and delta charts on a shared distance axis, plus the
  track map with the slower/faster segments colored.
- Corner table and stint chart.
- Charts must stay responsive on a phone; downsample for display.

Engineer integration: add "personal_best" and "last_lap_vs_best" (overall and worst corner) to the
snapshot so the LLM can answer "where am I losing time?" from real data.

Tests: storage round-trip, trace resampling, delta computation on synthetic laps, corner split.
```
