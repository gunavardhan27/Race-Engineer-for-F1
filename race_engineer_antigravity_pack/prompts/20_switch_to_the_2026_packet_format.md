# Prompt 20: Switch to the 2026 packet format

Before starting: read antigravity_pack/FEATURE_STATUS.md (from prompt 00a) and only build what is missing, partial or broken for this prompt. Extend existing code; don't rebuild what works. Packet layouts: docs/udp_spec_2026.h.

```
The game is "F1 25: 2026 Season Pack" and sends m_packetFormat 2026. Its official UDP spec is now in
docs/udp_spec_2026.h: treat that file as the source of truth for every packet layout, replacing every
"verify against EA's F1 25 spec" instruction in earlier prompts.

Reference implementation: f1telemetry/packets.py and f1telemetry/state.py in this zip were updated and
cross-checked field by field against the spec (8,450 car records, all packet sizes match). Compare them
with the project's current versions and merge the changes; don't blindly overwrite files that have
been extended since.

1. Breaking changes vs the 2025 format — fix every place in the codebase that depends on them:
   - 24 cars per array (cs_maxNumCarsInUDPData), not 22. Search the whole project for 22, NUM_CARS
     and any fixed-size car arrays/loops (state, spotter, overtake detection, timing tower, dashboard,
     storage, tests) and use the count for the packet's format (packets.num_cars(header)).
   - CarTelemetry: engine temperature is uint8 → 59 bytes per car.
   - CarStatus: new float m_ersHarvestLimitPerLap before m_ersDeployedThisLap → 59 bytes per car.
   - Participants: driverId, networkId and teamId are uint16 → name starts at byte 10; 60 bytes per car.
     Team ids are uint16 everywhere (also LobbyInfo, TimeTrial).
   - Motion: g-forces are int16 quantised (divide by 1000) → 54 bytes per car. Motion is only sent
     while the player is in control.
   - COLL event has a third byte: severity (0 low, 1 medium, 2 high).
   - Session: new active aero zones (full/partial), DRS zones, sector 2/3 start distances,
     m_formula 13 = "F1 26" cars, start reaction time, assists, corner-cutting stringency, and more.
   - New packet 16 CarTelemetry2 (all cars): active aero mode (corner/straight), active aero and
     Overtake Mode availability and activation distance, 2026-regulations flag, driving wrong way.
   - Penalty event: verify the PENA fields against the spec (Prompt 19 step 7 still applies).

2. Startup self-check: log the packet format, game version and, for the first packet of each id, its
   size vs the expected size from the spec table (inspect_telemetry.py has the 2026 sizes). Warn once
   on any mismatch, naming the packet.

3. Use the new 2026 data:
   - Replace DRS logic with active aero and Overtake Mode: callouts like "Overtake mode available"
     when it becomes available within range of the car ahead, and include availability/distance in the
     snapshot and dashboard. Keep DRS handling only for cars where m_2026Regulations = 0.
   - Collision severity: persona "collision" lines only for medium/high; a low-severity touch gets a
     lighter line or none. Store severity in the overtake features (Prompt 19).
   - Driving wrong way → immediate priority-0 call "Wrong way!" (pre-rendered).
   - Start reaction time (Session) → mention in good_start/bad_start ("reaction 0.21").
   - Sector 2/3 start distances → use for sector boundaries on the track map and in analysis.
   - Corner-cutting stringency (regular/strict) → include in track-limits messages and config.
   - TyreSets packet (12): tyre life, usable life and lap delta per set → strategy answers and the
     snapshot. LapPositions packet (15): position-per-lap chart on the dashboard.
   - Assist settings (braking, traction, ABS, gearbox, racing line) → store per session for the data
     report and ML (Prompt 14); the expected-performance logic (Prompt 18) should only compare
     sessions with comparable assists.

4. Keep 2025-format support working (some players may select the 2025 UDP format in-game):
   per-format layouts chosen by header.packet_format, defaulting to the 2026 layouts for unknown
   newer formats.

5. Tests: parse synthetic 2026 packets of the exact spec sizes for every packet id we use, 24-car
   arrays end to end (car index 23 works in state, spotter, overtakes and the timing tower),
   participant names with uint16 team ids, COLL severity, CarTelemetry2 calls, wrong-way call,
   and the startup size check.
```
