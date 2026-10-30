"""Mini-sector, sector, and corner comparison system for F1 2026 (Prompt 22).

Tracks 10m timing and telemetry bins for all 24 cars to power:
1. Official sectors & mini-sectors (default 25 equal segments)
2. F1-style sector colors (purple = session best, green = personal best, yellow = slower)
3. Field view ("fastest by mini-sector" with driver/team colors & wins legend)
4. Lap vs lap and Me vs Reference comparisons with delta strips and speed/brake traces
5. "Where I lose most" diagnosis (braking earlier, lower min speed, later on throttle)
6. Theoretical best lap calculation (sum of personal best mini-sectors)
7. Exclusion of flagged laps (SC/VSC, pit in/out, invalid, traffic)
8. Telemetry restriction detection (Participants m_yourTelemetry = 0)
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import logging
from pathlib import Path
import sqlite3
import struct
import time
from typing import Any, Optional
import zlib

import numpy as np

from f1telemetry.state import RaceState
from trackmodel import TrackModel, Corner

log = logging.getLogger("engineer.minisectors")

DEFAULT_MINISECTORS = 25
BIN_STEP_M = 10.0


@dataclass
class SegmentSummary:
    index: int
    name: str                        # e.g. "Turn 3 (Village)", "Mini-sector 5", "Straight after T4"
    kind: str                        # "minisector", "corner", "straight", "sector"
    start_m: float
    end_m: float
    time_a_ms: int
    time_b_ms: int
    delta_s: float                   # (time_a - time_b) / 1000.0 (positive = A slower than B)
    color: str                       # Color based on winner & delta intensity
    winner_car_idx: Optional[int] = None
    winner_name: str = ""
    winner_color: str = ""
    min_speed_a_kph: Optional[float] = None
    min_speed_b_kph: Optional[float] = None
    max_speed_a_kph: Optional[float] = None
    max_speed_b_kph: Optional[float] = None
    braking_point_a_m: Optional[float] = None
    braking_point_b_m: Optional[float] = None
    diagnosis: str = ""              # e.g. "Braking earlier", "Lower minimum speed", "Later on throttle"


@dataclass
class LapTimingRecord:
    session_uid: str
    car_idx: int
    lap_num: int
    lap_time_ms: int
    s1_ms: int
    s2_ms: int
    s3_ms: int
    is_valid: bool
    is_pit: bool
    is_sc: bool
    num_bins: int
    times_ms: list[int]              # Sampled at every 10m from 0 to length_m
    speed_kph: list[float]
    throttle: list[float]
    brake: list[float]
    is_traffic: bool = False
    telemetry_restricted: bool = False


class MiniSectorStorage:
    def __init__(self, db_path: str = "storage.db"):
        self.db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS lap_timing (
                    session_uid TEXT NOT NULL,
                    car_idx INTEGER NOT NULL,
                    lap_num INTEGER NOT NULL,
                    lap_time_ms INTEGER NOT NULL,
                    s1_ms INTEGER DEFAULT 0,
                    s2_ms INTEGER DEFAULT 0,
                    s3_ms INTEGER DEFAULT 0,
                    is_valid INTEGER DEFAULT 1,
                    is_pit INTEGER DEFAULT 0,
                    is_sc INTEGER DEFAULT 0,
                    is_traffic INTEGER DEFAULT 0,
                    telemetry_restricted INTEGER DEFAULT 0,
                    num_bins INTEGER NOT NULL,
                    times_blob BLOB NOT NULL,
                    speed_blob BLOB,
                    throttle_blob BLOB,
                    brake_blob BLOB,
                    created_at REAL NOT NULL,
                    PRIMARY KEY(session_uid, car_idx, lap_num)
                );
                CREATE INDEX IF NOT EXISTS idx_lap_timing_lookup
                    ON lap_timing(session_uid, car_idx, lap_num);
            """)
            try:
                conn.execute("ALTER TABLE lap_timing ADD COLUMN is_traffic INTEGER DEFAULT 0")
            except sqlite3.OperationalError:
                pass

    def save_lap_timing(self, record: LapTimingRecord) -> None:
        """Saves compressed timing and telemetry arrays for a car's lap."""
        # Times: uint32 ms
        times_bytes = zlib.compress(struct.pack(f"<{len(record.times_ms)}I", *record.times_ms))
        
        speed_bytes = None
        throttle_bytes = None
        brake_bytes = None

        if not record.telemetry_restricted and record.speed_kph:
            speed_ints = [int(round(s * 10)) for s in record.speed_kph]
            speed_bytes = zlib.compress(struct.pack(f"<{len(speed_ints)}H", *speed_ints))

            th_ints = [int(round(t * 100)) for t in record.throttle]
            throttle_bytes = zlib.compress(struct.pack(f"<{len(th_ints)}B", *th_ints))

            br_ints = [int(round(b * 100)) for b in record.brake]
            brake_bytes = zlib.compress(struct.pack(f"<{len(br_ints)}B", *br_ints))

        now = time.time()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO lap_timing (
                    session_uid, car_idx, lap_num, lap_time_ms, s1_ms, s2_ms, s3_ms,
                    is_valid, is_pit, is_sc, is_traffic, telemetry_restricted, num_bins,
                    times_blob, speed_blob, throttle_blob, brake_blob, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                record.session_uid, record.car_idx, record.lap_num, record.lap_time_ms,
                record.s1_ms, record.s2_ms, record.s3_ms,
                1 if record.is_valid else 0,
                1 if record.is_pit else 0,
                1 if record.is_sc else 0,
                1 if record.is_traffic else 0,
                1 if record.telemetry_restricted else 0,
                record.num_bins, times_bytes, speed_bytes, throttle_bytes, brake_bytes, now
            ))
            conn.commit()

    def get_lap_timing(self, session_uid: str, car_idx: int, lap_num: int) -> Optional[LapTimingRecord]:
        """Retrieves and decompresses lap timing and telemetry arrays."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("""
                SELECT * FROM lap_timing
                WHERE session_uid = ? AND car_idx = ? AND lap_num = ?
            """, (session_uid, car_idx, lap_num)).fetchone()

            if not row:
                return None

            n = row["num_bins"]
            raw_t = zlib.decompress(row["times_blob"])
            times_ms = list(struct.unpack(f"<{n}I", raw_t))

            speed_kph: list[float] = []
            throttle: list[float] = []
            brake: list[float] = []

            if not row["telemetry_restricted"] and row["speed_blob"]:
                raw_s = zlib.decompress(row["speed_blob"])
                speed_kph = [s / 10.0 for s in struct.unpack(f"<{n}H", raw_s)]

                raw_th = zlib.decompress(row["throttle_blob"])
                throttle = [t / 100.0 for t in struct.unpack(f"<{n}B", raw_th)]

                raw_br = zlib.decompress(row["brake_blob"])
                brake = [b / 100.0 for b in struct.unpack(f"<{n}B", raw_br)]

            row_keys = row.keys()
            is_trf = bool(row["is_traffic"]) if "is_traffic" in row_keys else False

            return LapTimingRecord(
                session_uid=row["session_uid"],
                car_idx=row["car_idx"],
                lap_num=row["lap_num"],
                lap_time_ms=row["lap_time_ms"],
                s1_ms=row["s1_ms"],
                s2_ms=row["s2_ms"],
                s3_ms=row["s3_ms"],
                is_valid=bool(row["is_valid"]),
                is_pit=bool(row["is_pit"]),
                is_sc=bool(row["is_sc"]),
                is_traffic=is_trf,
                telemetry_restricted=bool(row["telemetry_restricted"]),
                num_bins=n,
                times_ms=times_ms,
                speed_kph=speed_kph,
                throttle=throttle,
                brake=brake,
            )

    def list_session_laps(self, session_uid: str, include_flagged: bool = False) -> list[dict[str, Any]]:
        """Lists all recorded laps in a session with metadata."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            query = """
                SELECT session_uid, car_idx, lap_num, lap_time_ms, s1_ms, s2_ms, s3_ms,
                       is_valid, is_pit, is_sc, is_traffic, telemetry_restricted, num_bins
                FROM lap_timing
                WHERE session_uid = ?
            """
            if not include_flagged:
                query += " AND is_valid = 1 AND is_pit = 0 AND is_sc = 0 AND is_traffic = 0"
            query += " ORDER BY car_idx ASC, lap_num ASC"
            rows = conn.execute(query, (session_uid,)).fetchall()
            return [dict(r) for r in rows]


class MiniSectorTracker:
    def __init__(
        self,
        storage: Optional[MiniSectorStorage] = None,
        num_minisectors: int = DEFAULT_MINISECTORS,
    ):
        self.storage = storage or MiniSectorStorage()
        self.num_minisectors = num_minisectors
        # Live buffer: car_idx -> list of (distance_m, time_ms, speed_kph, throttle, brake)
        self._live_samples: dict[int, list[tuple[float, int, float, float, float]]] = {}
        self._current_laps: dict[int, int] = {}
        self._last_distances: dict[int, float] = {}

    def on_tick(self, state: RaceState, now: float) -> None:
        """Buffers live telemetry samples per car at 20-60Hz."""
        if not state.lap or not state.tel:
            return

        for car_idx in range(len(state.lap)):
            lap_data = state.lap[car_idx]
            tel_data = state.tel[car_idx] if car_idx < len(state.tel) else None

            if not lap_data:
                continue

            current_lap = getattr(lap_data, "current_lap_num", 1)
            dist_m = float(getattr(lap_data, "lap_distance", 0.0))
            time_ms = int(getattr(lap_data, "current_lap_time_ms", 0))

            spd = float(getattr(tel_data, "speed", 0.0)) if tel_data else 0.0
            th = float(getattr(tel_data, "throttle", 0.0)) if tel_data else 0.0
            br = float(getattr(tel_data, "brake", 0.0)) if tel_data else 0.0

            # Check for lap transition
            prev_lap = self._current_laps.get(car_idx, current_lap)
            prev_dist = self._last_distances.get(car_idx, dist_m)

            if current_lap != prev_lap or (prev_dist > 1000.0 and dist_m < 200.0):
                # Lap completed: process and store previous lap samples
                self._finalize_lap(state, car_idx, prev_lap)
                self._live_samples[car_idx] = []
                self._current_laps[car_idx] = current_lap

            self._current_laps[car_idx] = current_lap
            self._last_distances[car_idx] = dist_m

            # Collect live sample (handle negative distance before line by clamping to 0.0)
            clean_dist = max(0.0, dist_m)
            self._live_samples.setdefault(car_idx, []).append((clean_dist, time_ms, spd, th, br))

    def _finalize_lap(self, state: RaceState, car_idx: int, lap_num: int) -> None:
        """Interpolates samples onto 10m grid and stores lap timing record."""
        samples = self._live_samples.get(car_idx, [])
        if len(samples) < 10:
            return

        track_len = float(state.track_length or 5000.0)
        num_bins = int(track_len // BIN_STEP_M) + 1

        lap_data = state.lap[car_idx] if car_idx < len(state.lap) else None
        lap_time_ms = int(getattr(lap_data, "last_lap_time_ms", 0))
        if lap_time_ms <= 0 and samples:
            lap_time_ms = max(s[1] for s in samples)

        is_valid = bool(getattr(lap_data, "current_lap_invalid", 0) == 0)
        is_pit = bool(getattr(lap_data, "pit_status", 0) != 0)
        is_sc = bool(getattr(state, "safety_car_status", 0) != 0)
        is_traffic = bool(getattr(state, "_lap_traffic_time_s", 0.0) > 3.0)

        # Check telemetry restriction
        telemetry_restricted = False
        if state.participants and car_idx < len(state.participants):
            p = state.participants[car_idx]
            if getattr(p, "your_telemetry", 1) == 0:
                telemetry_restricted = True

        # Interpolate
        times_ms, speeds, throttles, brakes = self.interpolate_lap_samples(samples, track_len, lap_time_ms)

        # Official sector times from LapData / SessionHistory if available
        s1_ms = int(getattr(lap_data, "sector1_time_ms", 0))
        s2_ms = int(getattr(lap_data, "sector2_time_ms", 0))
        s3_ms = max(0, lap_time_ms - s1_ms - s2_ms) if (s1_ms > 0 and s2_ms > 0) else 0

        rec = LapTimingRecord(
            session_uid=str(state.session_uid or "0"),
            car_idx=car_idx,
            lap_num=lap_num,
            lap_time_ms=lap_time_ms,
            s1_ms=s1_ms,
            s2_ms=s2_ms,
            s3_ms=s3_ms,
            is_valid=is_valid,
            is_pit=is_pit,
            is_sc=is_sc,
            is_traffic=is_traffic,
            telemetry_restricted=telemetry_restricted,
            num_bins=num_bins,
            times_ms=times_ms,
            speed_kph=speeds if not telemetry_restricted else [],
            throttle=throttles if not telemetry_restricted else [],
            brake=brakes if not telemetry_restricted else [],
        )
        self.storage.save_lap_timing(rec)

    @staticmethod
    def interpolate_lap_samples(
        samples: list[tuple[float, int, float, float, float]],
        track_len_m: float,
        final_lap_time_ms: int,
    ) -> tuple[list[int], list[float], list[float], list[float]]:
        """Interpolates irregular distance samples onto uniform 10m bins."""
        num_bins = int(track_len_m // BIN_STEP_M) + 1
        target_grid = np.linspace(0.0, track_len_m, num_bins)

        # Sort samples by distance, deduplicate distances
        samples_sorted = sorted(samples, key=lambda s: s[0])
        dist_raw: list[float] = []
        time_raw: list[float] = []
        spd_raw: list[float] = []
        th_raw: list[float] = []
        br_raw: list[float] = []

        last_d = -1.0
        for d, t, s, th, br in samples_sorted:
            d_clamped = max(0.0, min(track_len_m, d))
            if d_clamped > last_d:
                dist_raw.append(d_clamped)
                time_raw.append(float(t))
                spd_raw.append(float(s))
                th_raw.append(float(th))
                br_raw.append(float(br))
                last_d = d_clamped

        if len(dist_raw) < 2:
            # Fallback if too few samples
            times_ms = [int(round(i * (final_lap_time_ms / max(1, num_bins - 1)))) for i in range(num_bins)]
            return times_ms, [150.0] * num_bins, [0.8] * num_bins, [0.0] * num_bins

        # Ensure start and end boundaries are covered
        if dist_raw[0] > 0.0:
            dist_raw.insert(0, 0.0)
            time_raw.insert(0, 0.0)
            spd_raw.insert(0, spd_raw[0])
            th_raw.insert(0, th_raw[0])
            br_raw.insert(0, br_raw[0])

        if dist_raw[-1] < track_len_m:
            dist_raw.append(track_len_m)
            time_raw.append(float(final_lap_time_ms))
            spd_raw.append(spd_raw[-1])
            th_raw.append(th_raw[-1])
            br_raw.append(br_raw[-1])

        # Monotonic time interpolation
        interp_times = np.interp(target_grid, dist_raw, time_raw)
        interp_speeds = np.interp(target_grid, dist_raw, spd_raw)
        interp_throttles = np.interp(target_grid, dist_raw, th_raw)
        interp_brakes = np.interp(target_grid, dist_raw, br_raw)

        # Guarantee monotonic times
        for i in range(1, len(interp_times)):
            if interp_times[i] < interp_times[i - 1]:
                interp_times[i] = interp_times[i - 1]

        times_ms = [int(round(t)) for t in interp_times]
        speeds = [round(float(s), 1) for s in interp_speeds]
        throttles = [round(float(th), 2) for th in interp_throttles]
        brakes = [round(float(br), 2) for br in interp_brakes]

        return times_ms, speeds, throttles, brakes

    # -------------------------------------------------------------------------
    # Comparison & Delta Analytics
    # -------------------------------------------------------------------------
    def compute_minisector_times(
        self,
        record: LapTimingRecord,
        track_len_m: float,
        num_minisectors: Optional[int] = None,
    ) -> list[int]:
        """Calculates segment duration in ms for each mini-sector."""
        n_sectors = num_minisectors or self.num_minisectors
        seg_times: list[int] = []
        num_bins = record.num_bins

        for i in range(n_sectors):
            start_dist = i * (track_len_m / n_sectors)
            end_dist = (i + 1) * (track_len_m / n_sectors)

            start_idx = min(num_bins - 1, int(round(start_dist / BIN_STEP_M)))
            end_idx = min(num_bins - 1, int(round(end_dist / BIN_STEP_M)))

            t_start = record.times_ms[start_idx]
            t_end = record.times_ms[end_idx] if end_idx > start_idx else (
                record.lap_time_ms if i == n_sectors - 1 else record.times_ms[start_idx]
            )
            seg_times.append(max(0, t_end - t_start))

        return seg_times

    def compare_laps(
        self,
        record_a: LapTimingRecord,
        record_b: LapTimingRecord,
        track_len_m: float,
        track_model: Optional[TrackModel] = None,
        driver_a_name: str = "Driver A",
        driver_b_name: str = "Driver B",
        color_a: str = "#00e676",
        color_b: str = "#2979ff",
    ) -> dict[str, Any]:
        """Compares two laps: produces mini-sector deltas, colors, delta strip, and loss reasons."""
        n_sectors = self.num_minisectors
        times_a = self.compute_minisector_times(record_a, track_len_m, n_sectors)
        times_b = self.compute_minisector_times(record_b, track_len_m, n_sectors)

        segments: list[dict[str, Any]] = []
        delta_strip: list[dict[str, Any]] = []

        cum_time_a = 0
        cum_time_b = 0

        for i in range(n_sectors):
            s_start = i * (track_len_m / n_sectors)
            s_end = (i + 1) * (track_len_m / n_sectors)
            t_a = times_a[i]
            t_b = times_b[i]
            cum_time_a += t_a
            cum_time_b += t_b

            delta_s = round((t_a - t_b) / 1000.0, 3)
            cum_delta_s = round((cum_time_a - cum_time_b) / 1000.0, 3)

            # Winner of segment
            if delta_s < 0:
                winner = driver_a_name
                winner_color = color_a
                intensity = min(1.0, abs(delta_s) / 0.25)
            elif delta_s > 0:
                winner = driver_b_name
                winner_color = color_b
                intensity = min(1.0, abs(delta_s) / 0.25)
            else:
                winner = "Tie"
                winner_color = "#9e9e9e"
                intensity = 0.0

            # Telemetry metrics in segment
            idx_start = min(record_a.num_bins - 1, int(s_start // BIN_STEP_M))
            idx_end = min(record_a.num_bins - 1, int(s_end // BIN_STEP_M)) + 1

            min_spd_a = min(record_a.speed_kph[idx_start:idx_end]) if record_a.speed_kph else None
            min_spd_b = min(record_b.speed_kph[idx_start:idx_end]) if record_b.speed_kph else None
            max_spd_a = max(record_a.speed_kph[idx_start:idx_end]) if record_a.speed_kph else None
            max_spd_b = max(record_b.speed_kph[idx_start:idx_end]) if record_b.speed_kph else None

            # Braking point detection (where brake > 0.15)
            bp_a = None
            bp_b = None
            if record_a.brake:
                for b_idx in range(idx_start, min(idx_end, len(record_a.brake))):
                    if record_a.brake[b_idx] > 0.15:
                        bp_a = round(b_idx * BIN_STEP_M, 1)
                        break
            if record_b.brake:
                for b_idx in range(idx_start, min(idx_end, len(record_b.brake))):
                    if record_b.brake[b_idx] > 0.15:
                        bp_b = round(b_idx * BIN_STEP_M, 1)
                        break

            # Diagnose loss reason if A is slower
            diagnosis = ""
            if delta_s > 0.03:
                if bp_a is not None and bp_b is not None and bp_a < bp_b - 12.0:
                    diagnosis = "Braking earlier"
                elif min_spd_a is not None and min_spd_b is not None and min_spd_a < min_spd_b - 4.0:
                    diagnosis = "Lower minimum speed"
                elif max_spd_a is not None and max_spd_b is not None and max_spd_a < max_spd_b - 4.0:
                    diagnosis = "Lower top speed / exit traction"
                else:
                    diagnosis = "Corner apex / line variance"

            # Resolve name from track model if available
            c_name = f"Mini-sector {i + 1}"
            if track_model:
                c = track_model.get_corner_at_dist((s_start + s_end) / 2.0)
                if c:
                    c_name = c.name

            seg_data = {
                "index": i + 1,
                "name": c_name,
                "start_m": round(s_start, 1),
                "end_m": round(s_end, 1),
                "time_a_ms": t_a,
                "time_b_ms": t_b,
                "delta_s": delta_s,
                "cum_delta_s": cum_delta_s,
                "winner": winner,
                "winner_color": winner_color,
                "intensity": round(intensity, 2),
                "min_speed_a_kph": min_spd_a,
                "min_speed_b_kph": min_spd_b,
                "max_speed_a_kph": max_spd_a,
                "max_speed_b_kph": max_spd_b,
                "braking_point_a_m": bp_a,
                "braking_point_b_m": bp_b,
                "diagnosis": diagnosis,
            }
            segments.append(seg_data)
            delta_strip.append({
                "start_m": round(s_start, 1),
                "end_m": round(s_end, 1),
                "delta_s": delta_s,
                "cum_delta_s": cum_delta_s,
                "color": winner_color,
            })

        # Top 3 loss segments for Lap A vs Lap B
        loss_segments = sorted([s for s in segments if s["delta_s"] > 0], key=lambda s: s["delta_s"], reverse=True)[:3]

        return {
            "driver_a": driver_a_name,
            "driver_b": driver_b_name,
            "lap_time_a_ms": record_a.lap_time_ms,
            "lap_time_b_ms": record_b.lap_time_ms,
            "total_delta_s": round((record_a.lap_time_ms - record_b.lap_time_ms) / 1000.0, 3),
            "segments": segments,
            "delta_strip": delta_strip,
            "top_losses": loss_segments,
        }

    def get_fastest_by_minisector(
        self,
        session_uid: str,
        track_len_m: float,
        drivers_meta: Optional[dict[int, dict[str, Any]]] = None,
        include_flagged: bool = False,
    ) -> dict[str, Any]:
        """Computes the fastest driver for each mini-sector across the entire field."""
        laps = self.storage.list_session_laps(session_uid, include_flagged=include_flagged)
        if not laps:
            return {"sectors": [], "wins_legend": []}

        drivers_meta = drivers_meta or {}
        default_palette = [
            "#e53935", "#1e88e5", "#43a047", "#fb8c00", "#8e24aa",
            "#00acc1", "#3949ab", "#fdd835", "#d81b60", "#00897b", "#6d4c41"
        ]

        # Load timing records for all valid laps
        n_sectors = self.num_minisectors
        sector_bests: list[dict[str, Any]] = [{"best_ms": 99999999, "car_idx": None} for _ in range(n_sectors)]

        for lap_info in laps:
            rec = self.storage.get_lap_timing(session_uid, lap_info["car_idx"], lap_info["lap_num"])
            if not rec:
                continue
            seg_times = self.compute_minisector_times(rec, track_len_m, n_sectors)
            for s_idx in range(n_sectors):
                t = seg_times[s_idx]
                if 0 < t < sector_bests[s_idx]["best_ms"]:
                    sector_bests[s_idx] = {"best_ms": t, "car_idx": rec.car_idx}

        # Build sectors payload and tally wins
        wins_tally: dict[int, int] = {}
        sectors_out: list[dict[str, Any]] = []

        for i in range(n_sectors):
            s_start = i * (track_len_m / n_sectors)
            s_end = (i + 1) * (track_len_m / n_sectors)
            b = sector_bests[i]
            c_idx = b["car_idx"]

            d_info = drivers_meta.get(c_idx, {}) if c_idx is not None else {}
            name = d_info.get("name", f"Car {c_idx}" if c_idx is not None else "None")
            team = d_info.get("team", "Unknown")
            color = d_info.get("color", default_palette[c_idx % len(default_palette)] if c_idx is not None else "#757575")

            if c_idx is not None:
                wins_tally[c_idx] = wins_tally.get(c_idx, 0) + 1

            sectors_out.append({
                "sector_index": i + 1,
                "start_m": round(s_start, 1),
                "end_m": round(s_end, 1),
                "best_time_ms": b["best_ms"] if b["best_ms"] < 99999999 else None,
                "winner_car_idx": c_idx,
                "winner_name": name,
                "winner_team": team,
                "winner_color": color,
            })

        # Legend sorted by mini-sectors won
        legend = []
        for c_idx, wins in sorted(wins_tally.items(), key=lambda x: x[1], reverse=True):
            d_info = drivers_meta.get(c_idx, {})
            name = d_info.get("name", f"Car {c_idx}")
            team = d_info.get("team", "Unknown")
            color = d_info.get("color", default_palette[c_idx % len(default_palette)])
            legend.append({
                "car_idx": c_idx,
                "name": name,
                "team": team,
                "color": color,
                "sectors_won": wins,
                "pct": round(wins / n_sectors * 100.0, 1),
            })

        return {"sectors": sectors_out, "wins_legend": legend}

    def get_theoretical_best(
        self,
        session_uid: str,
        car_idx: int,
        track_len_m: float,
        include_flagged: bool = False,
    ) -> dict[str, Any]:
        """Calculates a driver's theoretical best lap from their best mini-sectors."""
        laps = self.storage.list_session_laps(session_uid, include_flagged=include_flagged)
        driver_laps = [l for l in laps if l["car_idx"] == car_idx]
        if not driver_laps:
            return {"has_data": False, "theoretical_best_ms": None}

        n_sectors = self.num_minisectors
        best_per_sector = [99999999] * n_sectors
        actual_best_ms = min(l["lap_time_ms"] for l in driver_laps if l["lap_time_ms"] > 0)

        for l_info in driver_laps:
            rec = self.storage.get_lap_timing(session_uid, car_idx, l_info["lap_num"])
            if not rec:
                continue
            times = self.compute_minisector_times(rec, track_len_m, n_sectors)
            for i in range(n_sectors):
                if 0 < times[i] < best_per_sector[i]:
                    best_per_sector[i] = times[i]

        theoretical_best_ms = sum(best_per_sector)
        delta_s = round((actual_best_ms - theoretical_best_ms) / 1000.0, 3)

        def format_ms(ms: int) -> str:
            m = ms // 60000
            s = (ms % 60000) / 1000.0
            return f"{m}:{s:06.3f}"

        return {
            "has_data": True,
            "car_idx": car_idx,
            "actual_best_ms": actual_best_ms,
            "actual_best_str": format_ms(actual_best_ms),
            "theoretical_best_ms": theoretical_best_ms,
            "theoretical_best_str": format_ms(theoretical_best_ms),
            "potential_gain_s": delta_s,
            "summary_text": f"Your ideal lap is {format_ms(theoretical_best_ms)}, {delta_s:.3f}s faster than your best.",
        }

    def get_f1_sector_colors(
        self,
        session_uid: str,
        car_idx: int,
        lap_num: int,
        track_len_m: float,
        include_flagged: bool = False,
    ) -> dict[str, Any]:
        """Returns F1-style sector colors (purple = session best, green = personal best, yellow = slower)."""
        target_rec = self.storage.get_lap_timing(session_uid, car_idx, lap_num)
        if not target_rec:
            return {"sectors": [], "mini_sectors": []}

        all_laps = self.storage.list_session_laps(session_uid, include_flagged=include_flagged)
        n_sectors = self.num_minisectors
        target_minis = self.compute_minisector_times(target_rec, track_len_m, n_sectors)

        # 1. Official sectors comparison (S1, S2, S3)
        session_best_s1 = min(l["s1_ms"] for l in all_laps if l["s1_ms"] > 0) if all_laps else 999999
        session_best_s2 = min(l["s2_ms"] for l in all_laps if l["s2_ms"] > 0) if all_laps else 999999
        session_best_s3 = min(l["s3_ms"] for l in all_laps if l["s3_ms"] > 0) if all_laps else 999999

        my_laps = [l for l in all_laps if l["car_idx"] == car_idx]
        pb_s1 = min(l["s1_ms"] for l in my_laps if l["s1_ms"] > 0) if my_laps else 999999
        pb_s2 = min(l["s2_ms"] for l in my_laps if l["s2_ms"] > 0) if my_laps else 999999
        pb_s3 = min(l["s3_ms"] for l in my_laps if l["s3_ms"] > 0) if my_laps else 999999

        def color_code(t_ms: int, pb_ms: int, sb_ms: int) -> str:
            if t_ms <= sb_ms:
                return "purple"
            elif t_ms <= pb_ms:
                return "green"
            return "yellow"

        sectors_out = [
            {"sector": 1, "time_ms": target_rec.s1_ms, "color": color_code(target_rec.s1_ms, pb_s1, session_best_s1)},
            {"sector": 2, "time_ms": target_rec.s2_ms, "color": color_code(target_rec.s2_ms, pb_s2, session_best_s2)},
            {"sector": 3, "time_ms": target_rec.s3_ms, "color": color_code(target_rec.s3_ms, pb_s3, session_best_s3)},
        ]

        # 2. Mini-sectors comparison
        sb_minis = [99999999] * n_sectors
        pb_minis = [99999999] * n_sectors

        for l_info in all_laps:
            rec = self.storage.get_lap_timing(session_uid, l_info["car_idx"], l_info["lap_num"])
            if not rec:
                continue
            seg_times = self.compute_minisector_times(rec, track_len_m, n_sectors)
            for i in range(n_sectors):
                t = seg_times[i]
                if 0 < t < sb_minis[i]:
                    sb_minis[i] = t
                if l_info["car_idx"] == car_idx and 0 < t < pb_minis[i]:
                    pb_minis[i] = t

        mini_colors = []
        for i in range(n_sectors):
            t = target_minis[i]
            c = color_code(t, pb_minis[i], sb_minis[i])
            mini_colors.append({
                "mini_sector": i + 1,
                "time_ms": t,
                "color": c,
            })

        return {"sectors": sectors_out, "mini_sectors": mini_colors}
