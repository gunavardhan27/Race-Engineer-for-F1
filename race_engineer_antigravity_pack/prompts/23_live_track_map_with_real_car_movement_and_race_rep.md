# Prompt 23: Live track map with real car movement (and race replay)

Before starting: read antigravity_pack/FEATURE_STATUS.md (from prompt 00a) and only build what is missing, partial or broken for this prompt. Extend existing code; don't rebuild what works. Packet layouts: docs/udp_spec_2026.h.

```
Upgrade the dashboard track map to show all cars moving smoothly in real time, using Motion (id 0,
24 cars, world x/y/z, velocity, forward/right vectors) and the track model from Prompt 21. Check what
the current map already does and extend it.

1. Server stream:
   - A dedicated WebSocket channel /ws/positions at the telemetry rate (up to 20 Hz), separate from
     the slower state channel. Per car: index, x, z (quantised to 0.1 m), heading (from the forward
     vector), speed, position, status flags (pit, retired, out lap, player, wrong way). Use a compact
     binary or array format, not verbose JSON objects. Drop frames for slow clients.
   - Fallback when Motion isn't arriving (player not in control, spectating, garage): place cars on the
     track model centreline by LapData lap distance, flagged as "estimated".

2. Smooth rendering (browser):
   - Buffer ~100 ms and interpolate positions linearly and headings by shortest angle between
     packets, rendering with requestAnimationFrame at the display rate. If a packet is late,
     extrapolate with velocity for up to 200 ms, then hold.
   - Canvas with the static track layer (centreline, used edges, sector lines, corner numbers/names,
     active aero zones) cached offscreen; only cars are redrawn each frame.
   - Cars: team color (Participants m_liveryColours, fallback palette), position number, small heading
     arrow; my car highlighted and drawn on top; pit-lane cars dimmed; retired cars removed.

3. Overlays (toggles):
   - Battles: pairs within 1.0 s highlighted with a link line and the gap.
   - Marshal zones with yellow flags (Session m_marshalZones flag = 3) shaded on the track.
   - Safety car / VSC banner; overtake events flash briefly at the location.
   - Gap bubbles to the car ahead and behind me.

4. Views: whole track (default), "follow me" (zoom and pan with my car, rotation optional), tap a car
   to follow it and show its name, gap and tyre info.

5. Replay mode: load any recorded session (Prompt 14 raw recordings) and play it on the same map with
   play/pause, 1×/2×/4×/8× speed and a scrubbable timeline with markers for overtakes, pit stops,
   SC periods and incidents. Seeking uses a keyframe index built when the recording is first opened.

6. Performance: 60 fps on a mid-range phone with 24 cars; CPU use on the laptop server under ~2%;
   bandwidth under ~50 KB/s per client.

Tests: binary frame encoding/decoding round trip, interpolation and extrapolation timing, fallback
placement from lap distance, replay seek accuracy, frame dropping for a slow client.
```
