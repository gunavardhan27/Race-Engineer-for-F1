# Prompt 17: Radio check (in game) and sound test (in app)

Before starting: read antigravity_pack/FEATURE_STATUS.md (from prompt 00a) and only build what is missing, partial or broken for this prompt. Extend existing code; don't rebuild what works. Packet layouts: docs/udp_spec_2026.h.

```
Add two separate features. Keep them separate in code, UI and naming.

A. IN-GAME RADIO CHECK (during an F1 session, through the normal radio pipeline)

The voice intent "radio_check" already exists in deterministic.py: saying "radio check",
"comms check", "can you hear me" or "do you copy" answers "Loud and clear. Telemetry's live."
(or says no telemetry yet). lines/cocky.yaml has a radio_check pool for the persona. Build:

1. Voice: when radio_check fires and a persona is active, append a random persona radio_check
   line (respecting profanity level). Log the transcript and the latency breakdown
   (PTT up → STT → reply → first audio).

2. Two-way radio check command "radio_check" in commands.yaml (bindable to a wheel button, the
   radio menu, the touch MFD):
   - Engineer: "Radio check, how do you read?"
   - Waits up to 8 s for me to hold PTT and answer.
   - Reply: "Loud and clear. I heard: <transcript>." so I can check transcription accuracy.
   - No audio: "Nothing received. Check your mic." Very low peak level: "Very quiet, check your
     mic level." Empty transcript: "Couldn't make that out, say again."

3. Automatic on session start (config radio_check.on_session_start, default true): 3 s after the
   first packets of a new session: "Radio check. Telemetry's live, we're at <track>."
   Once per session; skipped in quiet mode.

B. APP SOUND TEST (no game needed, works offline, never mentions telemetry)

A "Sound test" panel on the dashboard Settings page, also in the tray menu and the first-run setup:

1. Speaker test: button plays the radio click + "Sound test. One, two, three. Radio's working."
   with local TTS through the selected output device. Toggle "with radio effect" on/off so I can
   compare. Volume slider for engineer voice, saved to config (separate volumes for engineer
   voice, spotter and menu beeps).
2. Mic test: hold the button (or my PTT button if the game isn't running), speak, release →
   plays my recording back through the radio effect and shows the transcript, a live level meter
   while recording, peak level, and a clipping warning.
3. Device pickers for input and output with a Refresh button; changing a device takes effect
   immediately without restarting. If a device disappears (headset unplugged), show a warning
   and fall back to the Windows default device.
4. Startup sound (desktop app, config sound_test.on_app_start, default true): click +
   "Engineer online." when the window first opens, so I know audio routing works before
   launching the game.
5. Sound test is disabled while a session is live (show "Use radio check in game instead") so
   it can't talk over race calls.

Tests: two-way radio check flow with fake audio (normal, silent, too quiet, empty transcript,
timeout), session-start check fires once and skips in quiet mode, persona line with profanity
filter, sound test blocked during a live session, device fallback on removal, volume settings
applied.
```
