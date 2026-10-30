# Prompt 24: Tyre degradation model and strategy prediction

Before starting: read antigravity_pack/FEATURE_STATUS.md (from prompt 00a) and only build what is missing, partial or broken for this prompt. Extend existing code; don't rebuild what works. Packet layouts: docs/udp_spec_2026.h.

```
Build tyre degradation analysis and a race strategy predictor. Deterministic statistics, not an LLM.
Uses the 2026 layouts (docs/udp_spec_2026.h). Check what storage/analysis already exists and reuse it.

PART A — Data per lap, all cars (offline sessions; online players with restricted telemetry give
timing only): compound (actual + visual), tyre age, tyre wear per corner (CarDamage), lap time,
fuel in tank (player; estimate others from lap count), track temperature, weather, and the lap flags
from Prompt 14 (SC/VSC, in/out lap, invalid, traffic, damage). Player also: TyreSets packet (life
span, usable life, lap delta time, wear and availability of every set).

PART B — Degradation model (tyres.py):
1. Representative laps only: valid, no SC/VSC, not in/out lap, no traffic flag, no damage change,
   dry/wet consistent with the compound. Long runs = >= 5 consecutive representative laps on one set.
2. Fuel correction: lap_time_corrected = lap_time - fuel_coef × (fuel_kg - reference_fuel). Estimate
   fuel_coef from my data by regression across stints (prior 0.03 s/kg, config), clamp to a sane range.
3. Per stint fit (robust regression, e.g. Huber):
     pace: corrected_lap_time = base + deg_s_per_lap × tyre_age (+ optional quadratic term when it
           improves the fit significantly, to capture a cliff)
     wear: worst-corner wear % = w0 + wear_per_lap × tyre_age, per corner
   Also detect a cliff: the tyre age/wear where pace loss per lap jumps (change-point on residuals);
   store it per compound per track when found.
4. Combine estimates per track + compound + conditions with weights and uncertainty:
   my race laps (highest) > my practice long runs > my past sessions at this track (database) >
   AI cars this session (lowest, AI tyre behaviour may differ). Bayesian-style update: prior from
   history/AI, updated with my laps; keep a standard error for every estimate.
5. Classification per compound at this track: HIGH / MEDIUM / LOW deg, based on projected laps to the
   cliff (or to a configurable wear limit if no cliff was found) relative to race distance and the
   game's usable life for that compound; show the numbers behind the label (s/lap, %/lap, projected
   laps) and a confidence (low/medium/high from the standard error and lap count).

PART C — Strategy predictor (strategy.py):
1. Inputs: laps remaining, current compound/age/wear, available sets (TyreSets), the deg model with
   uncertainty, fuel, pit loss, safety car state, weather forecast, rule constraints (in a dry race,
   at least two different dry compounds; make rules config-driven), and the game's own pit window
   (Session m_pitStopWindowIdealLap / LatestLap / RejoinPosition) as a baseline.
2. Pit loss: measure from my stops (pit lane time from LapData m_pitLaneTimeInLaneInMS plus the lap
   time difference of in/out laps vs representative laps); prior from config per track until measured.
   Under SC/VSC, apply a reduced pit loss factor (config, learned from data when available).
3. Enumerate plans: 0/1/2/3 stops, compound sequences from available sets, stop laps on every
   feasible lap. Simulate lap by lap with base pace + tyre loss − fuel gain (+ cliff penalty) + pit
   loss. Rank by total time; run Monte Carlo over the deg uncertainty (e.g. 500 samples) to report
   each plan's expected time, spread, and how often it's the best plan.
4. Race context, recomputed every lap: undercut/overcut threat vs the cars within 3 s ahead and behind
   (their tyre ages and deg from Part B, my rejoin position vs traffic using live gaps), SC opportunity
   ("box under the safety car" when it gains time vs the plan), rain crossover (when the forecast and
   lap times say inters are faster).
5. Practice mode: after practice sessions, produce a race plan for the weekend: deg labels per
   compound, recommended plan + plan B, ideal stop windows.

PART D — Output:
- Dashboard Strategy page: deg chart per compound (corrected pace vs tyre age, wear vs age, fitted
  lines with uncertainty bands, cliff markers), HIGH/MEDIUM/LOW badges with numbers and confidence,
  ranked plan table (expected total time, delta to best, spread, win %), the game's pit window for
  comparison, live recommendation, and what-if controls (pit lap, compound, SC now).
- Engineer: deterministic radio calls with confidence — e.g. "Deg's high on mediums, two tenths a
  lap. Plan B is a two-stop." / "Box this lap for the undercut on Norris." / "Safety car, box box,
  it's worth it." Only make a call when confidence is at least medium; otherwise defer to the game's
  pit window and say the data is thin. Respect quiet mode and the radio priorities.
- Snapshot: deg labels with numbers, current plan, next stop lap window, undercut threats, so the AI
  agent answers strategy questions from real numbers.
- Store every prediction with its inputs for later evaluation (predicted vs actual), feeding the ML
  work from Prompt 14.

Tests: fuel correction on synthetic laps, stint fit recovers known deg and cliff, representative lap
filtering, weighting of sources, classification thresholds, plan enumeration respects compound rules
and available sets, Monte Carlo ranking stable with a fixed seed, SC reduced pit loss changes the
recommendation, undercut detection, low-confidence fallback to the game's pit window.
```
