"""Unit tests for Prompt 25: Seed Reference Data Import.

Tests:
1. Idempotent import (re-running doesn't duplicate)
2. Validation flags without rejecting (values preserved, tyre pressures flagged)
3. Base setup computation (median across 11 teams, diff_from_base)
4. Merge preserves user-edited track notes (custom sections & text kept)
5. Corner matching on synthetic track model (sector + order + low-speed matching)
6. Unknown track ID prompt and confirmation
7. Team ID mapping persistence
8. Engineer setup intent recommendation with click comparison
"""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import pytest

from seed_import import SeedImporter
from trackmodel import TrackModel, Corner, BinData


@pytest.fixture
def tmp_seed_env(tmp_path: Path):
    """Sets up temporary databases and folders for testing SeedImporter."""
    tracks_db = str(tmp_path / "test_tracks.db")
    setups_db = str(tmp_path / "test_setups.db")
    knowledge_dir = str(tmp_path / "knowledge" / "tracks")
    lines_file = str(tmp_path / "cocky.yaml")

    # Create dummy lines file
    Path(lines_file).write_text("overtake_made:\n  - text: 'Nice pass.'\n", encoding="utf-8")

    importer = SeedImporter(
        tracks_db_path=tracks_db,
        setups_db_path=setups_db,
        knowledge_tracks_dir=knowledge_dir,
        lines_path=lines_file,
    )
    return importer, tmp_path, tracks_db, setups_db, knowledge_dir, lines_file


def test_idempotent_import(tmp_seed_env):
    """Importing twice produces the same record counts without duplicating rows."""
    importer, tmp_path, tracks_db, setups_db, knowledge_dir, lines_file = tmp_seed_env

    # First import
    res1 = importer.import_all(reimport=False)
    assert res1["tracks"]["count"] == 25
    assert res1["setups"]["imported"] == 275
    assert res1["setups"]["base_count"] == 25
    assert res1["track_notes"]["count"] == 25
    assert res1["track_turns"]["count"] == 181

    # Second import without reimport flag
    res2 = importer.import_all(reimport=False)
    assert res2["setups"]["imported"] == 0
    assert res2["setups"]["base_count"] == 0

    status = importer.get_status()
    assert status["tracks_total"] == 25
    assert status["setups_total"] == 300  # 275 team + 25 derived base
    assert status["turns_total"] == 181


def test_validation_flags_without_rejecting(tmp_seed_env):
    """Values outside confirmed ranges (e.g. low tyre pressures) are flagged, not changed, not rejected."""
    importer, tmp_path, tracks_db, setups_db, knowledge_dir, lines_file = tmp_seed_env
    res = importer.import_setups(reimport=False)
    assert res["imported"] == 275

    with sqlite3.connect(setups_db) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT * FROM setups WHERE source = 'imported' LIMIT 50")
        rows = cur.fetchall()

        # Check that setups with low tyre pressure have validation flags
        found_flagged = False
        for r in rows:
            flags = json.loads(r["validation_flags"])
            params = json.loads(r["params"])
            if (params.get("front_left_tyre_pressure") or 30.0) < 22.5:
                assert any("below game minimum" in f for f in flags)
                # Value was not changed
                assert params["front_left_tyre_pressure"] < 22.5
                found_flagged = True
        assert found_flagged


def test_base_setup_computation(tmp_seed_env):
    """Per track, computes a base setup from the median of 11 teams, with small team diffs."""
    importer, tmp_path, tracks_db, setups_db, knowledge_dir, lines_file = tmp_seed_env
    importer.import_tracks(reimport=False)
    importer.import_setups(reimport=False)

    with sqlite3.connect(setups_db) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # 25 Base setups derived
        cur.execute("SELECT * FROM setups WHERE source = 'derived'")
        base_rows = cur.fetchall()
        assert len(base_rows) == 25

        # Check Bahrain base setup
        cur.execute("SELECT * FROM setups WHERE track_slug = 'bahrain' AND source = 'derived'")
        bahrain_base = cur.fetchone()
        assert bahrain_base is not None
        base_params = json.loads(bahrain_base["params"])
        assert "front_wing" in base_params
        assert "rear_wing" in base_params

        # Check team variants have diff_from_base
        cur.execute("SELECT * FROM setups WHERE track_slug = 'bahrain' AND source = 'imported'")
        team_rows = cur.fetchall()
        assert len(team_rows) == 11
        for tr in team_rows:
            diff = json.loads(tr["diff_from_base"])
            assert isinstance(diff, dict)


def test_merge_preserves_user_edited_notes(tmp_seed_env):
    """Copying track notes merges missing corners/sections while preserving existing user edits."""
    importer, tmp_path, tracks_db, setups_db, knowledge_dir, lines_file = tmp_seed_env
    k_dir = Path(knowledge_dir)
    k_dir.mkdir(parents=True, exist_ok=True)

    # Pre-create user-edited great_britain.md (or silverstone.md)
    user_file = k_dir / "great_britain.md"
    user_content = """# Great Britain
My custom track intro.

## Corners
- T1: Abbey - Flat out right flick, my custom braking marker.

## My Custom Strategy
- Run soft tyres for 18 laps then switch to hards.
"""
    user_file.write_text(user_content, encoding="utf-8")

    importer.import_track_notes(reimport=False)

    merged_text = user_file.read_text(encoding="utf-8")
    # Custom intro preserved
    assert "My custom track intro." in merged_text
    # Custom strategy section preserved
    assert "## My Custom Strategy" in merged_text
    assert "Run soft tyres for 18 laps" in merged_text
    # Missing corners from seed merged in
    assert "T2" in merged_text or "T3" in merged_text or "Brooklands" in merged_text


def test_corner_matching_on_synthetic_track_model(tmp_seed_env):
    """Automatically matches key turns to TrackModel corners using sector, order, and speed profiles."""
    importer, tmp_path, tracks_db, setups_db, knowledge_dir, lines_file = tmp_seed_env
    importer.import_track_turns(reimport=False)

    # Build synthetic 3-sector track model with 7 corners
    c1 = Corner(number=1, name="Turn 1", start_dist_m=200, apex_dist_m=300, end_dist_m=400, direction="right", field_median_min_speed_kph=95.0, braking_start_median_m=150, braking_start_p10_m=140, braking_start_p90_m=160, median_entry_speed_kph=220)
    c2 = Corner(number=2, name="Turn 2", start_dist_m=450, apex_dist_m=550, end_dist_m=650, direction="left", field_median_min_speed_kph=180.0, braking_start_median_m=400, braking_start_p10_m=390, braking_start_p90_m=410, median_entry_speed_kph=240)
    c3 = Corner(number=3, name="Turn 3", start_dist_m=1200, apex_dist_m=1300, end_dist_m=1400, direction="right", field_median_min_speed_kph=240.0, braking_start_median_m=1100, braking_start_p10_m=1090, braking_start_p90_m=1110, median_entry_speed_kph=260)
    c4 = Corner(number=4, name="Turn 4", start_dist_m=2200, apex_dist_m=2300, end_dist_m=2400, direction="right", field_median_min_speed_kph=110.0, braking_start_median_m=2100, braking_start_p10_m=2090, braking_start_p90_m=2110, median_entry_speed_kph=230)
    c5 = Corner(number=5, name="Turn 5", start_dist_m=2600, apex_dist_m=2700, end_dist_m=2800, direction="left", field_median_min_speed_kph=140.0, braking_start_median_m=2500, braking_start_p10_m=2490, braking_start_p90_m=2510, median_entry_speed_kph=210)
    c6 = Corner(number=6, name="Turn 6", start_dist_m=3400, apex_dist_m=3500, end_dist_m=3600, direction="right", field_median_min_speed_kph=105.0, braking_start_median_m=3300, braking_start_p10_m=3290, braking_start_p90_m=3310, median_entry_speed_kph=225)
    c7 = Corner(number=7, name="Turn 7", start_dist_m=4200, apex_dist_m=4300, end_dist_m=4400, direction="right", field_median_min_speed_kph=125.0, braking_start_median_m=4100, braking_start_p10_m=4090, braking_start_p90_m=4110, median_entry_speed_kph=235)

    model = TrackModel(
        track_id=3,
        track_length_m=5412.0,
        corners=[c1, c2, c3, c4, c5, c6, c7],
        bins=[],
        is_ready=True,
    )

    matches = importer.match_turns_to_track_model("bahrain", model, sector2_start_m=1800.0, sector3_start_m=3600.0)
    assert len(matches) > 0

    # Sector 1 low speed Turn 1 should match low speed corner 1
    t1 = next(t for t in matches if t["label"] == "Turn 1")
    assert t1["matched_corner_number"] == 1

    # Confirm match via method
    ok = importer.confirm_turn_match("bahrain", t1["id"], 1)
    assert ok is True


def test_unknown_track_id_prompt_and_confirmation(tmp_seed_env):
    """When a session track ID is unknown or unconfirmed, prompts user and persists confirmation."""
    importer, tmp_path, tracks_db, setups_db, knowledge_dir, lines_file = tmp_seed_env
    importer.import_tracks(reimport=False)

    # Madrid has game_track_id = None in seed tracks.json
    check_unknown = importer.check_track_mapping(99)
    assert check_unknown["known"] is False
    assert "Which track is track id 99?" in check_unknown["prompt"]
    assert "Madrid" in check_unknown["options"]

    # Confirm Madrid is track 99
    ok = importer.confirm_track_mapping("madrid", 99)
    assert ok is True

    check_confirmed = importer.check_track_mapping(99)
    assert check_confirmed["known"] is True
    assert check_confirmed["confirmed"] is True
    assert check_confirmed["slug"] == "madrid"


def test_team_id_mapping_persistence(tmp_seed_env):
    """Unmapped team IDs prompt once and persist confirmed team name."""
    importer, tmp_path, tracks_db, setups_db, knowledge_dir, lines_file = tmp_seed_env

    check = importer.check_team_mapping(14)
    assert check["known"] is False
    assert "Which team are you driving?" in check["prompt"]
    assert "Cadillac" in check["options"]

    ok = importer.confirm_team_mapping(14, "Cadillac")
    assert ok is True

    check2 = importer.check_team_mapping(14)
    assert check2["known"] is True
    assert check2["confirmed"] is True
    assert check2["team_name"] == "Cadillac"


def test_setup_intent_recommendation(tmp_seed_env):
    """Answers what the sheet setup is for a track and team, comparing to car clicks."""
    importer, tmp_path, tracks_db, setups_db, knowledge_dir, lines_file = tmp_seed_env
    importer.import_tracks(reimport=False)
    importer.import_setups(reimport=False)

    my_car_setup = {"front_wing": 33, "rear_wing": 33, "brake_bias": 55}
    rec = importer.get_setup_for_track_and_team("bahrain", "Red Bull", car_setup_params=my_car_setup)
    assert rec["found"] is True
    assert "From the imported sheet (untested in game)" in rec["text"]
    assert "Red Bull" in rec["text"]
    assert "Compared to your car:" in rec["text"]
    assert "+2 front wing" in rec["text"]
