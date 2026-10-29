"""Tests for F1 25 car setup library.

Covers:
- CarSetups packet parsing with synthetic packets
- Fingerprint deduplication
- Setup change detection in RaceState
- Ranking rules with minimum 3-lap median rule and untested marking
- Import validation flagging missing and out-of-range values without guessing
- Free-text extraction via mock LLM provider
- Deterministic setup intent routing
- Plain-text sheet and JSON export formats
"""
import struct
import tempfile
from pathlib import Path
from typing import AsyncIterator
import pytest

from f1telemetry import packets
from f1telemetry.packets import HEADER, NUM_CARS, CAR_SETUP, PacketId, parse
from f1telemetry.state import RaceState
from f1telemetry.deterministic import route
from storage import SessionStorage
from setups import (
    SetupsStorage,
    compute_setup_fingerprint,
    diff_setups,
    export_as_json,
    export_as_sheet,
    generate_csv_template,
    import_from_csv,
    import_from_json,
    parse_free_text_setup,
    validate_setup_params,
)


def _build_synthetic_car_setups(
    player_idx: int = 0,
    front_wing: int = 35,
    rear_wing: int = 30,
    next_front_wing: float = 36.0,
) -> bytes:
    """Builds a valid 1133-byte synthetic CarSetups packet."""
    hdr = HEADER.pack(
        2025,       # packet_format
        25,         # game_year
        1,          # game_major_version
        0,          # game_minor_version
        1,          # packet_version
        PacketId.CAR_SETUPS,  # packet_id = 5
        123456789,  # session_uid
        123.45,     # session_time
        1000,       # frame_identifier
        0,          # overall_frame_identifier
        player_idx, # player_car_index
        255,        # secondary_player_car_index
    )

    car_bytes = []
    for i in range(NUM_CARS):
        if i == player_idx:
            c = CAR_SETUP.pack(
                front_wing, rear_wing, 55, 50,  # frontWing, rearWing, onThrottle, offThrottle
                -2.50, -1.00, 0.05, 0.20,       # frontCamber, rearCamber, frontToe, rearToe
                30, 25, 10, 8, 33, 36, 100, 54, 60,  # suspension, arbs, heights, brakes, eng brake
                23.0, 23.0, 24.5, 24.5,         # tyre pressures
                5,                              # ballast
                45.0,                           # fuelLoad
            )
        else:
            # Blank setups in multiplayer
            c = b"\x00" * CAR_SETUP.size
        car_bytes.append(c)

    trailer = struct.pack("<f", next_front_wing)
    return hdr + b"".join(car_bytes) + trailer


def test_car_setups_parsing_synthetic():
    """Verifies that PacketId.CAR_SETUPS decodes 50-byte per-car structs plus 4-byte trailer."""
    raw = _build_synthetic_car_setups(player_idx=0, front_wing=38, rear_wing=32, next_front_wing=39.0)
    assert len(raw) == HEADER.size + NUM_CARS * 50 + 4  # 1133 bytes

    h, data = parse(raw)
    assert h is not None
    assert h.packet_id == PacketId.CAR_SETUPS
    assert data is not None
    assert len(data) == NUM_CARS

    player_setup = data[0]
    assert player_setup.front_wing == 38
    assert player_setup.rear_wing == 32
    assert player_setup.on_throttle == 55
    assert player_setup.off_throttle == 50
    assert player_setup.front_camber == pytest.approx(-2.50)
    assert player_setup.brake_pressure == 100
    assert player_setup.brake_bias == 54
    assert player_setup.front_left_tyre_pressure == pytest.approx(24.5)
    assert player_setup.ballast == 5
    assert player_setup.fuel_load == pytest.approx(45.0)
    assert player_setup.next_front_wing_value == pytest.approx(39.0)

    # Check that another car has 0 values (blank setup)
    assert data[1].front_wing == 0
    assert data[1].next_front_wing_value == 0.0


def test_fingerprint_dedupe():
    """Identical setup values produce identical fingerprints and deduplicate in database."""
    params_a = {"front_wing": 35, "rear_wing": 30, "brake_bias": 54, "fuel_load": 40.0}
    # Differently ordered keys and float/int variants
    params_b = {"fuel_load": 40.0000, "rear_wing": 30, "front_wing": 35.0, "brake_bias": 54}

    fp_a = compute_setup_fingerprint(params_a)
    fp_b = compute_setup_fingerprint(params_b)
    assert fp_a == fp_b

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "setups_test.db")
        storage = SetupsStorage(db_path)

        s1 = storage.save_captured_setup(params_a, car_class=0, team_id=1, track_id=7, conditions="dry", session_type="race")
        s2 = storage.save_captured_setup(params_b, car_class=0, team_id=1, track_id=7, conditions="dry", session_type="race")

        # Must return the same setup record (deduplicated)
        assert s1["id"] == s2["id"]
        assert s1["fingerprint"] == s2["fingerprint"]

        # Only one row stored in table
        all_setups = storage.list_setups()
        assert len(all_setups) == 1


def test_change_detection_in_race_state():
    """RaceState detects when setup changes via fingerprint and emits setup_change signal."""
    st = RaceState()
    pkt1 = _build_synthetic_car_setups(front_wing=30, rear_wing=25)
    h1, data1 = parse(pkt1)

    sig1 = st.update(h1, data1)
    # First time seeing setup in session -> emits setup_change
    setup_sig1 = [s for s in sig1 if s[0] == "setup_change"]
    assert len(setup_sig1) == 1
    fp1 = st.current_setup_fingerprint
    assert fp1 is not None

    # Receiving same setup again -> no setup_change signal
    sig_dup = st.update(h1, data1)
    setup_sig_dup = [s for s in sig_dup if s[0] == "setup_change"]
    assert len(setup_sig_dup) == 0

    # Receiving modified setup (wings changed to 35/30) -> emits new setup_change
    pkt2 = _build_synthetic_car_setups(front_wing=35, rear_wing=30)
    h2, data2 = parse(pkt2)
    sig2 = st.update(h2, data2)
    setup_sig2 = [s for s in sig2 if s[0] == "setup_change"]
    assert len(setup_sig2) == 1
    fp2 = st.current_setup_fingerprint
    assert fp2 != fp1


def test_setup_ranking_min_lap_rule():
    """Ranking sorts by best lap, requires >= 3 valid laps for median, and flags untested setups."""
    with tempfile.TemporaryDirectory() as tmpdir:
        setups_db = str(Path(tmpdir) / "setups.db")
        sessions_db = str(Path(tmpdir) / "sessions.db")

        set_store = SetupsStorage(setups_db)
        sess_store = SessionStorage(sessions_db)

        # Create 3 setups for Silverstone (track_id=7)
        s1 = set_store.save_captured_setup({"front_wing": 40, "rear_wing": 35}, car_class=0, team_id=1, track_id=7, conditions="dry", session_type="race", name="Consistent Setup")
        s2 = set_store.save_captured_setup({"front_wing": 45, "rear_wing": 40}, car_class=0, team_id=1, track_id=7, conditions="dry", session_type="race", name="One-Lap Wonder")
        s3 = set_store.save_imported_setup({"front_wing": 30, "rear_wing": 25}, car_class=0, track_id=7, conditions="dry", session_type="race", name="Untested Setup")

        # Setup 1 has 4 valid laps: 80.0, 81.0, 82.0, 83.0 -> Best = 80.0, Median = 81.5
        for t in (80.0, 81.0, 82.0, 83.0):
            sess_store.save_completed_lap({
                "session_uid": "sess_1", "game_format": 2025, "formula": 0, "track_id": 7, "session_type": 10,
                "team_id": 1, "lap_num": 1, "lap_time_s": t, "is_valid": True, "setup_id": str(s1["id"]), "created_at": 100.0,
            })

        # Setup 2 has only 1 valid lap: 78.0 (faster, but < 3 laps so median is None)
        sess_store.save_completed_lap({
            "session_uid": "sess_1", "game_format": 2025, "formula": 0, "track_id": 7, "session_type": 10,
            "team_id": 1, "lap_num": 2, "lap_time_s": 78.0, "is_valid": True, "setup_id": str(s2["id"]), "created_at": 101.0,
        })

        # Let the writer thread flush to SQLite
        import time; time.sleep(0.1)

        ranked = set_store.get_ranked_setups(sess_store, track_id=7, car_class=0, conditions="dry", session_type="race")
        assert len(ranked) == 3

        # Setup 2 is ranked #1 by best lap (78.0s), but median is None (< 3 laps)
        assert ranked[0]["id"] == s2["id"]
        assert ranked[0]["best_lap_s"] == 78.0
        assert ranked[0]["median_lap_s"] is None
        assert ranked[0]["valid_laps_count"] == 1
        assert not ranked[0]["untested"]

        # Setup 1 is ranked #2 (best lap 80.0s), median is computed (81.5s because 4 laps >= 3)
        assert ranked[1]["id"] == s1["id"]
        assert ranked[1]["best_lap_s"] == 80.0
        assert ranked[1]["median_lap_s"] == 81.5
        assert ranked[1]["valid_laps_count"] == 4
        assert not ranked[1]["untested"]

        # Setup 3 has no laps yet -> marked as untested and at bottom
        assert ranked[2]["id"] == s3["id"]
        assert ranked[2]["best_lap_s"] is None
        assert ranked[2]["median_lap_s"] is None
        assert ranked[2]["valid_laps_count"] == 0
        assert ranked[2]["untested"] is True


def test_import_validation_out_of_range_and_missing():
    """Validation checks ranges and flags missing parameters without guessing values."""
    ranges = {
        "front_wing": {"min": 10, "max": 50, "step": 1},
        "rear_wing": {"min": 10, "max": 50, "step": 1},
        "brake_bias": {"min": 50, "max": 70, "step": 1},
    }

    # front_wing is 55 (> 50 max)
    # rear_wing is 30 (valid)
    # brake_bias is missing
    partial_input = {"front_wing": 55, "rear_wing": 30}

    val = validate_setup_params(partial_input, car_class=0, ranges=ranges)
    assert val["valid"] is False
    assert "front_wing" in val["out_of_range"]
    assert val["out_of_range"]["front_wing"]["value"] == 55
    assert val["out_of_range"]["front_wing"]["max"] == 50

    # Missing parameters must be listed
    assert "brake_bias" in val["missing"]
    assert "on_throttle" in val["missing"]

    # Critical requirement: Missing values must NEVER be invented!
    assert "brake_bias" not in val["params"]
    assert "front_suspension" not in val["params"]
    assert val["params"] == {"front_wing": 55, "rear_wing": 30}


class MockLLMProvider:
    """Mock LLM provider returning strictly formatted JSON for setup extraction."""
    def __init__(self, json_reply: str):
        self.json_reply = json_reply
        self.last_system = ""
        self.last_user = ""

    async def complete(self, system: str, user: str) -> str:
        self.last_system = system
        self.last_user = user
        return self.json_reply


@pytest.mark.anyio
async def test_llm_free_text_import():
    """Extracts setup parameters from free text using strict JSON prompt without guessing."""
    mock_response = '{\n  "front_wing": 38,\n  "rear_wing": 32,\n  "on_throttle": 55,\n  "brake_bias": 54\n}'
    mock_llm = MockLLMProvider(mock_response)

    free_text = "Here is my Silverstone setup: wings are 38 and 32, diff 55% on throttle, 54% brake bias."
    parsed = await parse_free_text_setup(free_text, mock_llm)

    # Verify system prompt instructs strict JSON and no guessing
    assert "DO NOT invent" in mock_llm.last_system
    assert "OMIT IT" in mock_llm.last_system

    # Verify extracted values
    assert parsed["front_wing"] == 38
    assert parsed["rear_wing"] == 32
    assert parsed["on_throttle"] == 55
    assert parsed["brake_bias"] == 54

    # Ensure unmentioned values (e.g. suspension, camber) are NOT invented
    assert "front_suspension" not in parsed
    assert "front_camber" not in parsed


def test_deterministic_setup_intent():
    """Deterministic setup intent answers 'what setup am I on' and 'best setup here'."""
    st = RaceState()
    pkt = _build_synthetic_car_setups(front_wing=36, rear_wing=28)
    h, data = parse(pkt)
    st.update(h, data)

    # 1. Driver asks for current setup
    reply = route("what setup am I on", st)
    assert reply.kind == "answer"
    assert reply.intent == "setup"
    assert "36 front wing" in reply.text
    assert "28 rear wing" in reply.text

    reply2 = route("what is my setup", st)
    assert reply2.kind == "answer"
    assert reply2.intent == "setup"

    # 2. Driver asks for best setup
    st.best_setup_info = {
        "name": "Silverstone Quali Peak",
        "best_lap_s": 86.42,
        "params": {"front_wing": 40, "rear_wing": 34},
    }
    reply_best = route("best setup here", st)
    assert reply_best.kind == "answer"
    assert reply_best.intent == "setup"
    assert "Silverstone Quali Peak" in reply_best.text
    assert "40 front" in reply_best.text

    # 3. Open-ended handling question routes to AI agent, NOT deterministic
    reply_ai = route("my car is understeering in high speed turns, what should I change?", st)
    assert reply_ai.kind == "ai"


def test_export_formats_and_diff():
    """Plain-text sheet and JSON exports match format specifications, and diff compares setups."""
    setup_a = {
        "id": 1,
        "name": "High Downforce",
        "fingerprint": "abc12345",
        "params": {"front_wing": 40, "rear_wing": 35, "brake_bias": 55},
    }
    setup_b = {
        "id": 2,
        "name": "Low Drag",
        "fingerprint": "def67890",
        "params": {"front_wing": 30, "rear_wing": 25, "brake_bias": 55},
    }

    # Diff
    diff = diff_setups(setup_a, setup_b)
    assert diff["changed_count"] == 2  # front_wing (-10) and rear_wing (-10)
    assert diff["diffs"]["front_wing"]["delta"] == -10
    assert diff["diffs"]["rear_wing"]["delta"] == -10
    assert diff["diffs"]["brake_bias"]["changed"] is False

    # Sheet export
    sheet = export_as_sheet(setup_a)
    assert "AERODYNAMICS" in sheet
    assert "Front Wing Aero:                 40" in sheet
    assert "SUSPENSION" in sheet

    # JSON export
    exported_json = export_as_json(setup_a)
    assert '"front_wing": 40' in exported_json
