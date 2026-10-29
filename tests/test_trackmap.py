"""Tests for Motion packet parsing, track outline sampling, SQLite persistence, and coordinate scaling."""
import math
import struct
from pathlib import Path

import pytest

from f1telemetry import packets
from f1telemetry.packets import HEADER, MOTION, NUM_CARS, PacketId
from f1telemetry.state import RaceState
from test_synthetic import motion_packet
from trackmap import (
    TrackMapManager,
    TrackMapStorage,
    TrackOutline,
    get_points_bounds,
    scale_coordinates,
)


def test_motion_packet_parsing():
    """Verify PacketId.MOTION = 0, 60-byte stride, and field decoding."""
    assert PacketId.MOTION == 0

    cars = {
        0: (123.45, 678.90, 1.5708, 2.45, -3.12, 0.98),
        1: (-50.0, 100.0, 0.0, 0.5, 0.0, 1.0),
    }
    raw = motion_packet(cars, trailer=120)

    h, parsed = packets.parse(raw)
    assert h is not None
    assert h.packet_id == PacketId.MOTION
    assert parsed is not None
    assert len(parsed) == NUM_CARS

    # Check car 0 (Player)
    m0 = parsed[0]
    assert math.isclose(m0.x, 123.45, abs_tol=1e-2)
    assert math.isclose(m0.z, 678.90, abs_tol=1e-2)
    assert math.isclose(m0.yaw, 1.5708, abs_tol=1e-3)
    assert math.isclose(m0.g_lat, 2.45, abs_tol=1e-2)
    assert math.isclose(m0.g_lon, -3.12, abs_tol=1e-2)
    assert math.isclose(m0.g_vert, 0.98, abs_tol=1e-2)

    # Check car 1
    m1 = parsed[1]
    assert math.isclose(m1.x, -50.0, abs_tol=1e-2)
    assert math.isclose(m1.z, 100.0, abs_tol=1e-2)

    # Test packet without trailer (e.g. legacy/testing)
    raw_no_trailer = motion_packet(cars, trailer=0)
    h2, parsed2 = packets.parse(raw_no_trailer)
    assert parsed2 is not None
    assert math.isclose(parsed2[0].x, 123.45, abs_tol=1e-2)

    # Test stride check skips truncated packets
    short_raw = raw[:HEADER.size + 10]
    _, short_parsed = packets.parse(short_raw)
    assert short_parsed is None


def test_race_state_stores_motion():
    """RaceState must store motion for all cars and expose my_motion."""
    state = RaceState(ptt_action=1)
    raw = motion_packet({0: (10.0, 20.0, 0.5, 1.0, 2.0, 3.0)})
    h, data = packets.parse(raw)
    state.update(h, data)

    assert state.motion is not None
    assert len(state.motion) == NUM_CARS
    assert state.my_motion is not None
    assert math.isclose(state.my_motion.x, 10.0, abs_tol=1e-2)
    assert math.isclose(state.my_motion.z, 20.0, abs_tol=1e-2)


def test_track_outline_sampling_clean_lap(tmp_path: Path):
    """Outline must be sampled every ~5m during a clean lap and persisted to SQLite."""
    db_file = tmp_path / "test_tracks.db"
    storage = TrackMapStorage(db_file)
    manager = TrackMapManager(storage=storage, sample_interval_m=5.0)
    manager.set_track(7) # Silverstone

    track_len = 5000.0
    # Simulate clean lap 2 from distance 0 to 5000m
    d = 0.0
    while d <= track_len:
        # circle path
        angle = (d / track_len) * 2 * math.pi
        x = 500.0 * math.cos(angle)
        z = 500.0 * math.sin(angle)
        sec = 0 if d < 1800 else (1 if d < 3600 else 2)

        manager.sample_step(
            lap_num=2,
            lap_distance=d,
            sector=sec,
            lap_invalid=False,
            pit_status=0,
            x=x,
            z=z,
            track_length_m=track_len,
        )
        d += 2.0 # 2m increments (finer than 5m sampling)

    # Complete the lap by advancing lap_num to 3
    outline = manager.sample_step(
        lap_num=3,
        lap_distance=10.0,
        sector=0,
        lap_invalid=False,
        pit_status=0,
        x=500.0,
        z=0.0,
        track_length_m=track_len,
    )

    assert outline is not None
    assert outline.track_id == 7
    assert len(outline.points) >= 50
    # Verify samples are approximately 5m apart
    dists = [p[2] for p in outline.points]
    diffs = [b - a for a, b in zip(dists, dists[1:])]
    assert all(d >= 4.5 for d in diffs)

    # Verify sector boundaries and finish line
    assert "s1" in outline.sectors
    assert "s2" in outline.sectors
    assert "s3" in outline.sectors
    assert len(outline.finish_line) == 2

    # Verify SQLite persistence
    loaded = storage.load(7)
    assert loaded is not None
    assert loaded.track_id == 7
    assert len(loaded.points) == len(outline.points)


def test_track_outline_sampling_invalid_lap_rejected(tmp_path: Path):
    """An invalid lap or pitting lap must not be saved to SQLite."""
    db_file = tmp_path / "test_tracks_invalid.db"
    storage = TrackMapStorage(db_file)
    manager = TrackMapManager(storage=storage, sample_interval_m=5.0)
    manager.set_track(7)

    track_len = 5000.0
    d = 0.0
    while d <= track_len:
        # Car cuts a corner at d = 2500m
        invalid = (d >= 2500)
        manager.sample_step(
            lap_num=1,
            lap_distance=d,
            sector=1,
            lap_invalid=invalid,
            pit_status=0,
            x=d,
            z=d,
            track_length_m=track_len,
        )
        d += 2.5

    # Trigger lap rollover
    res = manager.sample_step(
        lap_num=2,
        lap_distance=0.0,
        sector=0,
        lap_invalid=False,
        pit_status=0,
        x=0.0,
        z=0.0,
        track_length_m=track_len,
    )
    assert res is None
    assert storage.load(7) is None


def test_track_outline_rebuild(tmp_path: Path):
    """Rebuild must purge SQLite outline and reset sampler state."""
    db_file = tmp_path / "test_tracks_rebuild.db"
    storage = TrackMapStorage(db_file)
    manager = TrackMapManager(storage=storage, sample_interval_m=5.0)

    # Save a mock outline
    outline = TrackOutline(
        track_id=10,
        track_length_m=7000.0,
        points=[[0.0, 0.0, 0.0, 0], [10.0, 10.0, 10.0, 0]],
        sectors={"s1": [0.0, 0.0], "s2": [10.0, 10.0], "s3": [20.0, 20.0]},
        finish_line=[0.0, 0.0],
    )
    storage.save(outline)
    manager.set_track(10)
    assert manager.current_outline is not None

    # Rebuild
    manager.rebuild(10)
    assert manager.current_outline is None
    assert storage.load(10) is None


def test_coordinate_scaling():
    """Coordinate scaling must preserve aspect ratio, respect margins, and center content."""
    bounds = {"min_x": 0.0, "max_x": 200.0, "min_z": 0.0, "max_z": 100.0}
    canvas_w, canvas_h = 400.0, 300.0
    padding = 20.0

    # Test center point (100, 50) maps to canvas center (200, 150)
    cx, cy = scale_coordinates(100.0, 50.0, bounds, canvas_w, canvas_h, padding=padding)
    assert math.isclose(cx, 200.0, abs_tol=1e-1)
    assert math.isclose(cy, 150.0, abs_tol=1e-1)

    # Test bounds calculation helper
    pts = [[10.0, -20.0], [50.0, 80.0], [-30.0, 15.0]]
    b = get_points_bounds(pts)
    assert b["min_x"] == -30.0
    assert b["max_x"] == 50.0
    assert b["min_z"] == -20.0
    assert b["max_z"] == 80.0
