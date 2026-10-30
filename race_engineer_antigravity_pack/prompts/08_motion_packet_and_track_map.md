# Prompt 8: Motion packet and track map

Before starting: read antigravity_pack/FEATURE_STATUS.md (from prompt 00a) and only build what is missing, partial or broken for this prompt. Extend existing code; don't rebuild what works. Packet layouts: docs/udp_spec_2026.h.

> **Note:** SUPERSEDED IN PART: the Motion layout here is the old F1 25 one. Use docs/udp_spec_2026.h (54 bytes per car, int16 g-forces ÷ 1000, 24 cars) — Prompt 20. The player-only outline is replaced by the track model in Prompt 21.

```
Add the Motion packet and a live track map.

Parser (f1telemetry/packets.py):
- Add PacketId.MOTION = 0 and parse_motion. Per-car CarMotionData in F1 25 is 60 bytes:
  float worldPositionX, Y, Z; float worldVelocityX, Y, Z; int16 worldForwardDirX, Y, Z;
  int16 worldRightDirX, Y, Z; float gForceLateral, gForceLongitudinal, gForceVertical;
  float yaw, pitch, roll. Verify this against EA's F1 25 spec before writing it, and use the
  existing _stride() size check. Only keep position X/Z, yaw and the three g-forces.
- RaceState stores motion for all cars.

Track outline (trackmap.py):
- Build the outline from the player's world X/Z position sampled every ~5 m of lapDistance
  during the first clean (valid, no pit) lap at a track. Save it to SQLite keyed by track_id with
  the track length. Reuse it on later sessions; rebuild if I press "rebuild" in the UI.
- Until an outline exists, draw cars on a blank canvas scaled to positions seen so far.
- Add the start/finish line and sector boundaries (from the sector field in LapData).

Dashboard:
- Track map page (canvas, scales to screen): outline, all cars as dots with position number,
  player highlighted, car ahead and behind in a second color, pitting cars greyed.
- Include compact car positions (index, x, z) in the live payload at 10 Hz.
- Optional: g-force "friction circle" widget for the player.

Tests: motion parsing with synthetic packets (same style as test_synthetic.py), outline sampling,
coordinate scaling.
```
