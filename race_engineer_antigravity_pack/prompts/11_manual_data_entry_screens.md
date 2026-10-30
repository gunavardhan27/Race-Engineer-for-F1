# Prompt 11: Manual data entry screens

Before starting: read antigravity_pack/FEATURE_STATUS.md (from prompt 00a) and only build what is missing, partial or broken for this prompt. Extend existing code; don't rebuild what works. Packet layouts: docs/udp_spec_2026.h.

```
Add input screens to the dashboard so I can type in setups and track notes myself. Builds on the
setup library from Prompt 10 and the knowledge files from Prompt 5.

Setup entry form (dashboard "Setups" page → "New setup" / "Edit"):
- Generate the form from a definition file, setup_params.yaml, not hard-coded HTML. Each parameter
  has: key (matching the CarSetups params dict), label, group, unit, type (int/float), step and
  display order. Groups follow the in-game setup screen order: Aerodynamics, Transmission,
  Suspension Geometry, Suspension, Brakes, Tyres, plus Fuel and Ballast.
  If the 2026 season update adds or changes parameters, I only edit this YAML and the form updates.
- Each numeric field has −/+ stepper buttons that move by the game's step (one "click"), plus
  direct typing. Use inputmode="decimal" so phones show a number keypad. Large touch targets.
- Ranges come from setup_param_ranges. If a range is confirmed, clamp steppers to it and flag
  typed values outside it. If a range isn't confirmed yet, allow any number but show a small
  "range not confirmed" note. Never block saving because a range is unknown.
- Every field is optional. Empty stays empty: never fill defaults or guesses into the saved setup.
- Metadata at the top: name, car class, team, track, conditions (dry/wet), session type
  (quali/race), source note and source link, free-text notes. Pre-fill car class, team, track
  and conditions from the live session when one is running; all editable.
- Start options: blank, "copy current car setup" (from the latest captured CarSetups packet), or
  "duplicate" an existing setup and edit it.
- Live diff column next to each field showing the difference from the setup currently on the car
  (e.g. "+2", "−0.10"), so I can see the change in clicks while typing.
- Before saving: server-side validation (same rules as the client), and a duplicate check using
  the setup fingerprint. If identical values already exist, offer to open that setup instead.
- Edit and delete existing setups. Captured setups (source "captured") are read-only; editing one
  creates a new imported copy.
- Autosave the unsaved form to localStorage so a page refresh or phone sleep doesn't lose it;
  clear the draft after saving.
- Desktop: Tab moves field to field in group order, arrow up/down acts as the stepper, Enter on the
  last field saves.

Track notes editor (dashboard "Tracks" page):
- List tracks; open one to edit its knowledge/tracks/<track>.md in a form with one section per
  heading (Corners, Overtaking, Tyres, Setup notes) plus a raw-markdown view.
- Corners section as editable rows: number, name, short description. These feed the AI agent and
  the STT vocabulary (Prompt 2 and Prompt 5), so reload them without restarting the app.
- Save writes the .md file atomically (write temp file, then rename) and keeps the previous version
  as <track>.md.bak.

API:
- REST endpoints under /api: GET/POST/PUT/DELETE /setups, GET /setups/schema (the YAML as JSON,
  merged with ranges), GET /setups/current, GET/PUT /tracks/{track}. All require the dashboard
  token. Validate request bodies with Pydantic models.

Tests: form schema generation from YAML, validation (confirmed range, unconfirmed range, empty
fields), fingerprint duplicate detection, captured-setup read-only rule, atomic track-note save,
and API auth.
```
