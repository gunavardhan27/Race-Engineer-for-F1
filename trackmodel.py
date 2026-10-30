"""Track model generated automatically from the entire field's telemetry (Part A of Prompt 21).

Samples all active cars across 2m bins, computes smoothed centreline, approximate used width
(2nd and 98th percentiles), identifies corners with curvature and min speed points, estimates
braking zones from all cars' brake inputs from lap 1, persists per track_id and layout in SQLite,
and preserves corner names across rebuilds.
"""
from __future__ import annotations

import json
import logging
import math
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

import numpy as np

log = logging.getLogger("engineer.trackmodel")
DEFAULT_DB_PATH = Path(__file__).parent / "tracks.db"


@dataclass
class Corner:
    number: int                      # 1, 2, ...
    name: str                        # e.g. "Turn 1", "Stowe", "Copse"
    start_dist_m: float
    apex_dist_m: float
    end_dist_m: float
    direction: str                   # "left" or "right"
    field_median_min_speed_kph: float
    braking_start_median_m: float
    braking_start_p10_m: float
    braking_start_p90_m: float
    median_entry_speed_kph: float

    @property
    def length_m(self) -> float:
        if self.end_dist_m >= self.start_dist_m:
            return self.end_dist_m - self.start_dist_m
        return self.end_dist_m + 100000.0 - self.start_dist_m  # wrap fallback


@dataclass
class BinData:
    bin_index: int
    lap_distance_m: float
    x: float
    y: float
    z: float
    heading: float                   # Tangent heading in radians
    curvature: float                 # Radians per metre
    left_edge_m: float               # 2nd percentile lateral offset (typically <= 0)
    right_edge_m: float              # 98th percentile lateral offset (typically >= 0)
    median_speed_kph: float
    lap_sample_count: int = 0        # Distinct laps with samples in this bin


@dataclass
class TrackModel:
    track_id: int
    track_length_m: float
    version: int = 1
    bins: list[BinData] = field(default_factory=list)
    corners: list[Corner] = field(default_factory=list)
    is_ready: bool = False
    coverage_pct: float = 0.0
    created_at: float = 0.0
    updated_at: float = 0.0

    def get_bin(self, distance_m: float) -> Optional[BinData]:
        if not self.bins or self.track_length_m <= 0:
            return None
        d = distance_m % self.track_length_m
        idx = int(d // 2.0)
        if 0 <= idx < len(self.bins):
            return self.bins[idx]
        return self.bins[0] if self.bins else None

    def get_corner_at_dist(self, distance_m: float) -> Optional[Corner]:
        if not self.corners or self.track_length_m <= 0:
            return None
        d = distance_m % self.track_length_m
        for c in self.corners:
            if c.start_dist_m <= c.end_dist_m:
                if c.start_dist_m <= d <= c.end_dist_m:
                    return c
            else:  # Corner wraps around finish line
                if d >= c.start_dist_m or d <= c.end_dist_m:
                    return c
        return None

    def get_upcoming_corner(self, distance_m: float, lookahead_m: float = 350.0) -> tuple[Optional[Corner], float]:
        """Returns (corner, distance_to_braking_or_start) for the next corner within lookahead_m."""
        if not self.corners or self.track_length_m <= 0:
            return None, 999.0
        d = distance_m % self.track_length_m
        best_c, best_gap = None, 999.0
        for c in self.corners:
            target_m = c.braking_start_median_m if c.braking_start_median_m > 0 else c.start_dist_m
            gap = target_m - d
            if gap < -50.0:  # already well past
                gap += self.track_length_m
            if 0 <= gap <= lookahead_m:
                if gap < best_gap:
                    best_gap = gap
                    best_c = c
        return best_c, best_gap

    def get_lateral_offset(self, x: float, z: float, dist_m: float) -> float:
        """Computes lateral offset (in metres) from centreline. Positive = right, Negative = left."""
        b = self.get_bin(dist_m)
        if b is None:
            return 0.0
        # Tangent vector from heading: (sin(h), cos(h))
        # Normal (right) vector: (cos(h), -sin(h))
        nx = math.cos(b.heading)
        nz = -math.sin(b.heading)
        dx = x - b.x
        dz = z - b.z
        return dx * nx + dz * nz

    def is_inside_corner(self, lateral_offset: float, direction: str) -> bool:
        """Returns True if the lateral offset is toward the inside of the corner."""
        if direction == "right":
            return lateral_offset > 0.5
        elif direction == "left":
            return lateral_offset < -0.5
        return False

    def to_dict(self) -> dict[str, Any]:
        return {
            "track_id": self.track_id,
            "track_length_m": self.track_length_m,
            "version": self.version,
            "is_ready": self.is_ready,
            "coverage_pct": self.coverage_pct,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "corners": [asdict(c) for c in self.corners],
            "bins": [asdict(b) for b in self.bins],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TrackModel:
        bins = [BinData(**b) for b in d.get("bins", [])]
        corners = [Corner(**c) for c in d.get("corners", [])]
        return cls(
            track_id=d.get("track_id", 0),
            track_length_m=d.get("track_length_m", 0.0),
            version=d.get("version", 1),
            bins=bins,
            corners=corners,
            is_ready=d.get("is_ready", False),
            coverage_pct=d.get("coverage_pct", 0.0),
            created_at=d.get("created_at", 0.0),
            updated_at=d.get("updated_at", 0.0),
        )


class TrackModelBuilder:
    """Collects multi-car telemetry frames and builds a unified TrackModel."""

    def __init__(
        self,
        track_id: int,
        track_length_m: float,
        curvature_threshold: float = 0.005,
        knowledge_base: Any | None = None,
    ):
        self.track_id = track_id
        self.track_length_m = max(track_length_m, 100.0)
        self.curvature_threshold = curvature_threshold
        self.knowledge = knowledge_base

        self.bin_size = 2.0
        self.num_bins = max(1, int(math.ceil(self.track_length_m / self.bin_size)))

        # Per-bin data accumulation
        self._xs: list[list[float]] = [[] for _ in range(self.num_bins)]
        self._ys: list[list[float]] = [[] for _ in range(self.num_bins)]
        self._zs: list[list[float]] = [[] for _ in range(self.num_bins)]
        self._speeds: list[list[float]] = [[] for _ in range(self.num_bins)]
        self._brakes: list[list[float]] = [[] for _ in range(self.num_bins)]
        self._throttles: list[list[float]] = [[] for _ in range(self.num_bins)]
        self._steers: list[list[float]] = [[] for _ in range(self.num_bins)]
        self._laps_seen: list[set[tuple[int, int]]] = [set() for _ in range(self.num_bins)]

        # Tracking brake onset events across all cars: [(lap_dist, speed_kph)]
        self._brake_onsets: list[tuple[float, float]] = []
        self._car_prev_brake: dict[int, float] = {}
        self._car_low_brake_dist: dict[int, float] = {}

        # Active aero zones from Session packet (straights that must not contain corners)
        self.active_aero_zones: list[tuple[float, float]] = []

    def feed_sample(
        self,
        car_idx: int,
        lap_num: int,
        lap_dist: float,
        total_dist: float,
        x: float,
        y: float,
        z: float,
        speed_kph: float,
        brake: float,
        throttle: float,
        steer: float,
        pit_status: int = 0,
        result_status: int = 2,
        safety_car: int = 0,
    ):
        """Feeds one sample for one car."""
        # Filters: pit lane, inactive, grid chaos (first 200m), safety car
        if pit_status != 0 or result_status != 2:
            return
        if total_dist < 200.0:
            return
        if safety_car in (1, 2, 3):
            return

        norm_dist = lap_dist % self.track_length_m
        b_idx = int(norm_dist // self.bin_size) % self.num_bins

        self._xs[b_idx].append(x)
        self._ys[b_idx].append(y)
        self._zs[b_idx].append(z)
        self._speeds[b_idx].append(speed_kph)
        self._brakes[b_idx].append(brake)
        self._throttles[b_idx].append(throttle)
        self._steers[b_idx].append(steer)
        self._laps_seen[b_idx].add((car_idx, lap_num))

        # Brake onset detection: brake > 0.2 after period < 0.05
        prev_b = self._car_prev_brake.get(car_idx, 0.0)
        low_dist = self._car_low_brake_dist.get(car_idx, 0.0)

        if brake < 0.05:
            self._car_low_brake_dist[car_idx] = low_dist + self.bin_size
        else:
            if brake > 0.2 and prev_b <= 0.2 and low_dist >= 15.0:
                self._brake_onsets.append((norm_dist, speed_kph))
            self._car_low_brake_dist[car_idx] = 0.0

        self._car_prev_brake[car_idx] = brake

    def feed_race_state(self, state: Any):
        """Batch ingests current packet state for all cars."""
        if not state or not getattr(state, "lap", None) or not getattr(state, "motion", None):
            return
        laps = state.lap
        motion = state.motion
        tels = getattr(state, "tel", None)
        sess = getattr(state, "session", None)

        sc = getattr(sess, "safety_car", 0) if sess else 0
        if sess and hasattr(sess, "active_aero_zones") and sess.active_aero_zones:
            self.active_aero_zones = list(sess.active_aero_zones)

        n = min(len(laps), len(motion), len(tels) if tels else len(laps))
        for i in range(n):
            l = laps[i]
            m = motion[i]
            t = tels[i] if tels and i < len(tels) else None

            speed = getattr(t, "speed_kph", 0.0) if t else 0.0
            brake = getattr(t, "brake", 0.0) if t else 0.0
            throttle = getattr(t, "throttle", 0.0) if t else 0.0
            steer = getattr(t, "steer", 0.0) if t else 0.0

            self.feed_sample(
                car_idx=i,
                lap_num=getattr(l, "lap_num", 1),
                lap_dist=getattr(l, "lap_distance", 0.0),
                total_dist=getattr(l, "total_distance", 0.0),
                x=getattr(m, "x", 0.0),
                y=getattr(m, "y", 0.0),
                z=getattr(m, "z", 0.0),
                speed_kph=speed,
                brake=brake,
                throttle=throttle,
                steer=steer,
                pit_status=getattr(l, "pit_status", 0),
                result_status=getattr(l, "result_status", 2),
                safety_car=sc,
            )

    def build_model(self) -> TrackModel:
        """Constructs and returns the finalized TrackModel."""
        raw_x = np.zeros(self.num_bins)
        raw_y = np.zeros(self.num_bins)
        raw_z = np.zeros(self.num_bins)
        med_speed = np.zeros(self.num_bins)
        lap_counts = np.zeros(self.num_bins, dtype=int)
        valid_mask = np.zeros(self.num_bins, dtype=bool)

        for i in range(self.num_bins):
            lap_counts[i] = len(self._laps_seen[i])
            if self._xs[i]:
                raw_x[i] = float(np.median(self._xs[i]))
                raw_y[i] = float(np.median(self._ys[i]))
                raw_z[i] = float(np.median(self._zs[i]))
                med_speed[i] = float(np.median(self._speeds[i])) if self._speeds[i] else 100.0
                valid_mask[i] = True

        # Interpolate missing bins if partial coverage
        if not np.all(valid_mask) and np.any(valid_mask):
            valid_indices = np.where(valid_mask)[0]
            all_indices = np.arange(self.num_bins)
            raw_x = np.interp(all_indices, valid_indices, raw_x[valid_indices], period=self.num_bins)
            raw_y = np.interp(all_indices, valid_indices, raw_y[valid_indices], period=self.num_bins)
            raw_z = np.interp(all_indices, valid_indices, raw_z[valid_indices], period=self.num_bins)
            med_speed = np.interp(all_indices, valid_indices, med_speed[valid_indices], period=self.num_bins)

        # Smooth centreline (moving average ~10m = 5 bins with wraparound)
        window = 5
        half = window // 2
        smooth_x = np.zeros(self.num_bins)
        smooth_y = np.zeros(self.num_bins)
        smooth_z = np.zeros(self.num_bins)

        for i in range(self.num_bins):
            idxs = [(i + offset) % self.num_bins for offset in range(-half, half + 1)]
            smooth_x[i] = float(np.mean(raw_x[idxs]))
            smooth_y[i] = float(np.mean(raw_y[idxs]))
            smooth_z[i] = float(np.mean(raw_z[idxs]))

        # Heading and curvature
        headings = np.zeros(self.num_bins)
        curvatures = np.zeros(self.num_bins)

        for i in range(self.num_bins):
            next_i = (i + 1) % self.num_bins
            prev_i = (i - 1 + self.num_bins) % self.num_bins
            dx = smooth_x[next_i] - smooth_x[prev_i]
            dz = smooth_z[next_i] - smooth_z[prev_i]
            headings[i] = math.atan2(dx, dz)

        for i in range(self.num_bins):
            next_i = (i + 1) % self.num_bins
            dh = (headings[next_i] - headings[i] + math.pi) % (2 * math.pi) - math.pi
            curvatures[i] = dh / self.bin_size

        # Usable width: lateral offsets 2nd and 98th percentiles per bin
        left_edges = np.zeros(self.num_bins)
        right_edges = np.zeros(self.num_bins)

        for i in range(self.num_bins):
            if self._xs[i]:
                nx = math.cos(headings[i])
                nz = -math.sin(headings[i])
                offsets = []
                for sx, sz in zip(self._xs[i], self._zs[i]):
                    off = (sx - smooth_x[i]) * nx + (sz - smooth_z[i]) * nz
                    offsets.append(off)
                left_edges[i] = float(np.percentile(offsets, 2))
                right_edges[i] = float(np.percentile(offsets, 98))
            else:
                left_edges[i] = -6.0
                right_edges[i] = 6.0

        # Construct BinData list
        bins: list[BinData] = []
        for i in range(self.num_bins):
            bins.append(
                BinData(
                    bin_index=i,
                    lap_distance_m=round(i * self.bin_size, 1),
                    x=round(float(smooth_x[i]), 3),
                    y=round(float(smooth_y[i]), 3),
                    z=round(float(smooth_z[i]), 3),
                    heading=round(float(headings[i]), 4),
                    curvature=round(float(curvatures[i]), 6),
                    left_edge_m=round(float(left_edges[i]), 2),
                    right_edge_m=round(float(right_edges[i]), 2),
                    median_speed_kph=round(float(med_speed[i]), 1),
                    lap_sample_count=int(lap_counts[i]),
                )
            )

        # Corners detection: bins where |curvature| > threshold, excluding active aero straights
        corners = self._detect_corners(bins, curvatures, med_speed)

        # Confidence readiness: ready if every bin has >= 3 distinct laps (or fallback if >90% coverage)
        valid_count = int(np.sum(lap_counts >= 1))
        coverage_pct = round((valid_count / self.num_bins) * 100.0, 1)
        is_ready = bool(np.all(lap_counts >= 3)) or (coverage_pct >= 90.0 and len(corners) > 0)

        now = time.time()
        model = TrackModel(
            track_id=self.track_id,
            track_length_m=self.track_length_m,
            version=1,
            bins=bins,
            corners=corners,
            is_ready=is_ready,
            coverage_pct=coverage_pct,
            created_at=now,
            updated_at=now,
        )
        return model

    def _is_in_active_aero(self, dist_m: float) -> bool:
        """Returns True if dist_m lies within a marked active aero straight."""
        for s_m, e_m in self.active_aero_zones:
            if s_m <= e_m:
                if s_m <= dist_m <= e_m:
                    return True
            else:
                if dist_m >= s_m or dist_m <= e_m:
                    return True
        return False

    def _detect_corners(
        self,
        bins: list[BinData],
        curvatures: np.ndarray,
        med_speed: np.ndarray,
    ) -> list[Corner]:
        """Identifies corners from curvature and min-speed profiles, merging gaps < 30m."""
        is_curved = np.zeros(self.num_bins, dtype=bool)
        for i in range(self.num_bins):
            d_m = i * self.bin_size
            if abs(curvatures[i]) >= self.curvature_threshold and not self._is_in_active_aero(d_m):
                is_curved[i] = True

        # Group contiguous curved bins
        raw_segments: list[tuple[int, int]] = []
        in_seg = False
        start_idx = 0
        for i in range(self.num_bins):
            if is_curved[i] and not in_seg:
                in_seg = True
                start_idx = i
            elif not is_curved[i] and in_seg:
                in_seg = False
                raw_segments.append((start_idx, i - 1))
        if in_seg:
            raw_segments.append((start_idx, self.num_bins - 1))

        if not raw_segments:
            return []

        # Merge segments if gap < 30m and curvature signs match
        merged: list[tuple[int, int]] = []
        cur_start, cur_end = raw_segments[0]

        for s_idx, e_idx in raw_segments[1:]:
            gap_m = (s_idx - cur_end) * self.bin_size
            sign_cur = np.sign(np.mean(curvatures[cur_start:cur_end + 1]))
            sign_next = np.sign(np.mean(curvatures[s_idx:e_idx + 1]))

            if gap_m <= 30.0 and (sign_cur == sign_next or sign_cur == 0 or sign_next == 0):
                cur_end = e_idx
            else:
                merged.append((cur_start, cur_end))
                cur_start, cur_end = s_idx, e_idx
        merged.append((cur_start, cur_end))

        # Filter out short blips (< 20m)
        merged = [
            (s, e) for s, e in merged
            if (e - s + 1) * self.bin_size >= 20.0
        ]

        # Build Corner objects
        corners: list[Corner] = []
        for c_idx, (s_idx, e_idx) in enumerate(merged, start=1):
            seg_curv = curvatures[s_idx:e_idx + 1]
            seg_speeds = med_speed[s_idx:e_idx + 1]

            mean_c = float(np.mean(seg_curv))
            direction = "right" if mean_c > 0 else "left"

            # Apex: bin with max |curvature|, cross-checked with min speed
            max_c_idx = s_idx + int(np.argmax(np.abs(seg_curv)))
            min_spd_idx = s_idx + int(np.argmin(seg_speeds))
            # Average apex between max curvature and min speed point
            apex_bin = int(round((max_c_idx + min_spd_idx) / 2.0))
            apex_m = round(apex_bin * self.bin_size, 1)

            min_spd = float(np.min(seg_speeds))

            # Braking zone from all cars' brake onset distributions
            s_m = round(s_idx * self.bin_size, 1)
            e_m = round(e_idx * self.bin_size, 1)

            b_starts = [
                d for d, sp in self._brake_onsets
                if (s_m - 200.0) <= d <= (apex_m + 20.0)
            ]

            if b_starts:
                b_med = float(np.median(b_starts))
                b_p10 = float(np.percentile(b_starts, 10))
                b_p90 = float(np.percentile(b_starts, 90))
                entry_spds = [sp for d, sp in self._brake_onsets if (s_m - 200.0) <= d <= (apex_m + 20.0)]
                entry_spd = float(np.median(entry_spds)) if entry_spds else min_spd + 40.0
            else:
                # Default fallback estimation before brake onset events
                b_med = max(0.0, s_m - 60.0)
                b_p10 = max(0.0, s_m - 80.0)
                b_p90 = max(0.0, s_m - 40.0)
                entry_spd = min_spd + 50.0

            # Default name from Turn number or KnowledgeBase matching
            corner_name = f"Turn {c_idx}"
            if self.knowledge:
                try:
                    c_names = self.knowledge.get_corner_names(self.track_id)
                    if c_names and c_idx in c_names:
                        corner_name = c_names[c_idx]
                except Exception:
                    pass

            corners.append(
                Corner(
                    number=c_idx,
                    name=corner_name,
                    start_dist_m=s_m,
                    apex_dist_m=apex_m,
                    end_dist_m=e_m,
                    direction=direction,
                    field_median_min_speed_kph=round(min_spd, 1),
                    braking_start_median_m=round(b_med, 1),
                    braking_start_p10_m=round(b_p10, 1),
                    braking_start_p90_m=round(b_p90, 1),
                    median_entry_speed_kph=round(entry_spd, 1),
                )
            )

        return corners


class TrackModelStorage:
    """Manages SQLite storage for track models with schema versioning and apex matching."""

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
                CREATE TABLE IF NOT EXISTS track_models (
                    track_id INTEGER,
                    track_length_m REAL,
                    version INTEGER,
                    is_ready INTEGER,
                    model_json TEXT,
                    created_at REAL,
                    updated_at REAL,
                    PRIMARY KEY (track_id, track_length_m)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS corner_names (
                    track_id INTEGER,
                    corner_number INTEGER,
                    apex_dist_m REAL,
                    name TEXT,
                    updated_at REAL,
                    PRIMARY KEY (track_id, corner_number)
                )
            """)
            conn.commit()

    def load_model(self, track_id: int, track_length_m: float = 0.0) -> Optional[TrackModel]:
        with self._get_conn() as conn:
            if track_length_m > 0:
                cur = conn.execute(
                    "SELECT * FROM track_models WHERE track_id = ? AND ABS(track_length_m - ?) < 50.0",
                    (track_id, track_length_m),
                )
            else:
                cur = conn.execute(
                    "SELECT * FROM track_models WHERE track_id = ? ORDER BY updated_at DESC LIMIT 1",
                    (track_id,),
                )
            row = cur.fetchone()
            if not row:
                return None
            try:
                data = json.loads(row["model_json"])
                model = TrackModel.from_dict(data)
                # Re-apply persisted corner names by apex proximity
                self.apply_stored_corner_names(model)
                return model
            except Exception as e:
                log.warning("Failed deserializing track model for track_id=%s: %s", track_id, e)
                return None

    def save_model(self, model: TrackModel):
        # Preserve existing corner names by matching apex distances
        self.apply_stored_corner_names(model)
        now = time.time()
        data = model.to_dict()
        with self._get_conn() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO track_models
                (track_id, track_length_m, version, is_ready, model_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                model.track_id,
                model.track_length_m,
                model.version,
                1 if model.is_ready else 0,
                json.dumps(data),
                model.created_at or now,
                now,
            ))
            conn.commit()

    def set_corner_name(self, track_id: int, corner_number: int, apex_dist_m: float, name: str):
        """Sets a custom corner name, preserved across future model rebuilds."""
        with self._get_conn() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO corner_names (track_id, corner_number, apex_dist_m, name, updated_at)
                VALUES (?, ?, ?, ?, ?)
            """, (track_id, corner_number, apex_dist_m, name, time.time()))
            conn.commit()

    def apply_stored_corner_names(self, model: TrackModel):
        """Matches custom corner names by nearest apex within 50 m across model rebuilds."""
        with self._get_conn() as conn:
            cur = conn.execute("SELECT * FROM corner_names WHERE track_id = ?", (model.track_id,))
            rows = cur.fetchall()
            for r in rows:
                c_num = r["corner_number"]
                c_apex = r["apex_dist_m"]
                name = r["name"]
                # Match nearest apex within 50 m
                best_c = None
                best_diff = 50.0
                for c in model.corners:
                    diff = abs(c.apex_dist_m - c_apex)
                    if diff < best_diff:
                        best_diff = diff
                        best_c = c
                if best_c:
                    best_c.name = name
                elif 0 <= c_num - 1 < len(model.corners):
                    model.corners[c_num - 1].name = name

    def delete_model(self, track_id: int):
        with self._get_conn() as conn:
            conn.execute("DELETE FROM track_models WHERE track_id = ?", (track_id,))
            conn.commit()
