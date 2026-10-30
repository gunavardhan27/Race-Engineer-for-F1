# Prompt 7: Live dashboard

Before starting: read antigravity_pack/FEATURE_STATUS.md (from prompt 00a) and only build what is missing, partial or broken for this prompt. Extend existing code; don't rebuild what works. Packet layouts: docs/udp_spec_2026.h.

```
Add a live web dashboard.

Backend (dashboard/server.py):
- FastAPI + uvicorn, started from main.py inside the same asyncio loop (uvicorn.Server with
  config, not uvicorn.run). Config: dashboard.enabled, host (default 0.0.0.0), port (8765).
- WebSocket /ws/live pushes state.snapshot() plus extra fields at 10 Hz (configurable, max 20).
  Only send when something changed. One broadcaster task, many clients; a slow client must never
  block others (per-client queue of size 1, drop old frames).
- Add a timing-tower view to RaceState: a method returning all running cars ordered by position
  with name, team id, gap to car ahead, gap to leader, last lap, tyre compound and age, pit stops,
  and penalties. Include it in the live payload.
- Simple shared-secret access: a random token generated on first run, stored in config, required as
  a query parameter. Print the dashboard URL with the token and a QR code (qrcode package) in the
  terminal at startup so I can open it on my phone.
- Windows note in README: allow the port through Windows Firewall on private networks only.

Frontend (dashboard/static/):
- Plain HTML/CSS/JS, no build step. Small libraries only, vendored locally (no CDN at runtime).
- Responsive: phone portrait, tablet landscape and desktop. Dark theme by default, light theme
  toggle. Big, high-contrast numbers readable at arm's length.
- Live page panels: position/lap/last lap; gap ahead and behind with per-lap trend arrows;
  four-corner tyre widget (wear % and surface temp, color-coded); fuel (kg, laps delta, kg/lap);
  ERS bar; damage; weather now + next rain; pit window; safety car banner; timing tower.
- A radio log panel showing every message the engineer said and every driver transcript, pushed
  over the same WebSocket.
- Auto-reconnect with backoff if the connection drops; show "no telemetry" when no packets arrive
  for 3 s.

Tests: payload builder, change detection, broadcaster with one slow and one fast fake client,
token check.
```
