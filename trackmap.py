"""Track outline sampling, SQLite persistence, and coordinate scaling for live track map."""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from f1telemetry.state import RUNNING, RaceState

log = logging.getLogger("engineer.trackmap")

DEFAULT_DB_PATH = Path(__file__).parent / "tracks.db"


@dataclass
class TrackOutline:
    track_id: int
    track_length_m: float
    points: list[list[float]]          # [[x, z, lap_distance, sector], ...]
    sectors: dict[str, list[float]]     # {"s1": [x, z], "s2": [x, z], "s3": [x, z]}
    finish_line: list[float]           # [x, z]
    created_at: float = 0.0

    @property
    def bounds(self) -> dict[str, float]:
        if not self.points:
            return {"min_x": 0.0, "max_x": 0.0, "min_z": 0.0, "max_z": 0.0}
        xs = [p[0] for p in self.points]
        zs = [p[1] for p in self.points]
        return {
            "min_x": min(xs),
            "max_x": max(xs),
            "min_z": min(zs),
            "max_z": max(zs),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "track_id": self.track_id,
            "track_length_m": self.track_length_m,
            "points": self.points,
            "sectors": self.sectors,
            "finish_line": self.finish_line,
            "bounds": self.bounds,
            "created_at": self.created_at,
        }


class TrackMapStorage:
    """Manages SQLite storage for track outlines keyed by track_id."""
    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH):
        self.db_path = Path(db_path)
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._get_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS track_outlines (
                    track_id INTEGER PRIMARY KEY,
                    track_length_m REAL,
                    points_json TEXT,
                    sectors_json TEXT,
                    finish_line_json TEXT,
                    created_at REAL
                )
            """)
            conn.commit()

    def load(self, track_id: int) -> TrackOutline | None:
        with self._get_conn() as conn:
            cur = conn.execute(
                "SELECT * FROM track_outlines WHERE track_id = ?", (track_id,)
            )
            row = cur.fetchone()
            if not row:
                return None
            return TrackOutline(
                track_id=row["track_id"],
                track_length_m=row["track_length_m"],
                points=json.loads(row["points_json"]),
                sectors=json.loads(row["sectors_json"]),
                finish_line=json.loads(row["finish_line_json"]),
                created_at=row["created_at"],
            )

    def save(self, outline: TrackOutline):
        with self._get_conn() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO track_outlines
                (track_id, track_length_m, points_json, sectors_json, finish_line_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                outline.track_id,
                outline.track_length_m,
                json.dumps(outline.points),
                json.dumps(outline.sectors),
                json.dumps(outline.finish_line),
                outline.created_at or time.time(),
            ))
            conn.commit()

    def delete(self, track_id: int):
        with self._get_conn() as conn:
            conn.execute("DELETE FROM track_outlines WHERE track_id = ?", (track_id,))
            conn.commit()


def scale_coordinates(
    x: float,
    z: float,
    bounds: dict[str, float] | tuple[float, float, float, float],
    canvas_width: float,
    canvas_height: float,
    padding: float = 20.0,
) -> tuple[float, float]:
    """Transforms world (x, z) coordinates into 2D canvas coordinates (canvas_x, canvas_y).

    Preserves aspect ratio and centers the content within the canvas dimensions.
    """
    if isinstance(bounds, dict):
        min_x = bounds["min_x"]
        max_x = bounds["max_x"]
        min_z = bounds["min_z"]
        max_z = bounds["max_z"]
    else:
        min_x, max_x, min_z, max_z = bounds

    span_x = max(max_x - min_x, 1.0)
    span_z = max(max_z - min_z, 1.0)

    usable_w = max(canvas_width - 2 * padding, 1.0)
    usable_h = max(canvas_height - 2 * padding, 1.0)

    scale = min(usable_w / span_x, usable_h / span_z)

    content_w = span_x * scale
    content_h = span_z * scale
    offset_x = padding + (usable_w - content_w) / 2.0
    offset_y = padding + (usable_h - content_h) / 2.0

    canvas_x = offset_x + (x - min_x) * scale
    canvas_y = offset_y + (z - min_z) * scale
    return canvas_x, canvas_y


def get_points_bounds(points: list[tuple[float, float]] | list[list[float]]) -> dict[str, float]:
    """Computes bounding box dict from an array of [x, z] points."""
    if not points:
        return {"min_x": -100.0, "max_x": 100.0, "min_z": -100.0, "max_z": 100.0}
    xs = [p[0] for p in points]
    zs = [p[1] for p in points]
    return {
        "min_x": min(xs),
        "max_x": max(xs),
        "min_z": min(zs),
        "max_z": max(zs),
    }


class TrackMapManager:
    """Handles track outline acquisition, caching, and state extraction."""
    def __init__(self, storage: TrackMapStorage | None = None, sample_interval_m: float = 5.0):
        self.storage = storage or TrackMapStorage()
        self.sample_interval_m = sample_interval_m
        self.current_track_id: int | None = None
        self.current_outline: TrackOutline | None = None

        # Clean lap sampling buffer
        self._sampling_lap_num: int | None = None
        self._sampling_points: list[list[float]] = []
        self._s2_point: list[float] | None = None
        self._s3_point: list[float] | None = None
        self._finish_point: list[float] | None = None
        self._last_sampled_dist: float = -999.0
        self._lap_was_invalid: bool = False

    def set_track(self, track_id: int):
        """Loads cached outline for track_id if available; resets sampler if changed."""
        if self.current_track_id == track_id and self.current_outline is not None:
            return
        self.current_track_id = track_id
        self.current_outline = self.storage.load(track_id)
        if self.current_outline:
            log.info("Loaded track outline for track %d (%d points)", track_id, len(self.current_outline.points))
        else:
            log.info("No saved outline for track %d; will sample on next clean lap", track_id)
            self._reset_sampler()

    def _reset_sampler(self):
        self._sampling_lap_num = None
        self._sampling_points = []
        self._s2_point = None
        self._s3_point = None
        self._finish_point = None
        self._last_sampled_dist = -999.0
        self._lap_was_invalid = False

    def rebuild(self, track_id: int | None = None):
        """Purges saved outline and starts fresh sampling for the specified or current track."""
        tid = track_id if track_id is not None else self.current_track_id
        if tid is not None:
            self.storage.delete(tid)
        self.current_outline = None
        self._reset_sampler()
        log.info("Track outline rebuild triggered for track %s", tid)

    def sample_step(
        self,
        lap_num: int,
        lap_distance: float,
        sector: int,
        lap_invalid: bool,
        pit_status: int,
        x: float,
        z: float,
        track_length_m: float,
    ) -> TrackOutline | None:
        """Processes one telemetry frame for outline sampling. Returns TrackOutline when finalized."""
        if self.current_outline is not None:
            return self.current_outline

        pitting = (pit_status != 0)

        # Lap rollover or new lap start
        if self._sampling_lap_num is None or lap_num != self._sampling_lap_num:
            if self._sampling_lap_num is not None and not self._lap_was_invalid:
                # Validate coverage: lap must have enough points and reach near the end of track
                min_coverage = track_length_m * 0.75 if track_length_m > 0 else 1000.0
                if len(self._sampling_points) >= 30 and self._last_sampled_dist >= min_coverage:
                    finish = self._finish_point or self._sampling_points[0][:2]
                    sectors = {
                        "s1": finish,
                        "s2": self._s2_point or self._sampling_points[len(self._sampling_points) // 3][:2],
                        "s3": self._s3_point or self._sampling_points[(2 * len(self._sampling_points)) // 3][:2],
                    }
                    outline = TrackOutline(
                        track_id=self.current_track_id if self.current_track_id is not None else 0,
                        track_length_m=track_length_m,
                        points=self._sampling_points,
                        sectors=sectors,
                        finish_line=finish,
                        created_at=time.time(),
                    )
                    if self.current_track_id is not None:
                        self.storage.save(outline)
                    self.current_outline = outline
                    self._reset_sampler()
                    return outline

            # Reset sampling for new lap
            self._sampling_lap_num = lap_num
            self._sampling_points = []
            self._s2_point = None
            self._s3_point = None
            self._finish_point = None
            self._last_sampled_dist = -999.0
            self._lap_was_invalid = lap_invalid or pitting

        # Mark invalid if corner cut or in pit
        if lap_invalid or pitting:
            self._lap_was_invalid = True

        if not self._lap_was_invalid and lap_distance >= 0:
            if lap_distance - self._last_sampled_dist >= self.sample_interval_m or self._last_sampled_dist < 0:
                pt = [round(x, 2), round(z, 2), round(lap_distance, 1), sector]
                self._sampling_points.append(pt)
                self._last_sampled_dist = lap_distance

                if self._finish_point is None and lap_distance < 25.0:
                    self._finish_point = [round(x, 2), round(z, 2)]
                if sector == 1 and self._s2_point is None:
                    self._s2_point = [round(x, 2), round(z, 2)]
                if sector == 2 and self._s3_point is None:
                    self._s3_point = [round(x, 2), round(z, 2)]

        return None

    def update_from_state(self, state: RaceState):
        """Pulls current telemetry and runs sampling step."""
        if state.session and state.session.track_id is not None:
            if self.current_track_id != state.session.track_id:
                self.set_track(state.session.track_id)

        if self.current_outline is not None:
            return

        me = state.me
        motion = state.my_motion
        if me is None or motion is None:
            return

        track_len = state.session.track_length_m if state.session else 5000.0
        self.sample_step(
            lap_num=me.lap_num,
            lap_distance=me.lap_distance,
            sector=me.sector,
            lap_invalid=me.lap_invalid,
            pit_status=me.pit_status,
            x=motion.x,
            z=motion.z,
            track_length_m=track_len,
        )

    def extract_car_positions(self, state: RaceState) -> list[dict[str, Any]]:
        """Extracts lightweight car positioning for track map rendering."""
        if not state.motion or not state.lap:
            return []

        me = state.me
        ahead_idx = state.car_in_position(me.position - 1) if (me and me.position > 1) else None
        behind_idx = state.car_in_position(me.position + 1) if me else None

        positions = []
        for i, m in enumerate(state.motion):
            if i >= len(state.lap):
                break
            c = state.lap[i]
            if c.result_status in RUNNING and c.position > 0:
                positions.append({
                    "idx": i,
                    "x": round(m.x, 1),
                    "z": round(m.z, 1),
                    "yaw": round(m.yaw, 2),
                    "position": c.position,
                    "is_player": (i == state.player),
                    "is_ahead": (i == ahead_idx),
                    "is_behind": (i == behind_idx),
                    "pitting": (c.pit_status != 0),
                })
        return positions
