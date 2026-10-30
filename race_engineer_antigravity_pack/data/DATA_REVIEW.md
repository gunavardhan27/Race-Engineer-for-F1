# Review of the uploaded spreadsheets

Both spreadsheets were converted into import files in this folder. They are useful starting data,
but neither has been verified in the game, so the app treats them as **imported and untested** and
ranks setups by your own lap times once you drive them.

## F1_26_Complete_Matrix_275_Setups.xlsx → setups_seed.json

- 25 tracks × 11 teams (Red Bull, Ferrari, McLaren, Mercedes, Aston Martin, Williams, Alpine, RB,
  Haas, Audi, Cadillac) = 275 setups, no duplicates.
- Overall trends are sensible: low wing at Monza (12/10), Las Vegas, Spa and Baku; maximum at
  Monaco (50/45), Singapore and Hungary.
- **Team variants are nearly identical.** Within a track, teams differ by at most 1–3 clicks, and
  camber, toe and brake pressure are identical for all teams. In the F1 games every team gets the
  same setup options, so treat each track as one base setup with small variations, not 11
  genuinely different setups.
- **Tyre pressures may be below the game's minimum.** 121 of 275 front pressures are below
  22.5 psi and 171 of 275 rear pressures below 20.5 psi. In recent F1 games the minimums have been
  around those values, so the game may not allow them. The app's range check will flag these once
  it has learned the real ranges from your captured setups.
- Front toe is 0.00 everywhere, and brake pressure 100% everywhere.
- **Missing:** engine braking, ballast, fuel load. Left empty (never filled with guesses).
- The spreadsheet has one value per axle for tyre pressure; it was applied to both wheels of
  that axle.
- All setups are labelled for **F1 26 cars (2026 regulations, m_formula 13)**, dry, any session
  type. The 2026 CarSetups packet has the same setup fields as before, so the format still fits.

## F1_26_All_25_Circuits_Detailed_Turn_by_Turn.xlsx → track_turns.json + knowledge/tracks/*.md

- 25 tracks, 181 features: **key corners only (6–12 per track), not every corner.**
- Corner names are present for 52 features (e.g. Copse, Eau Rouge/Raidillon, Spoon, Parabolica);
  others are turn numbers only. Names I spot-checked for Silverstone, Spa, Monza, Suzuka, Imola,
  Monaco and Bahrain match the real circuits.
- **Madrid** (new for 2026) could not be checked; treat its turn numbers and names as unverified.
- **6 notes mention DRS.** 2026 cars use active aero and Overtake Mode instead; DRS applies only to
  pre-2026 cars. The notes were kept, with a comment in the affected track files.
- Turn numbers are official ones. The app's track model (Prompt 21) numbers corners by curvature,
  which may differ (kinks, chicanes merged), so corners are matched by sector + lap order and you
  confirm the match on the Tracks page.
- The knowledge files follow the Prompt 5 format (Corners, Overtaking, Tyres, Setup notes) and
  are meant to be edited.

## tracks.json

- Maps the 25 track names (with aliases like Silverstone, Spa, Suzuka) to the game's track ids.
- Ids are from the F1 25 track list as I recall it. **Verify each one**: run
  `python inspect_telemetry.py` on a track and compare the printed `track_id`.
- Madrid's id is unknown: the app should ask you which track it is the first time it sees an
  unknown track id, and remember the answer.
- Team ids are not in the spec file I found either; the app should ask once which team your
  team id is (inspect_telemetry.py prints your team_id).
