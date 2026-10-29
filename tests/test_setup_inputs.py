"""Tests for Setup Entry Form and Track Notes Editor.

Covers:
- Form schema generation from setup_params.yaml merged with storage ranges
- Validation rules: confirmed ranges flagged, unconfirmed ranges allowed, empty fields left empty
- Fingerprint duplicate detection and force-save behavior
- Captured setups read-only rule (PUT fork vs error, DELETE error)
- Atomic track note save and backup (.bak preservation and in-memory reload)
- API endpoint contracts and auth enforcement
"""
import os
import shutil
import tempfile
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from setups import (
    SetupsStorage,
    load_setup_param_definitions,
    get_setup_schema,
    validate_setup_input,
    compute_setup_fingerprint,
)
from config import KnowledgeConfig
from knowledge import KnowledgeBase
from f1telemetry.state import RaceState
from dashboard.server import create_app, Broadcaster


@pytest.fixture
def temp_db(tmp_path):
    db_file = tmp_path / "test_setups.db"
    storage = SetupsStorage(db_path=str(db_file))
    # Seed a confirmed range for front_wing, rear_wing, and brake_bias
    storage.set_param_range("front_wing", 0, 1.0, 50.0, 1.0)
    storage.set_param_range("rear_wing", 0, 1.0, 50.0, 1.0)
    storage.set_param_range("brake_bias", 0, 50.0, 70.0, 1.0)
    return storage


@pytest.fixture
def temp_knowledge(tmp_path):
    k_dir = tmp_path / "knowledge"
    tracks_dir = k_dir / "tracks"
    tracks_dir.mkdir(parents=True)

    # Initial silverstone track notes
    initial_md = """# Silverstone Circuit

## Corners
1. Abbey (T1): Fast right-hander taken in 7th gear.
2. Farm Curve (T2): Flat out left curve.
3. Village (T3): Heavy braking right-hander into 3rd gear.
4. The Loop (T4): Tight left hairpin.

## Overtaking
DRS on the Wellington Straight and Hangar Straight.

## Tyres
Front-left takes heavy punishment through Becketts and Copse.

## Setup notes
High downforce with stiff front suspension for high-speed responsiveness.
"""
    (tracks_dir / "silverstone.md").write_text(initial_md, encoding="utf-8")
    cfg = KnowledgeConfig(tracks_dir=str(tracks_dir), general_path=str(k_dir / "general.md"))
    kb = KnowledgeBase(config=cfg)
    kb.load_track("silverstone")
    return kb, tracks_dir


# ==============================================================================
# 1. Schema Generation from YAML
# ==============================================================================
def test_schema_generation_from_yaml(temp_db):
    """Schema must be generated dynamically from setup_params.yaml and merged with ranges."""
    yaml_file = Path("setup_params.yaml")
    assert yaml_file.exists(), "setup_params.yaml must exist"

    # Raw definition loading
    defs = load_setup_param_definitions(yaml_file)
    assert defs["version"] >= 1
    assert any(g["name"] == "Aerodynamics" for g in defs["groups"])

    # Merged schema
    schema = get_setup_schema(car_class=0, storage=temp_db, yaml_path=yaml_file)
    assert schema["car_class"] == 0
    assert any(g == "Aerodynamics" or (isinstance(g, dict) and g.get("name") == "Aerodynamics") for g in schema["groups"])
    assert len(schema["parameters"]) >= 23

    # Check confirmed range parameter
    fw = next(p for p in schema["parameters"] if p["key"] == "front_wing")
    assert fw["label"] == "Front Wing Aero"
    assert fw["group"] == "Aerodynamics"
    assert fw["has_confirmed_range"] is True
    assert fw["min"] == 1
    assert fw["max"] == 50
    assert fw["type"] == "int"

    # Check unconfirmed range parameter (e.g. ballast if not in ranges)
    ballast = next(p for p in schema["parameters"] if p["key"] == "ballast")
    assert ballast["label"] == "Ballast"
    assert ballast["has_confirmed_range"] is False
    assert ballast["min"] is None
    assert ballast["max"] is None


# ==============================================================================
# 2. Validation Rules
# ==============================================================================
def test_validation_rules(temp_db):
    """Validation must clamp/flag confirmed ranges, allow unconfirmed ranges, and keep empty fields empty."""
    # Test 1: Out of range for confirmed parameter
    result = validate_setup_input(
        {"front_wing": 55, "rear_wing": 30},
        car_class=0,
        storage=temp_db,
    )
    assert result["valid"] is False
    assert "front_wing" in result["out_of_range"]
    assert result["out_of_range"]["front_wing"]["value"] == 55
    assert result["out_of_range"]["front_wing"]["min"] == 1
    assert result["out_of_range"]["front_wing"]["max"] == 50

    # Test 2: Unconfirmed range parameter allows any number and does NOT fail validation
    result2 = validate_setup_input(
        {"front_wing": 35, "ballast": 12},  # ballast has no confirmed range in temp_db
        car_class=0,
        storage=temp_db,
    )
    assert result2["valid"] is True
    assert result2["clean_params"]["front_wing"] == 35
    assert result2["clean_params"]["ballast"] == 12
    assert "ballast" in result2["unconfirmed_ranges"]

    # Test 3: Empty fields stay empty; never populated with defaults or guesses
    result3 = validate_setup_input(
        {"front_wing": 30, "brake_bias": ""},  # brake_bias is empty string
        car_class=0,
        storage=temp_db,
    )
    assert result3["valid"] is True
    assert "front_wing" in result3["clean_params"]
    assert "brake_bias" not in result3["clean_params"]
    assert "rear_wing" not in result3["clean_params"]
    assert "rear_wing" in result3["missing"]


# ==============================================================================
# 3. Fingerprint Duplicate Detection
# ==============================================================================
def test_fingerprint_duplicate_detection(temp_db):
    """Creating a setup with identical parameter values must flag duplicate unless forced."""
    params = {"front_wing": 35, "rear_wing": 30, "brake_bias": 55}
    fp = compute_setup_fingerprint(params)

    # 1. Create first setup
    first, is_dup = temp_db.create_custom_setup(
        params=params,
        name="Baseline Setup",
        car_class=0,
        track_id=7,
        conditions="dry",
        session_type="race",
        team_id=1,
    )
    assert is_dup is False
    assert first["fingerprint"] == fp

    # 2. Duplicate without force -> flags is_dup=True and returns existing
    existing, is_dup2 = temp_db.create_custom_setup(
        params=params,
        name="Identical Setup",
        car_class=0,
        track_id=7,
        conditions="dry",
        session_type="race",
        team_id=1,
        force=False,
    )
    assert is_dup2 is True
    assert existing["id"] == first["id"]

    # 3. Duplicate with force=True -> succeeds by updating existing setup
    second, is_dup3 = temp_db.create_custom_setup(
        params=params,
        name="Identical Setup Forced",
        car_class=0,
        track_id=7,
        conditions="dry",
        session_type="race",
        team_id=1,
        force=True,
    )
    assert is_dup3 is False
    assert second["id"] == first["id"]
    assert second["name"] == "Identical Setup Forced"
    assert second["fingerprint"] == fp


# ==============================================================================
# 4. Captured Setup Read-Only Rule
# ==============================================================================
def test_captured_setup_read_only_rule(temp_db):
    """Captured setups must be immutable; editing forks into a copy, deleting is rejected."""
    # Seed a captured setup
    captured = temp_db.save_captured_setup(
        params={"front_wing": 32, "rear_wing": 28},
        car_class=0,
        team_id=1,
        track_id=7,
        conditions="dry",
        session_type="race",
    )
    captured_id = captured["id"]
    assert captured_id is not None

    captured = temp_db.get_setup(captured_id)
    assert captured["source"] == "captured"

    # Direct update without allow_fork -> raises ValueError
    with pytest.raises(ValueError) as exc:
        temp_db.update_setup(captured_id, {"name": "Modified", "params": {"front_wing": 34}}, allow_fork=False)
    assert "Captured setups are read-only" in str(exc.value)

    # Update with allow_fork=True -> forks into a new imported copy
    forked = temp_db.update_setup(
        captured_id,
        {
            "name": "Forked Setup",
            "params": {"front_wing": 34, "rear_wing": 28},
            "source_note": "Forked from captured telemetry",
        },
        allow_fork=True,
    )
    assert forked["id"] != captured_id
    assert forked["source"] == "imported"
    assert forked["name"] == "Forked Setup"

    # Original captured setup remains intact
    orig = temp_db.get_setup(captured_id)
    assert orig["source"] == "captured"
    assert orig["params"]["front_wing"] == 32

    # Delete on captured setup -> raises ValueError
    with pytest.raises(ValueError) as del_exc:
        temp_db.delete_setup(captured_id)
    assert "cannot be deleted" in str(del_exc.value)

    # Delete on imported copy -> succeeds
    assert temp_db.delete_setup(forked["id"]) is True
    assert temp_db.get_setup(forked["id"]) is None


# ==============================================================================
# 5. Atomic Track Note Save and Backup
# ==============================================================================
def test_atomic_track_note_save_and_backup(temp_knowledge):
    """Saving track notes writes atomically, keeps .bak copy, and updates memory immediately."""
    kb, tracks_dir = temp_knowledge
    silverstone_file = tracks_dir / "silverstone.md"
    bak_file = tracks_dir / "silverstone.md.bak"

    # Initial state
    assert silverstone_file.exists()
    assert not bak_file.exists()
    initial_content = silverstone_file.read_text(encoding="utf-8")

    # Save new content
    updated_content = """# Silverstone Circuit

## Corners
1. Abbey (T1): High speed right turn.
2. Copse (T9): Flat out 290 km/h entry.
3. Stowe (T15): Hard braking right-hander into Vale.

## Overtaking
DRS into Brooklands and Stowe.

## Tyres
Heavy lateral load on left side.

## Setup notes
Slightly stiffer front roll bar.
"""
    saved_res = kb.save_track_markdown("silverstone", updated_content)
    assert saved_res["slug"] == "silverstone"
    assert saved_res["exists"] is True

    # 1. New file content is updated
    assert silverstone_file.read_text(encoding="utf-8") == updated_content

    # 2. Backup file is created with initial content
    assert bak_file.exists()
    assert bak_file.read_text(encoding="utf-8") == initial_content

    # 3. No leftover .tmp files
    tmp_files = list(tracks_dir.glob("*.tmp"))
    assert len(tmp_files) == 0

    # 4. In-memory reload verified: STT vocabulary & corner names updated immediately
    corner_names = kb.get_corner_names()
    assert "Abbey" in corner_names
    assert "Copse" in corner_names
    assert "Stowe" in corner_names
    assert "The Loop" not in corner_names  # removed in update


# ==============================================================================
# 6. API Endpoints and Authentication
# ==============================================================================
def test_api_endpoints_and_auth(temp_db, temp_knowledge):
    """API endpoints under /api must require dashboard token and handle setup and track operations."""
    kb, _ = temp_knowledge
    token = "test_auth_token_secret"

    state = RaceState(ptt_action=1)
    broadcaster = Broadcaster()
    app = create_app(state, broadcaster, token, setups_storage=temp_db, knowledge=kb)
    client = TestClient(app)

    # --- Auth Tests ---
    assert client.get("/api/setups/schema").status_code == 403
    assert client.get("/api/setups").status_code == 403
    assert client.post("/api/setups", json={}).status_code == 403
    assert client.get("/api/tracks").status_code == 403
    assert client.get("/api/tracks/silverstone").status_code == 403
    assert client.put("/api/tracks/silverstone", json={}).status_code == 403

    # --- GET /api/setups/schema ---
    resp = client.get(f"/api/setups/schema?token={token}")
    assert resp.status_code == 200
    schema_data = resp.json()
    assert "groups" in schema_data
    assert "parameters" in schema_data

    # --- POST /api/setups ---
    payload = {
        "name": "API Test Setup",
        "track_id": 7,
        "car_class": 0,
        "conditions": "dry",
        "session_type": "race",
        "team_id": 1,
        "source": "imported",
        "source_note": "Via API",
        "params": {"front_wing": 36, "rear_wing": 31, "brake_bias": 56},
    }
    resp = client.post(f"/api/setups?token={token}", json=payload)
    assert resp.status_code == 201
    created = resp.json()
    setup_id = created["id"]
    assert created["name"] == "API Test Setup"
    assert created["fingerprint"] is not None

    # Duplicate detection via API
    resp_dup = client.post(f"/api/setups?token={token}", json=payload)
    assert resp_dup.status_code == 409
    dup_body = resp_dup.json()
    assert dup_body["error"] == "duplicate_setup"
    assert "existing_setup" in dup_body

    # Forced duplicate via API
    payload_forced = dict(payload)
    payload_forced["force"] = True
    payload_forced["name"] = "Forced Duplicate"
    resp_force = client.post(f"/api/setups?token={token}", json=payload_forced)
    assert resp_force.status_code == 201
    assert resp_force.json()["id"] == setup_id
    assert resp_force.json()["name"] == "Forced Duplicate"

    # --- PUT /api/setups/{id} ---
    update_payload = {
        "name": "Updated API Setup",
        "params": {"front_wing": 37, "rear_wing": 31},
    }
    resp_put = client.put(f"/api/setups/{setup_id}?token={token}", json=update_payload)
    assert resp_put.status_code == 200
    assert resp_put.json()["name"] == "Updated API Setup"

    # --- GET /api/tracks and PUT /api/tracks/{track} ---
    resp_tracks = client.get(f"/api/tracks?token={token}")
    assert resp_tracks.status_code == 200
    tracks_list = resp_tracks.json()["tracks"]
    assert any(t["slug"] == "silverstone" for t in tracks_list)

    # Get single track notes
    resp_track = client.get(f"/api/tracks/silverstone?token={token}")
    assert resp_track.status_code == 200
    track_data = resp_track.json()
    assert track_data["slug"] == "silverstone"
    assert "corners_list" in track_data
    assert len(track_data["corners_list"]) > 0

    # Save track notes via API
    track_update_payload = {
        "sections": {
            "overtaking": "DRS on Hangar Straight only.",
            "tyres": "Medium compound recommended.",
            "setup_notes": "Low drag setup.",
        },
        "corners_list": [
            {"number": 1, "name": "Abbey", "description": "Flat out"},
            {"number": 2, "name": "Farm", "description": "Easy flat"},
        ],
    }
    resp_save_track = client.put(f"/api/tracks/silverstone?token={token}", json=track_update_payload)
    assert resp_save_track.status_code == 200
    assert resp_save_track.json()["status"] == "saved"
    assert resp_save_track.json()["corners_count"] == 2

    # --- DELETE /api/setups/{id} ---
    resp_del = client.delete(f"/api/setups/{setup_id}?token={token}")
    assert resp_del.status_code == 200
    assert resp_del.json()["deleted"] is True
