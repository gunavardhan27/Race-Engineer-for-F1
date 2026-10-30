# Prompt 15: Command system — wheel buttons, radio menu, touch MFD, hotkeys

Before starting: read antigravity_pack/FEATURE_STATUS.md (from prompt 00a) and only build what is missing, partial or broken for this prompt. Extend existing code; don't rebuild what works. Packet layouts: docs/udp_spec_2026.h.

```
Add a command system so I can request information without speaking. Voice stays available; these
are extra ways to trigger the same commands. Note: the game's own MFD can't be modified (UDP is
output-only), so this is our own MFD-style layer.

1. commands.yaml — one place that defines every command:
    commands:
      gaps:          {label: "Gaps",          intents: [gap_ahead, gap_behind]}
      tyres:         {label: "Tyres",         intents: [tyres]}
      fuel:          {label: "Fuel",          intents: [fuel]}
      weather:       {label: "Weather",       intents: [weather]}
      pit_window:    {label: "Pit window",    intents: [pit_window]}
      damage:        {label: "Damage",        intents: [damage]}
      position:      {label: "Position",      intents: [position, laps]}
      lap_summary:   {label: "Lap summary",   action: lap_debrief}       # last lap debrief on demand
      strategy:      {label: "Strategy",      ai: "Strategy check. Should we change anything? One recommendation."}
      setup_advice:  {label: "Car balance",   ai: "Based on my last laps, what should I change from the car right now?"}
      time_loss:     {label: "Where am I slow", ai: "Where am I losing the most time versus my best lap?"}
      repeat:        {label: "Repeat",        action: repeat_last}
      quiet_mode:    {label: "Quiet mode",    action: toggle_quiet}       # only critical calls + spotter
      persona_off:   {label: "Banter off",    action: toggle_persona}
   - "intents" use the existing deterministic answers in deterministic.py (combine multiple
     answers into one transmission). "ai" sends that fixed question through the agent (Prompt 3)
     and the usage guard (Prompt 13). "action" runs a built-in function.
   - Validate on load: unknown intents/actions are a startup error with a clear message.
   - Repeat_last replays the last engineer transmission (keep the rendered audio, don't re-synthesize).
   - Quiet mode: only spotter, safety car/VSC, damage, penalty and pit-window-closing calls play;
     everything else is dropped (not queued). Persona reactions off. Announce "Quiet mode on/off."

2. Bindings (same file):
    bindings:
      udp_action:              # in-game "UDP Action N" bound to wheel buttons
        1: {tap: lap_summary, hold: ptt}     # hold > 300 ms = push-to-talk, short tap = command
        2: gaps
        3: tyres
        4: fuel
        5: menu_open
        6: menu_next
        7: menu_select
        8: repeat
      hotkeys:                 # global keyboard shortcuts (also works with Stream Deck / button boxes)
        "ctrl+alt+1": gaps
        "ctrl+alt+2": strategy
   - UDP actions come from BUTN events (RaceState already tracks button bits; generalize the PTT
     edge detection to all 12 UDP Action bits and emit ("button", n, pressed)).
   - Tap vs hold per button: a press shorter than hold_ms (default 300) is a tap; longer is a hold.
     For a button whose hold is PTT, start capturing audio immediately on press (pre-roll) and
     discard it if the press turns out to be a tap.
   - Hotkeys: use a global hotkey library that works while the game has focus on Windows.
     Make hotkeys optional (config hotkeys.enabled) and document if it needs admin rights.
   - Debounce: ignore repeats of the same command within 1.5 s.

3. Radio menu (3-button MFD):
   - menu: ordered list of command ids in commands.yaml (default: gaps, tyres, fuel, weather,
     pit_window, damage, lap_summary, strategy, setup_advice, time_loss, quiet_mode, repeat).
   - menu_open speaks the current item's label; menu_next advances (wraps) and speaks the label;
     menu_select runs it. Menu closes after 5 s of no input or after a selection; it reopens on
     the last-used item.
   - Pre-render all labels to audio at startup so the menu is instant; speak labels with a
     short, quieter beep before them so they're distinguishable from engineer messages.
   - The spotter interrupts menu audio; persona reactions never interrupt it.

4. Touch MFD on the dashboard:
   - "MFD" page: big buttons for every command (order from the menu list), sized for a phone or
     tablet mounted next to the wheel. Tap → POST /api/command/{id} (token-protected).
   - The answer is spoken on the radio and also shown on the button page for 10 s.
   - Highlight state for toggles (quiet mode, banter).
   - Optional "lock" toggle so accidental touches are ignored.

5. Response routing:
   - Every command, whatever triggered it, goes through one function run_command(id, source)
     that logs to radio_log (Prompt 14) with the source (voice, udp_action, menu, mfd, hotkey).
   - Command answers use priority -1 (same as replies to the driver).

Tests: commands.yaml validation, multi-intent answer composition, tap vs hold timing, PTT pre-roll
discarded on tap, menu navigation and timeout, quiet mode filtering, repeat_last, debounce, and the
MFD API with and without the token.
```
