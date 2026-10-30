# Prompt 22: Who was faster where — sectors, mini-sectors and corners on the track map

Before starting: read antigravity_pack/FEATURE_STATUS.md (from prompt 00a) and only build what is missing, partial or broken for this prompt. Extend existing code; don't rebuild what works. Packet layouts: docs/udp_spec_2026.h.

```
Extend the Laps page track map so it shows who was faster in each part of the lap, for me and for the
whole field. Uses the track model (Prompt 21) and the 2026 layouts (docs/udp_spec_2026.h, 24 cars).
Check what's already built (storage, Laps page, track map, analysis) and extend it; don't duplicate.

1. Timing data for ALL cars (not just me):
   - Official sectors: SessionHistory packet (id 11) for every car — lap and sector times plus the
     valid bit flags (lap, S1, S2, S3). Sector boundaries from Session m_sector2LapDistanceStart /
     m_sector3LapDistanceStart.
   - Mini-sectors: for every car, every lap, record the lap time at each 10 m of lap distance from
     LapData (m_lapDistance, m_currentLapTimeInMS), interpolated. Store one compact array per car per
     lap (uint32 ms, zlib) in a lap_timing table. From these compute segment times for:
       mini-sectors (config, default 25 equal segments per lap) and corners (track model
       corner start→end, plus the straights between them).
   - Speed traces for all cars at 10 m bins from CarTelemetry (speed, throttle, brake), compressed,
     so "why" can be shown (min speed, braking point). Note: in online lobbies other players may
     have restricted telemetry (Participants m_yourTelemetry = 0); then only timing is available
     for them; show "telemetry restricted" instead of empty charts.
   - Mark laps with SC/VSC, pit in/out, invalid, or traffic flags (Prompt 14) and exclude them from
     "best" calculations by default (toggle to include).

2. Comparison modes on the Laps page (selector above the map):
   a) Lap vs lap: any two laps (mine or any driver's, any lap). Each mini-sector colored by who was
      faster (my color vs their color), intensity by the size of the delta; a delta strip below the map.
   b) Me vs reference: reference = my best / session fastest / pole lap / teammate's best /
      car ahead's last lap.
   c) Field view ("fastest by mini-sector"): each mini-sector colored by the team color of the driver
      with the fastest time there this session (team colors from Participants m_liveryColours;
      fall back to a fixed palette). Legend lists drivers by number of mini-sectors won.
   d) F1-style sector colors for any lap: purple = session best, green = that driver's personal best,
      yellow = slower; for S1/S2/S3 and for mini-sectors.

3. Interaction:
   - Tap/hover a segment: segment name (corner name from track notes if set, else "T5" or
     "Straight after T4"), each compared driver's time, the delta, entry/min/exit speed, and braking
     point when telemetry is available.
   - Tap a driver in the legend to compare against them.
   - Synced cursor between the map, the delta strip and the speed/throttle/brake charts.
   - Works on a phone: precompute segment data on the server, send compact arrays; render on
     canvas; no more than ~2,000 drawn segments.

4. Summaries next to the map:
   - Theoretical best for each driver (sum of their best mini-sectors) vs their actual best lap,
     and the gap for me: "Your ideal lap is 1:27.312, 0.41 faster than your best".
   - "Where I lose most": top 3 segments vs the selected reference, with the time and the main
     difference (braking earlier, lower min speed, later on throttle).
   - Sector table: S1/S2/S3 for the compared laps with purple/green/yellow.

5. Engineer integration:
   - Snapshot fields: my theoretical best, biggest loss segment vs the session fastest lap.
   - Deterministic intents: "where am I slow", "who's fastest in sector 2", "what's my ideal lap",
     answered from this data.
   - Post-session radio message can mention the biggest segment loss vs the fastest driver.

6. Live view: during a session, the field view (2c) and my last lap vs my best (2b) update after
   every completed lap via the existing WebSocket; sending only the changed laps.

Tests: mini-sector times from synthetic distance/time series (including laps starting with negative
lap distance before the line), sector colors (purple/green/yellow), fastest-by-mini-sector winner per
segment, theoretical best, exclusion of flagged laps, restricted-telemetry handling, payload size cap.
```
