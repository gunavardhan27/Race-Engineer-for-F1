"""Persistent session storage and lap trace recording for F1 25.

Uses SQLite with WAL mode and a dedicated background writer thread fed by a Queue,
ensuring disk writes never block the asyncio telemetry loop.
Traces for the player are resampled onto an exact 5-meter grid of lapDistance and
stored as compressed blobs (NumPy + zlib/deflate) to keep the database small.
"""
from __future__ import annotations

import io
import json
import logging
import queue
import sqlite3
import threading
import time
import zlib
from typing import Any, Optional

import numpy as np

log = logging.getLogger("engineer.storage")


def compress_trace(trace: dict[str, np.ndarray]) -> bytes:
    """Serializes and compresses a lap trace dict using numpy savez_compressed."""
    buf = io.BytesIO()
    np.savez_compressed(
        buf,
        distance=np.asarray(trace["distance"], dtype=np.float32),
        speed=np.asarray(trace["speed"], dtype=np.float32),
        throttle=np.asarray(trace["throttle"], dtype=np.float32),
        brake=np.asarray(trace["brake"], dtype=np.float32),
        steer=np.asarray(trace["steer"], dtype=np.float32),
        gear=np.asarray(trace["gear"], dtype=np.int8),
        rpm=np.asarray(trace["rpm"], dtype=np.uint16),
        world_x=np.asarray(trace["world_x"], dtype=np.float32),
        world_z=np.asarray(trace["world_z"], dtype=np.float32),
    )
    return buf.getvalue()


def decompress_trace(blob: bytes) -> dict[str, np.ndarray]:
    """Decompresses and returns a lap trace dict from a compressed blob."""
    with io.BytesIO(blob) as buf:
        with np.load(buf) as npz:
            return {k: npz[k] for k in npz.files}


def resample_lap_trace(
    raw_samples: list[tuple],
    track_length_m: float,
    distance_step: float = 5.0,
) -> dict[str, np.ndarray]:
    """Resamples raw telemetry tuples onto a uniform grid every distance_step meters.

    Raw sample tuple format:
    (lap_distance, speed_kph, throttle, brake, steer, gear, rpm, world_x, world_z)
    """
    if not raw_samples:
        empty_f = np.zeros(0, dtype=np.float32)
        return {
            "distance": empty_f,
            "speed": empty_f,
            "throttle": empty_f,
            "brake": empty_f,
            "steer": empty_f,
            "gear": np.zeros(0, dtype=np.int8),
            "rpm": np.zeros(0, dtype=np.uint16),
            "world_x": empty_f,
            "world_z": empty_f,
        }

    # Extract columns
    dists = np.array([s[0] for s in raw_samples], dtype=np.float32)
    speeds = np.array([s[1] for s in raw_samples], dtype=np.float32)
    throttles = np.array([s[2] for s in raw_samples], dtype=np.float32)
    brakes = np.array([s[3] for s in raw_samples], dtype=np.float32)
    steers = np.array([s[4] for s in raw_samples], dtype=np.float32)
    gears = np.array([s[5] for s in raw_samples], dtype=np.float32)
    rpms = np.array([s[6] for s in raw_samples], dtype=np.float32)
    xs = np.array([s[7] for s in raw_samples], dtype=np.float32)
    zs = np.array([s[8] for s in raw_samples], dtype=np.float32)

    # Sort by distance
    sort_idx = np.argsort(dists)
    dists = dists[sort_idx]
    speeds = speeds[sort_idx]
    throttles = throttles[sort_idx]
    brakes = brakes[sort_idx]
    steers = steers[sort_idx]
    gears = gears[sort_idx]
    rpms = rpms[sort_idx]
    xs = xs[sort_idx]
    zs = zs[sort_idx]

    # Ensure strictly monotonic increasing distances for 1D interpolation
    unique_mask = np.concatenate(([True], np.diff(dists) > 0.001))
    if not np.all(unique_mask):
        dists = dists[unique_mask]
        speeds = speeds[unique_mask]
        throttles = throttles[unique_mask]
        brakes = brakes[unique_mask]
        steers = steers[unique_mask]
        gears = gears[unique_mask]
        rpms = rpms[unique_mask]
        xs = xs[unique_mask]
        zs = zs[unique_mask]

    max_dist = float(max(dists[-1] if len(dists) else 0.0, track_length_m or 100.0))
    grid = np.arange(0.0, max_dist + (distance_step / 2.0), distance_step, dtype=np.float32)

    resampled_speed = np.interp(grid, dists, speeds).astype(np.float32)
    resampled_throttle = np.clip(np.interp(grid, dists, throttles), 0.0, 1.0).astype(np.float32)
    resampled_brake = np.clip(np.interp(grid, dists, brakes), 0.0, 1.0).astype(np.float32)
    resampled_steer = np.clip(np.interp(grid, dists, steers), -1.0, 1.0).astype(np.float32)
    resampled_gear = np.clip(np.round(np.interp(grid, dists, gears)), -1, 8).astype(np.int8)
    resampled_rpm = np.clip(np.round(np.interp(grid, dists, rpms)), 0, 65535).astype(np.uint16)
    resampled_x = np.interp(grid, dists, xs).astype(np.float32)
    resampled_z = np.interp(grid, dists, zs).astype(np.float32)

    return {
        "distance": grid,
        "speed": resampled_speed,
        "throttle": resampled_throttle,
        "brake": resampled_brake,
        "steer": resampled_steer,
        "gear": resampled_gear,
        "rpm": resampled_rpm,
        "world_x": resampled_x,
        "world_z": resampled_z,
    }


class SessionStorage:
    """Thread-safe SQLite persistent storage with WAL mode and asynchronous writer queue."""

    def __init__(self, db_path: str = "sessions.db"):
        self.db_path = db_path
        self._queue: queue.Queue = queue.Queue()
        self._closed = False
        self._init_db()

        self._writer_thread = threading.Thread(target=self._writer_worker, daemon=True, name="StorageWriter")
        self._writer_thread.start()

    def _init_db(self):
        """Initializes tables and WAL mode synchronously before starting background thread."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.execute("PRAGMA synchronous = NORMAL;")
            conn.execute("PRAGMA foreign_keys = ON;")

            conn.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_uid TEXT UNIQUE,
                    started_at REAL NOT NULL,
                    game_format INTEGER NOT NULL,
                    formula INTEGER NOT NULL DEFAULT 0,
                    track_id INTEGER NOT NULL,
                    session_type INTEGER NOT NULL,
                    team_id INTEGER NOT NULL DEFAULT 0,
                    weather_at_start INTEGER NOT NULL DEFAULT 0,
                    ai_difficulty INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS laps (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    lap_num INTEGER NOT NULL,
                    lap_time_s REAL NOT NULL,
                    sector1_s REAL,
                    sector2_s REAL,
                    sector3_s REAL,
                    is_valid INTEGER NOT NULL DEFAULT 1,
                    position INTEGER,
                    tyre_compound INTEGER,
                    tyre_age INTEGER,
                    fuel_start_kg REAL,
                    fuel_end_kg REAL,
                    tyre_wear_start_pct REAL,
                    tyre_wear_end_pct REAL,
                    setup_id TEXT,
                    stint INTEGER DEFAULT 0,
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS lap_traces (
                    lap_id INTEGER PRIMARY KEY REFERENCES laps(id) ON DELETE CASCADE,
                    num_points INTEGER NOT NULL,
                    distance_step_m REAL NOT NULL DEFAULT 5.0,
                    data BLOB NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_sessions_track ON sessions(track_id, formula, session_type);
                CREATE INDEX IF NOT EXISTS idx_laps_session ON laps(session_id, lap_num);
                CREATE INDEX IF NOT EXISTS idx_laps_time ON laps(lap_time_s, is_valid);

                CREATE TABLE IF NOT EXISTS overtakes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_uid TEXT NOT NULL,
                    lap_num INTEGER NOT NULL,
                    timestamp REAL NOT NULL,
                    other_vehicle_idx INTEGER NOT NULL,
                    other_driver_name TEXT,
                    direction TEXT NOT NULL,
                    classification TEXT NOT NULL,
                    braking_delta_m REAL,
                    start_gap_m REAL,
                    overlap_at_turn_in INTEGER,
                    ran_deep INTEGER,
                    contact INTEGER,
                    forced_off INTEGER,
                    switchback INTEGER,
                    corner_name TEXT,
                    raw_features TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_overtakes_session ON overtakes(session_uid, lap_num);

                CREATE TABLE IF NOT EXISTS session_timeline (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_uid TEXT NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    start_time REAL NOT NULL,
                    end_time REAL NOT NULL,
                    sample_count INTEGER NOT NULL,
                    data BLOB NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_timeline_session ON session_timeline(session_uid, chunk_index);
            """)
            try:
                conn.execute("ALTER TABLE sessions ADD COLUMN ai_difficulty INTEGER NOT NULL DEFAULT 0;")
            except Exception:
                pass
            conn.commit()

    def _writer_worker(self):
        """Dedicated background thread handling all SQLite write operations."""
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.execute("PRAGMA foreign_keys = ON;")

        try:
            while True:
                item = self._queue.get()
                if item is None:
                    break

                action, args = item
                try:
                    if action == "save_session":
                        self._exec_save_session(conn, args)
                    elif action == "save_lap":
                        self._exec_save_lap(conn, args)
                    elif action == "discard_after":
                        self._exec_discard_after(conn, args)
                    elif action == "save_overtake":
                        self._exec_save_overtake(conn, args)
                    elif action == "save_timeline_chunk":
                        self._exec_save_timeline_chunk(conn, args)
                    conn.commit()
                except Exception as e:
                    log.exception("Storage writer error executing %s: %s", action, e)
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                finally:
                    self._queue.task_done()
        finally:
            conn.close()

    def _exec_save_session(self, conn: sqlite3.Connection, data: dict[str, Any]):
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO sessions (session_uid, started_at, game_format, formula, track_id, session_type, team_id, weather_at_start, ai_difficulty)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_uid) DO UPDATE SET
                formula = excluded.formula,
                track_id = excluded.track_id,
                session_type = excluded.session_type,
                team_id = excluded.team_id,
                ai_difficulty = excluded.ai_difficulty;
        """, (
            str(data["session_uid"]),
            data.get("started_at", time.time()),
            data.get("game_format", 2025),
            data.get("formula", 0),
            data.get("track_id", 0),
            data.get("session_type", 0),
            data.get("team_id", 0),
            data.get("weather_at_start", 0),
            data.get("ai_difficulty", 0),
        ))

    def _exec_save_lap(self, conn: sqlite3.Connection, data: dict[str, Any]):
        cur = conn.cursor()
        # Find session_id from session_uid
        session_uid = str(data["session_uid"])
        cur.execute("SELECT id FROM sessions WHERE session_uid = ?", (session_uid,))
        row = cur.fetchone()
        if not row:
            # Create session on the fly if not yet recorded
            cur.execute("""
                INSERT INTO sessions (session_uid, started_at, game_format, formula, track_id, session_type, team_id, weather_at_start)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                session_uid,
                time.time(),
                data.get("game_format", 2025),
                data.get("formula", 0),
                data.get("track_id", 0),
                data.get("session_type", 0),
                data.get("team_id", 0),
                data.get("weather_at_start", 0),
            ))
            session_id = cur.lastrowid
        else:
            session_id = row[0]

        # Insert lap record
        cur.execute("""
            INSERT INTO laps (
                session_id, lap_num, lap_time_s, sector1_s, sector2_s, sector3_s,
                is_valid, position, tyre_compound, tyre_age, fuel_start_kg, fuel_end_kg,
                tyre_wear_start_pct, tyre_wear_end_pct, setup_id, stint, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            session_id,
            data["lap_num"],
            data["lap_time_s"],
            data.get("sector1_s"),
            data.get("sector2_s"),
            data.get("sector3_s"),
            1 if data.get("is_valid", True) else 0,
            data.get("position"),
            data.get("tyre_compound"),
            data.get("tyre_age"),
            data.get("fuel_start_kg"),
            data.get("fuel_end_kg"),
            data.get("tyre_wear_start_pct"),
            data.get("tyre_wear_end_pct"),
            data.get("setup_id"),
            data.get("stint", 0),
            data.get("created_at", time.time()),
        ))
        lap_id = cur.lastrowid

        # Insert compressed trace blob if provided
        trace = data.get("trace")
        if trace and "distance" in trace and len(trace["distance"]) > 0:
            blob = compress_trace(trace)
            cur.execute("""
                INSERT INTO lap_traces (lap_id, num_points, distance_step_m, data)
                VALUES (?, ?, ?, ?)
            """, (lap_id, len(trace["distance"]), 5.0, blob))

    def _exec_discard_after(self, conn: sqlite3.Connection, data: dict[str, Any]):
        session_uid = str(data["session_uid"])
        lap_num = data["lap_num"]
        cur = conn.cursor()
        cur.execute("""
            DELETE FROM laps
            WHERE session_id = (SELECT id FROM sessions WHERE session_uid = ?)
              AND lap_num >= ?
        """, (session_uid, lap_num))

    def _exec_save_overtake(self, conn: sqlite3.Connection, data: dict[str, Any]):
        cur = conn.cursor()
        raw_feat = data.get("raw_features")
        if isinstance(raw_feat, dict):
            raw_feat = json.dumps(raw_feat)
        cur.execute("""
            INSERT INTO overtakes (
                session_uid, lap_num, timestamp, other_vehicle_idx, other_driver_name,
                direction, classification, braking_delta_m, start_gap_m, overlap_at_turn_in,
                ran_deep, contact, forced_off, switchback, corner_name, raw_features
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            str(data["session_uid"]),
            data.get("lap_num", 0),
            data.get("timestamp", time.time()),
            data.get("other_vehicle_idx", 0),
            data.get("other_driver_name", ""),
            data.get("direction", "pass"),
            data.get("classification", "clean"),
            data.get("braking_delta_m"),
            data.get("start_gap_m"),
            1 if data.get("overlap_at_turn_in") else 0,
            1 if data.get("ran_deep") else 0,
            1 if data.get("contact") else 0,
            1 if data.get("forced_off") else 0,
            1 if data.get("switchback") else 0,
            data.get("corner_name", ""),
            raw_feat or "",
        ))

    def _exec_save_timeline_chunk(self, conn: sqlite3.Connection, data: dict[str, Any]):
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO session_timeline (
                session_uid, chunk_index, start_time, end_time, sample_count, data
            ) VALUES (?, ?, ?, ?, ?, ?)
        """, (
            str(data["session_uid"]),
            data.get("chunk_index", 0),
            data.get("start_time", 0.0),
            data.get("end_time", 0.0),
            data.get("sample_count", 0),
            data["data"],
        ))

    # ---- Public Async / Non-blocking Ingestion API -----------------------
    def save_session(self, session_data: dict[str, Any]):
        """Enqueues session metadata creation/update without blocking."""
        if not self._closed:
            self._queue.put(("save_session", dict(session_data)))

    def save_overtake(self, data: dict[str, Any]):
        """Enqueues an overtake record to be saved without blocking."""
        if not self._closed:
            self._queue.put(("save_overtake", dict(data)))

    def save_timeline_chunk(
        self, session_uid: str | int, chunk_index: int, start_time: float,
        end_time: float, sample_count: int, compressed_data: bytes
    ):
        """Enqueues a compressed 10s timeline snapshot chunk without blocking."""
        if not self._closed:
            self._queue.put(("save_timeline_chunk", {
                "session_uid": str(session_uid),
                "chunk_index": chunk_index,
                "start_time": start_time,
                "end_time": end_time,
                "sample_count": sample_count,
                "data": compressed_data,
            }))

    def save_completed_lap(self, lap_data: dict[str, Any], trace: dict[str, np.ndarray] | None = None):
        """Enqueues a completed lap record and its trace blob without blocking."""
        if not self._closed:
            d = dict(lap_data)
            if trace is not None:
                d["trace"] = trace
            self._queue.put(("save_lap", d))

    def discard_laps_after(self, session_uid: str | int, lap_num: int):
        """Discards saved laps on flashback or restart."""
        if not self._closed:
            self._queue.put(("discard_after", {"session_uid": str(session_uid), "lap_num": lap_num}))

    def close(self):
        """Flushes the writer queue and cleanly shuts down the worker thread."""
        if not self._closed:
            self._closed = True
            self._queue.put(None)
            self._writer_thread.join(timeout=3.0)

    # ---- Synchronous Read / Query APIs (WAL Concurrent Reads) ------------
    def _reader_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def get_sessions(
        self,
        track_id: Optional[int] = None,
        formula: Optional[int] = None,
        session_type: Optional[int] = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Queries recent sessions with optional filtering."""
        with self._reader_conn() as conn:
            query = ["SELECT * FROM sessions WHERE 1=1"]
            params: list[Any] = []
            if track_id is not None:
                query.append("AND track_id = ?")
                params.append(track_id)
            if formula is not None:
                query.append("AND formula = ?")
                params.append(formula)
            if session_type is not None:
                query.append("AND session_type = ?")
                params.append(session_type)
            query.append("ORDER BY started_at DESC LIMIT ?")
            params.append(limit)

            cur = conn.cursor()
            cur.execute(" ".join(query), params)
            return [dict(r) for r in cur.fetchall()]

    def get_session_by_id(self, session_id: int) -> Optional[dict[str, Any]]:
        with self._reader_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM sessions WHERE id = ?", (session_id,))
            r = cur.fetchone()
            return dict(r) if r else None

    def get_overtakes(self, session_uid: str | int) -> list[dict[str, Any]]:
        """Retrieves all overtakes for a session_uid."""
        with self._reader_conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT * FROM overtakes
                WHERE session_uid = ?
                ORDER BY timestamp ASC
            """, (str(session_uid),))
            return [dict(r) for r in cur.fetchall()]

    def get_timeline(self, session_uid: str | int) -> list[dict[str, Any]]:
        """Retrieves timeline chunks for a session_uid."""
        with self._reader_conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT * FROM session_timeline
                WHERE session_uid = ?
                ORDER BY chunk_index ASC
            """, (str(session_uid),))
            return [dict(r) for r in cur.fetchall()]

    def get_session_by_uid(self, session_uid: str | int) -> Optional[dict[str, Any]]:
        with self._reader_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM sessions WHERE session_uid = ?", (str(session_uid),))
            r = cur.fetchone()
            return dict(r) if r else None

    def get_laps_for_session(self, session_id: int, valid_only: bool = False) -> list[dict[str, Any]]:
        """Returns all completed laps for a session ordered by lap number."""
        with self._reader_conn() as conn:
            cur = conn.cursor()
            sql = "SELECT * FROM laps WHERE session_id = ?"
            params: list[Any] = [session_id]
            if valid_only:
                sql += " AND is_valid = 1"
            sql += " ORDER BY lap_num ASC"
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]

    def get_lap_by_id(self, lap_id: int) -> Optional[dict[str, Any]]:
        with self._reader_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM laps WHERE id = ?", (lap_id,))
            r = cur.fetchone()
            return dict(r) if r else None

    def get_lap_trace(self, lap_id: int, downsample_points: Optional[int] = None) -> Optional[dict[str, Any]]:
        """Returns the decompressed lap trace dict, optionally downsampled for mobile."""
        with self._reader_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT data FROM lap_traces WHERE lap_id = ?", (lap_id,))
            r = cur.fetchone()
            if not r:
                return None
            trace = decompress_trace(r["data"])

            if downsample_points is not None and len(trace["distance"]) > downsample_points:
                step = int(np.ceil(len(trace["distance"]) / downsample_points))
                return {k: v[::step] for k, v in trace.items()}
            return trace

    def get_personal_best(
        self,
        track_id: int,
        formula: Optional[int] = None,
        session_type: Optional[int] = None,
    ) -> Optional[dict[str, Any]]:
        """Retrieves the overall personal best valid lap for track + car class + session type."""
        with self._reader_conn() as conn:
            cur = conn.cursor()
            query = """
                SELECT l.*, s.track_id, s.formula, s.session_type
                FROM laps l
                JOIN sessions s ON l.session_id = s.id
                WHERE s.track_id = ? AND l.is_valid = 1 AND l.lap_time_s > 0
            """
            params: list[Any] = [track_id]
            if formula is not None:
                query += " AND s.formula = ?"
                params.append(formula)
            if session_type is not None:
                query += " AND s.session_type = ?"
                params.append(session_type)
            query += " ORDER BY l.lap_time_s ASC LIMIT 1"

            cur.execute(query, params)
            r = cur.fetchone()
            if r:
                return dict(r)

            # Fallback to any session type if filtered search has no valid laps
            if session_type is not None:
                cur.execute("""
                    SELECT l.*, s.track_id, s.formula, s.session_type
                    FROM laps l
                    JOIN sessions s ON l.session_id = s.id
                    WHERE s.track_id = ? AND l.is_valid = 1 AND l.lap_time_s > 0
                    ORDER BY l.lap_time_s ASC LIMIT 1
                """, (track_id,))
                fallback = cur.fetchone()
                return dict(fallback) if fallback else None

            return None

    def get_reference_lap(
        self,
        session_id: int,
        track_id: int,
        formula: Optional[int] = None,
        session_type: Optional[int] = None,
        stint: Optional[int] = None,
        mode: str = "session",
        exclude_lap_id: Optional[int] = None,
    ) -> Optional[dict[str, Any]]:
        """Retrieves reference valid lap based on mode: 'session', 'stint', or 'all_time'."""
        with self._reader_conn() as conn:
            cur = conn.cursor()
            if mode == "stint" and stint is not None:
                query = """
                    SELECT l.*, s.track_id, s.formula, s.session_type
                    FROM laps l
                    JOIN sessions s ON l.session_id = s.id
                    WHERE l.session_id = ? AND l.stint = ? AND l.is_valid = 1 AND l.lap_time_s > 0
                """
                params = [session_id, stint]
                if exclude_lap_id:
                    query += " AND l.id != ?"
                    params.append(exclude_lap_id)
                query += " ORDER BY l.lap_time_s ASC LIMIT 1"
                cur.execute(query, params)
                r = cur.fetchone()
                if r:
                    return dict(r)

            if mode in ("session", "stint"):
                query = """
                    SELECT l.*, s.track_id, s.formula, s.session_type
                    FROM laps l
                    JOIN sessions s ON l.session_id = s.id
                    WHERE l.session_id = ? AND l.is_valid = 1 AND l.lap_time_s > 0
                """
                params = [session_id]
                if exclude_lap_id:
                    query += " AND l.id != ?"
                    params.append(exclude_lap_id)
                query += " ORDER BY l.lap_time_s ASC LIMIT 1"
                cur.execute(query, params)
                r = cur.fetchone()
                if r:
                    return dict(r)

            return self.get_personal_best(track_id, formula=formula, session_type=session_type)

    def get_historical_team_sessions(
        self, track_id: int, formula: int, team_id: int, ai_difficulty: int
    ) -> list[dict[str, Any]]:
        """Look up past sessions at this track in this formula with this team at this AI difficulty."""
        with self._reader_conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT s.id, s.session_uid, s.track_id, s.formula, s.team_id, s.ai_difficulty,
                       (SELECT l.position FROM laps l WHERE l.session_id = s.id ORDER BY l.lap_num DESC LIMIT 1) as position
                FROM sessions s
                WHERE s.track_id = ? AND s.formula = ? AND s.team_id = ? AND s.ai_difficulty = ?
                ORDER BY s.started_at DESC
            """, (track_id, formula, team_id, ai_difficulty))
            rows = cur.fetchall()
            return [dict(r) for r in rows if r["position"] is not None]

    def record_session_teammate_delta(self, session_uid: str, ai_difficulty: int, team_id: int, delta_s: float):
        """Records average teammate delta for AI difficulty tracking."""
        with self._reader_conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS session_teammate_deltas (
                    session_uid TEXT PRIMARY KEY,
                    ai_difficulty INTEGER NOT NULL,
                    team_id INTEGER NOT NULL,
                    delta_s REAL NOT NULL,
                    created_at REAL NOT NULL
                );
            """)
            cur.execute("""
                INSERT INTO session_teammate_deltas (session_uid, ai_difficulty, team_id, delta_s, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(session_uid) DO UPDATE SET delta_s = excluded.delta_s;
            """, (str(session_uid), ai_difficulty, team_id, delta_s, time.time()))
            conn.commit()

    def get_ai_difficulty_history(self, ai_difficulty: int, team_id: int) -> list[dict[str, Any]]:
        """Returns past sessions' teammate lap deltas for the specified AI difficulty and team."""
        with self._reader_conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS session_teammate_deltas (
                    session_uid TEXT PRIMARY KEY,
                    ai_difficulty INTEGER NOT NULL,
                    team_id INTEGER NOT NULL,
                    delta_s REAL NOT NULL,
                    created_at REAL NOT NULL
                );
            """)
            cur.execute("""
                SELECT * FROM session_teammate_deltas
                WHERE ai_difficulty = ? AND team_id = ?
                ORDER BY created_at DESC
            """, (ai_difficulty, team_id))
            return [dict(r) for r in cur.fetchall()]


class LapTraceCollector:
    """Collects player telemetry throughout a lap, handles flashbacks, and produces resampled traces."""

    def __init__(self, storage: SessionStorage):
        self.storage = storage
        self.current_lap_num: Optional[int] = None
        self.current_session_uid: Optional[str] = None
        self._raw_samples: list[tuple] = []
        self._last_distance: float = 0.0
        self._fuel_start: Optional[float] = None
        self._wear_start: Optional[float] = None

    def on_session_packet(self, state):
        """Updates session record in storage when session packet is received."""
        s = state.session
        if s and state.session_uid:
            self.storage.save_session({
                "session_uid": state.session_uid,
                "game_format": 2025,
                "formula": s.formula,
                "track_id": s.track_id,
                "session_type": s.session_type,
                "team_id": state.my_team_id,
                "weather_at_start": s.weather,
                "ai_difficulty": getattr(s, "ai_difficulty", 0),
                "started_at": time.time(),
            })

    def on_tick(self, state):
        """Records a 20 Hz sample for the player car."""
        me = state.me
        tel = state.my_tel
        motion = state.my_motion
        st = state.my_status
        dmg = state.my_damage

        if me is None or tel is None:
            return

        # Initialize session/lap tracking
        if self.current_session_uid != state.session_uid:
            self.current_session_uid = state.session_uid
            self.current_lap_num = me.lap_num
            self._raw_samples.clear()
            self._last_distance = 0.0
            self._fuel_start = st.fuel_in_tank if st else None
            self._wear_start = max(dmg.tyre_wear) if dmg and dmg.tyre_wear else 0.0

        if self.current_lap_num != me.lap_num:
            # Lap change: if lap was completed, it is handled via on_lap_completed
            self.current_lap_num = me.lap_num
            self._raw_samples.clear()
            self._last_distance = 0.0
            self._fuel_start = st.fuel_in_tank if st else None
            self._wear_start = max(dmg.tyre_wear) if dmg and dmg.tyre_wear else 0.0

        dist = me.lap_distance
        if dist < 0.0:
            return

        # Check for in-lap flashback (player jumped backwards in distance on the same lap)
        if dist < self._last_distance - 25.0:
            self._raw_samples = [s for s in self._raw_samples if s[0] <= dist]

        self._last_distance = dist
        x = motion.x if motion else 0.0
        z = motion.z if motion else 0.0

        sample = (
            dist,
            float(tel.speed_kph),
            float(tel.throttle),
            float(tel.brake),
            float(tel.steer),
            float(tel.gear),
            float(tel.rpm),
            float(x),
            float(z),
        )
        self._raw_samples.append(sample)

    def on_lap_completed(self, rec, state):
        """Processes completed lap, resamples trace at 5m steps, and saves to storage."""
        s = state.session
        track_len = s.track_length_m if s else 5000.0
        trace = resample_lap_trace(self._raw_samples, track_len, distance_step=5.0)

        st = state.my_status
        dmg = state.my_damage
        fuel_end = st.fuel_in_tank if st else None
        wear_end = max(dmg.tyre_wear) if dmg and dmg.tyre_wear else 0.0

        lap_data = {
            "session_uid": state.session_uid,
            "game_format": 2025,
            "formula": s.formula if s else 0,
            "track_id": s.track_id if s else 0,
            "session_type": s.session_type if s else 0,
            "team_id": state.my_team_id,
            "lap_num": rec.lap,
            "lap_time_s": rec.lap_time_ms / 1000.0,
            "sector1_s": rec.sector1_ms / 1000.0 if rec.sector1_ms > 0 else None,
            "sector2_s": rec.sector2_ms / 1000.0 if rec.sector2_ms > 0 else None,
            "sector3_s": rec.sector3_ms / 1000.0 if rec.sector3_ms > 0 else None,
            "is_valid": not rec.invalid,
            "position": rec.position,
            "tyre_compound": st.actual_compound if st else None,
            "tyre_age": st.tyre_age_laps if st else None,
            "fuel_start_kg": self._fuel_start,
            "fuel_end_kg": fuel_end,
            "tyre_wear_start_pct": self._wear_start,
            "tyre_wear_end_pct": wear_end,
            "setup_id": str(getattr(state, "current_setup_id", None)) if getattr(state, "current_setup_id", None) is not None else getattr(state, "current_setup_fingerprint", None),
            "stint": rec.stint,
            "created_at": time.time(),
        }

        self.storage.save_completed_lap(lap_data, trace)

        # Reset state for the new lap
        self._raw_samples.clear()
        self._last_distance = 0.0
        self._fuel_start = fuel_end
        self._wear_start = wear_end

    def on_flashback(self, lap_num: int, session_uid: str | int):
        """Rolls back storage and discards current buffer on flashback."""
        self._raw_samples.clear()
        self._last_distance = 0.0
        self.storage.discard_laps_after(session_uid, lap_num)
