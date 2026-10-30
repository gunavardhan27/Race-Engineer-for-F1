# Prompt 14: Record everything, ML-ready

Before starting: read antigravity_pack/FEATURE_STATUS.md (from prompt 00a) and only build what is missing, partial or broken for this prompt. Extend existing code; don't rebuild what works. Packet layouts: docs/udp_spec_2026.h.

> **Note:** Packet layouts: docs/udp_spec_2026.h. SessionHistory, TyreSets and LapPositions are all in that spec.

```
Make sure every session produces complete, clean data that a future RAG pipeline and local ML
models (tyre degradation, pace, setup recommendation, mistake patterns) can be trained on.
Do NOT build any RAG or ML now; only the data foundation.

1. Raw recordings, always on:
   - Record every session's raw UDP packets automatically (reuse the --record format) to
     data/raw/<date>_<track>_<session_type>_<session_uid>.bin.zst, compressed with zstandard
     in a background thread.
   - Link each recording to its row in the sessions table, so any feature can be recomputed
     later from raw data, even ones we haven't thought of yet.
   - Disk budget: config data.raw_max_gb (default 20). When exceeded, delete the oldest raw
     files but never their derived database rows. Log how much space each hour of racing uses.

2. More data per lap (add to the laps table or a new lap_features table):
   - Per-corner tyre surface and inner temps (lap average and max), tyre pressures, brake temps.
   - Fuel used, ERS deployed and harvested, ERS mode time share.
   - Track temp, air temp, weather and rain percentage during the lap.
   - Setup id (Prompt 10), tyre compound and age, fuel-corrected flag inputs (fuel at lap start).
   - Flags for: in/out lap, safety car or VSC during lap, traffic (car within 1.5 s ahead for
     >30% of the lap), invalid, flashback used, collision, spin.
   - Mistake events with lap distance and corner (spin, lockup if detectable, off-track,
     collision), from the personality and track-limits features.

3. All cars, not just me:
   - Parse the SessionHistory packet (id 11) and store every car's lap and sector times and tyre
     stints. Verify its layout against EA's F1 25 spec. This is the data for opponent pace and
     undercut models later.

4. Text data for RAG later (store, don't index yet):
   - Table radio_log: timestamp, session, lap, speaker (driver / engineer / spotter / persona),
     text, intent, and which provider answered.
   - Post-race messages, track notes edits (with version history), setup notes and import
     source text.

5. Data quality and documentation:
   - Every table gets a schema_version and game_format column.
   - docs/DATA_DICTIONARY.md: every table and column, with units (kph, °C, kg, ms, metres) and
     where the value comes from (packet + field).
   - `python main.py --export parquet <dir>`: export all tables to Parquet (pyarrow), plus
     lap_traces expanded to one row per sample, for use in notebooks later.
   - `python main.py --data-report`: number of sessions, laps, valid laps and stints per track
     and car class, so I can see when there's enough data for each future model.

Tests: recording rotation and disk budget, lap feature extraction on synthetic laps,
SessionHistory parsing, radio log writes, Parquet export round-trip, data report counts.
```
