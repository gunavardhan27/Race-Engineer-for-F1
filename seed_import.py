"""Seed reference data importer for F1 2026 Race Engineer (Prompt 25).

Imports and manages:
1. Tracks and game track IDs from data/tracks.json into tracks.db
2. 275 setups from data/setups_seed.json into setups.db (with validation flags and 25 derived base setups)
3. 25 track notes from data/knowledge/tracks/*.md into knowledge/tracks/ (preserving user edits)
4. Turn-by-turn corner features from data/track_turns.json into tracks.db + TrackModel corner matcher
5. Persona lines from data/lines/cocky.yaml merged into lines/cocky.yaml
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import statistics
import time
from typing import Any, Optional
import yaml

from setups import compute_setup_fingerprint, SetupsStorage
from trackmodel import TrackModel, Corner

log = logging.getLogger("engineer.seed_import")

F1_TEAMS_2026 = [
    "Red Bull",
    "Ferrari",
    "McLaren",
    "Mercedes",
    "Aston Martin",
    "Williams",
    "Alpine",
    "RB",
    "Haas",
    "Audi",
    "Cadillac",
]


def find_seed_data_dir() -> Path:
    """Finds the directory containing seed data files."""
    candidates = [
        Path("antigravity_pack/data"),
        Path("race_engineer_antigravity_pack/data"),
        Path(__file__).parent / "antigravity_pack" / "data",
        Path(__file__).parent / "race_engineer_antigravity_pack" / "data",
        Path("data"),
    ]
    for c in candidates:
        if c.is_dir() and (c / "tracks.json").is_file():
            return c
    return Path("antigravity_pack/data")


class SeedImporter:
    def __init__(
        self,
        data_dir: Path | str | None = None,
        tracks_db_path: str = "tracks.db",
        setups_db_path: str = "setups.db",
        knowledge_tracks_dir: str = "knowledge/tracks",
        lines_path: str = "lines/cocky.yaml",
    ):
        self.data_dir = Path(data_dir) if data_dir else find_seed_data_dir()
        self.tracks_db_path = tracks_db_path
        self.setups_db_path = setups_db_path
        self.knowledge_tracks_dir = Path(knowledge_tracks_dir)
        self.lines_path = Path(lines_path)

        self._init_tracks_db()
        self._init_setups_db()

    def _init_tracks_db(self) -> None:
        """Initializes tracks, team mappings, and track turns tables in tracks.db."""
        with sqlite3.connect(self.tracks_db_path) as conn:
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS tracks (
                    slug TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    aliases TEXT NOT NULL,
                    game_track_id INTEGER,
                    game_track_id_status TEXT DEFAULT '',
                    confirmed INTEGER DEFAULT 0,
                    length_m REAL DEFAULT 0,
                    turn_count INTEGER DEFAULT 0,
                    updated_at REAL
                );

                CREATE TABLE IF NOT EXISTS team_mappings (
                    team_id INTEGER PRIMARY KEY,
                    team_name TEXT NOT NULL,
                    confirmed INTEGER DEFAULT 0,
                    updated_at REAL
                );

                CREATE TABLE IF NOT EXISTS track_turns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    track_slug TEXT NOT NULL,
                    turn_order INTEGER NOT NULL,
                    sector INTEGER NOT NULL,
                    label TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    turn_first INTEGER,
                    turn_last INTEGER,
                    name TEXT,
                    speed_profile TEXT,
                    corner_type TEXT,
                    notes TEXT,
                    mentions_drs INTEGER DEFAULT 0,
                    matched_corner_number INTEGER,
                    match_confirmed INTEGER DEFAULT 0,
                    updated_at REAL,
                    UNIQUE(track_slug, turn_order)
                );

                CREATE TABLE IF NOT EXISTS seed_meta (
                    key TEXT PRIMARY KEY,
                    val TEXT
                );
            """)

    def _init_setups_db(self) -> None:
        """Ensures setups table in setups.db has validation and derived metadata columns."""
        storage = SetupsStorage(self.setups_db_path)
        with sqlite3.connect(self.setups_db_path) as conn:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(setups)").fetchall()]
            if "validation_flags" not in cols:
                conn.execute("ALTER TABLE setups ADD COLUMN validation_flags TEXT DEFAULT '[]'")
            if "diff_from_base" not in cols:
                conn.execute("ALTER TABLE setups ADD COLUMN diff_from_base TEXT DEFAULT '{}'")
            if "team_name" not in cols:
                conn.execute("ALTER TABLE setups ADD COLUMN team_name TEXT DEFAULT ''")
            if "track_slug" not in cols:
                conn.execute("ALTER TABLE setups ADD COLUMN track_slug TEXT DEFAULT ''")

    # -------------------------------------------------------------------------
    # 1. Tracks & Team IDs Import
    # -------------------------------------------------------------------------
    def import_tracks(self, reimport: bool = False) -> dict[str, Any]:
        """Imports 25 tracks from tracks.json into tracks.db."""
        tracks_file = self.data_dir / "tracks.json"
        if not tracks_file.is_file():
            return {"error": f"tracks.json not found at {tracks_file}", "count": 0}

        with open(tracks_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        tracks_list = data.get("tracks", [])
        imported = 0
        now = time.time()

        with sqlite3.connect(self.tracks_db_path) as conn:
            cur = conn.cursor()
            for t in tracks_list:
                slug = t.get("slug", "")
                name = t.get("name", "")
                aliases = json.dumps(t.get("aliases", []))
                game_id = t.get("game_track_id")
                status = t.get("game_track_id_status", "")

                # Verified if status says verified / confirmed and game_id is not null
                confirmed = 1 if (game_id is not None and "verify" not in status.lower() and "unknown" not in status.lower()) else 0

                cur.execute("SELECT slug, confirmed, game_track_id FROM tracks WHERE slug = ?", (slug,))
                row = cur.fetchone()

                if row is None:
                    cur.execute("""
                        INSERT INTO tracks (slug, name, aliases, game_track_id, game_track_id_status, confirmed, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (slug, name, aliases, game_id, status, confirmed, now))
                    imported += 1
                elif reimport:
                    # Update metadata but preserve existing user confirmation if present
                    prev_confirmed = row[1]
                    prev_game_id = row[2]
                    final_game_id = prev_game_id if (prev_confirmed and prev_game_id is not None) else game_id
                    final_confirmed = max(prev_confirmed, confirmed)
                    cur.execute("""
                        UPDATE tracks
                        SET name = ?, aliases = ?, game_track_id = ?, game_track_id_status = ?, confirmed = ?, updated_at = ?
                        WHERE slug = ?
                    """, (name, aliases, final_game_id, status, final_confirmed, now, slug))
                    imported += 1

            conn.commit()

        return {"count": imported, "total": len(tracks_list)}

    def confirm_track_mapping(self, slug: str, game_track_id: int) -> bool:
        """Confirms game track ID for a track slug."""
        now = time.time()
        with sqlite3.connect(self.tracks_db_path) as conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE tracks
                SET game_track_id = ?, confirmed = 1, updated_at = ?
                WHERE slug = ?
            """, (game_track_id, now, slug))
            conn.commit()
            return cur.rowcount > 0

    def confirm_team_mapping(self, team_id: int, team_name: str) -> bool:
        """Confirms team name for a team ID."""
        now = time.time()
        with sqlite3.connect(self.tracks_db_path) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO team_mappings (team_id, team_name, confirmed, updated_at)
                VALUES (?, ?, 1, ?)
            """, (team_id, team_name, now))
            conn.commit()
            return True

    def check_track_mapping(self, game_track_id: int) -> dict[str, Any]:
        """Checks if a game track ID is known and confirmed."""
        with sqlite3.connect(self.tracks_db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("SELECT * FROM tracks WHERE game_track_id = ?", (game_track_id,))
            row = cur.fetchone()
            if row and row["confirmed"] == 1:
                return {"known": True, "confirmed": True, "slug": row["slug"], "name": row["name"]}
            elif row:
                return {
                    "known": True,
                    "confirmed": False,
                    "slug": row["slug"],
                    "name": row["name"],
                    "prompt": f"Is track id {game_track_id} {row['name']}?",
                    "options": self.get_all_track_names(),
                }
            else:
                return {
                    "known": False,
                    "confirmed": False,
                    "prompt": f"Which track is track id {game_track_id}?",
                    "options": self.get_all_track_names(),
                }

    def check_team_mapping(self, team_id: int) -> dict[str, Any]:
        """Checks if a team ID is mapped and confirmed."""
        with sqlite3.connect(self.tracks_db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("SELECT * FROM team_mappings WHERE team_id = ?", (team_id,))
            row = cur.fetchone()
            if row and row["confirmed"] == 1:
                return {"known": True, "confirmed": True, "team_name": row["team_name"]}
            else:
                return {
                    "known": False,
                    "confirmed": False,
                    "prompt": "Which team are you driving?",
                    "options": F1_TEAMS_2026,
                }

    def get_all_track_names(self) -> list[str]:
        """Returns list of all 25 track names."""
        with sqlite3.connect(self.tracks_db_path) as conn:
            rows = conn.execute("SELECT name FROM tracks ORDER BY name ASC").fetchall()
            if rows:
                return [r[0] for r in rows]
        return [
            "Abu Dhabi", "Australia", "Austria", "Azerbaijan", "Bahrain",
            "Belgium", "Brazil", "Canada", "China", "Great Britain",
            "Hungary", "Imola", "Italy (Monza)", "Japan", "Las Vegas",
            "Madrid", "Mexico", "Miami", "Monaco", "Netherlands",
            "Qatar", "Saudi Arabia", "Singapore", "Spain (Catalunya)", "United States (Austin)"
        ]

    # -------------------------------------------------------------------------
    # 2. Setups Import (275 Setups + 25 Derived Base Setups)
    # -------------------------------------------------------------------------
    def validate_setup_params(self, params: dict[str, Any]) -> list[str]:
        """Validates params against game boundaries and DATA_REVIEW notes. Never modifies values."""
        flags: list[str] = []

        fl_press = params.get("front_left_tyre_pressure")
        fr_press = params.get("front_right_tyre_pressure")
        if (fl_press is not None and fl_press < 22.5) or (fr_press is not None and fr_press < 22.5):
            val = fl_press or fr_press
            flags.append(f"Front tyre pressure ({val} psi) may be below game minimum (22.5 psi)")

        rl_press = params.get("rear_left_tyre_pressure")
        rr_press = params.get("rear_right_tyre_pressure")
        if (rl_press is not None and rl_press < 20.5) or (rr_press is not None and rr_press < 20.5):
            val = rl_press or rr_press
            flags.append(f"Rear tyre pressure ({val} psi) may be below game minimum (20.5 psi)")

        # Normal sanity bounds
        bounds = {
            "front_wing": (0, 50),
            "rear_wing": (0, 50),
            "brake_bias": (50, 70),
            "brake_pressure": (50, 100),
            "on_throttle": (50, 100),
            "off_throttle": (50, 100),
        }
        for k, (b_min, b_max) in bounds.items():
            if k in params and params[k] is not None:
                v = params[k]
                if v < b_min or v > b_max:
                    flags.append(f"{k} ({v}) is outside typical range ({b_min}-{b_max})")

        return flags

    def compute_base_params(self, setups_params: list[dict[str, Any]]) -> dict[str, Any]:
        """Computes median parameter setup across 11 teams."""
        if not setups_params:
            return {}

        all_keys: set[str] = set()
        for p in setups_params:
            all_keys.update(p.keys())

        base: dict[str, Any] = {}
        float_1_keys = {"front_camber", "rear_camber", "front_left_tyre_pressure", "front_right_tyre_pressure", "rear_left_tyre_pressure", "rear_right_tyre_pressure"}
        float_2_keys = {"front_toe", "rear_toe"}

        for k in sorted(all_keys):
            vals = [p[k] for p in setups_params if k in p and p[k] is not None]
            if not vals:
                continue
            med = statistics.median(vals)
            if k in float_1_keys:
                base[k] = round(med, 1)
            elif k in float_2_keys:
                base[k] = round(med, 2)
            else:
                base[k] = int(round(med))

        return base

    def import_setups(self, reimport: bool = False) -> dict[str, Any]:
        """Imports 275 seed setups + 25 derived base setups with validation and deduplication."""
        setups_file = self.data_dir / "setups_seed.json"
        if not setups_file.is_file():
            return {"error": f"setups_seed.json not found at {setups_file}", "imported": 0}

        with open(setups_file, "r", encoding="utf-8") as f:
            raw_data = json.load(f)

        seed_list = raw_data.get("setups", [])
        if not seed_list:
            return {"imported": 0, "base_count": 0}

        # Track slug -> game track ID map
        track_id_map: dict[str, int] = {}
        with sqlite3.connect(self.tracks_db_path) as conn:
            for row in conn.execute("SELECT slug, game_track_id FROM tracks").fetchall():
                if row[1] is not None:
                    track_id_map[row[0]] = row[1]

        # Group by track_slug
        by_track: dict[str, list[dict[str, Any]]] = {}
        for s in seed_list:
            slug = s.get("track_slug", "unknown")
            by_track.setdefault(slug, []).append(s)

        imported_count = 0
        base_count = 0
        total_flags_count = 0
        now = time.time()

        with sqlite3.connect(self.setups_db_path) as conn:
            cur = conn.cursor()

            if reimport:
                # Remove previously imported seed setups and derived setups
                cur.execute("DELETE FROM setups WHERE source_note LIKE '%F1_26_Complete_Matrix_275_Setups%' OR source = 'derived'")
                conn.commit()

            for slug, team_setups in by_track.items():
                track_name = team_setups[0].get("track", slug.title())
                track_id = track_id_map.get(slug, 0)

                # 1. Compute Base setup across teams
                base_params = self.compute_base_params([s.get("params", {}) for s in team_setups])
                base_fp = compute_setup_fingerprint(base_params)
                base_flags = self.validate_setup_params(base_params)
                total_flags_count += len(base_flags)

                # Insert Base setup
                cur.execute("SELECT id FROM setups WHERE fingerprint = ?", (base_fp,))
                existing_base = cur.fetchone()
                if not existing_base:
                    cur.execute("""
                        INSERT INTO setups (
                            fingerprint, car_class, team_id, track_id, conditions,
                            session_type, game_format, source, source_note, source_link,
                            notes, name, created_at, params, schema_version,
                            validation_flags, diff_from_base, team_name, track_slug
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
                    """, (
                        base_fp, 13, 0, track_id, "dry",
                        "any", 2026, "derived",
                        "Derived median across 11 teams from imported spreadsheet (untested in game)",
                        "", "Base setup computed as parameter median across teams.",
                        f"{track_name} Base Setup (Derived)", now,
                        json.dumps(base_params, sort_keys=True),
                        json.dumps(base_flags), "{}", "Base", slug
                    ))
                    base_count += 1

                # 2. Insert each Team setup with diff from base
                for s in team_setups:
                    team_name = s.get("team", "Unknown")
                    params = s.get("params", {})
                    fp = compute_setup_fingerprint(params)
                    flags = self.validate_setup_params(params)
                    total_flags_count += len(flags)

                    # Compute small diff from base
                    diff: dict[str, float] = {}
                    for k, val in params.items():
                        if k in base_params:
                            d = round(val - base_params[k], 2)
                            if abs(d) > 1e-4:
                                diff[k] = d

                    cur.execute("SELECT id FROM setups WHERE fingerprint = ?", (fp,))
                    if cur.fetchone() is None:
                        cur.execute("""
                            INSERT INTO setups (
                                fingerprint, car_class, team_id, track_id, conditions,
                                session_type, game_format, source, source_note, source_link,
                                notes, name, created_at, params, schema_version,
                                validation_flags, diff_from_base, team_name, track_slug
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
                        """, (
                            fp, 13, 0, track_id, "dry",
                            "any", 2026, "imported",
                            s.get("source_note", "User spreadsheet F1_26_Complete_Matrix_275_Setups.xlsx (untested in game)"),
                            "", f"Missing params: {', '.join(s.get('missing_params', []))}",
                            f"{team_name} - {track_name} (Imported)", now,
                            json.dumps(params, sort_keys=True),
                            json.dumps(flags), json.dumps(diff), team_name, slug
                        ))
                        imported_count += 1

            conn.commit()

        # Update metadata
        with sqlite3.connect(self.tracks_db_path) as conn:
            conn.execute("INSERT OR REPLACE INTO seed_meta (key, val) VALUES ('last_import_time', ?)", (str(now),))
            conn.execute("INSERT OR REPLACE INTO seed_meta (key, val) VALUES ('setups_imported', ?)", (str(imported_count + base_count),))
            conn.execute("INSERT OR REPLACE INTO seed_meta (key, val) VALUES ('flags_count', ?)", (str(total_flags_count),))
            conn.commit()

        return {
            "imported": imported_count,
            "base_count": base_count,
            "total_setups": len(seed_list),
            "flags_count": total_flags_count,
        }

    # -------------------------------------------------------------------------
    # 3. Track Notes Import (Merge into knowledge/tracks/ without overwriting user text)
    # -------------------------------------------------------------------------
    def parse_markdown_sections(self, text: str) -> dict[str, str]:
        """Splits markdown into title -> body mapping."""
        sections: dict[str, str] = {}
        current_title = "Header"
        current_lines: list[str] = []

        for line in text.splitlines():
            if line.startswith("## "):
                if current_lines:
                    sections[current_title.strip()] = "\n".join(current_lines).strip()
                    current_lines = []
                current_title = line[3:].strip()
            else:
                current_lines.append(line)

        if current_lines:
            sections[current_title.strip()] = "\n".join(current_lines).strip()

        return sections

    def merge_track_note(self, dest_path: Path, seed_text: str) -> str:
        """Merges seed markdown into existing file without overwriting user text."""
        if not dest_path.is_file():
            return seed_text

        existing_text = dest_path.read_text(encoding="utf-8")
        existing_sections = self.parse_markdown_sections(existing_text)
        seed_sections = self.parse_markdown_sections(seed_text)

        # Merge sections
        merged_sections = dict(existing_sections)
        for s_title, s_body in seed_sections.items():
            if s_title not in merged_sections:
                merged_sections[s_title] = s_body
            elif s_title.lower() == "corners":
                # Merge missing turn lines
                exist_body = merged_sections[s_title]
                exist_turns = set(re.findall(r"(?:T|Turn\s*)(\d+)", exist_body, flags=re.I))
                new_lines = []
                for line in s_body.splitlines():
                    t_nums = re.findall(r"(?:T|Turn\s*)(\d+)", line, flags=re.I)
                    if t_nums and not any(tn in exist_turns for tn in t_nums):
                        new_lines.append(line)
                if new_lines:
                    merged_sections[s_title] = exist_body.rstrip() + "\n" + "\n".join(new_lines)

        # Build output markdown
        parts: list[str] = []
        if "Header" in merged_sections:
            parts.append(merged_sections["Header"])

        for title, body in merged_sections.items():
            if title == "Header":
                continue
            parts.append(f"## {title}\n{body}")

        return "\n\n".join(parts) + "\n"

    def import_track_notes(self, reimport: bool = False) -> dict[str, Any]:
        """Imports 25 track notes into knowledge/tracks/ with non-destructive merge."""
        seed_tracks_dir = self.data_dir / "knowledge" / "tracks"
        if not seed_tracks_dir.is_dir():
            return {"error": f"Seed tracks dir not found at {seed_tracks_dir}", "count": 0}

        self.knowledge_tracks_dir.mkdir(parents=True, exist_ok=True)
        count = 0

        # Mapping for known aliases e.g. great_britain -> silverstone
        alias_map = {
            "great_britain.md": "silverstone.md",
            "italy_monza.md": "monza.md",
            "belgium.md": "spa.md",
        }

        for seed_file in seed_tracks_dir.glob("*.md"):
            seed_text = seed_file.read_text(encoding="utf-8")

            # Check if file or alias already exists
            dest_file = self.knowledge_tracks_dir / seed_file.name
            alias_name = alias_map.get(seed_file.name)
            if alias_name and (self.knowledge_tracks_dir / alias_name).is_file():
                dest_file = self.knowledge_tracks_dir / alias_name

            if dest_file.is_file() and not reimport:
                # Merge missing sections without touching user edits
                merged = self.merge_track_note(dest_file, seed_text)
                dest_file.write_text(merged, encoding="utf-8")
            else:
                dest_file.write_text(seed_text, encoding="utf-8")

            count += 1

        return {"count": count, "dir": str(self.knowledge_tracks_dir)}

    # -------------------------------------------------------------------------
    # 4. Turn-by-Turn Structured Features & Corner Matcher
    # -------------------------------------------------------------------------
    def import_track_turns(self, reimport: bool = False) -> dict[str, Any]:
        """Imports key turns from track_turns.json into track_turns table in tracks.db."""
        turns_file = self.data_dir / "track_turns.json"
        if not turns_file.is_file():
            return {"error": f"track_turns.json not found at {turns_file}", "count": 0}

        with open(turns_file, "r", encoding="utf-8") as f:
            raw = json.load(f)

        features = raw.get("features", [])
        now = time.time()
        imported = 0

        with sqlite3.connect(self.tracks_db_path) as conn:
            cur = conn.cursor()
            if reimport:
                cur.execute("DELETE FROM track_turns WHERE match_confirmed = 0")

            for feat in features:
                slug = feat.get("track_slug", "")
                order = int(feat.get("order", 0))
                sector = int(feat.get("sector", 1))
                label = feat.get("label", "")
                kind = feat.get("kind", "corner")
                t_first = feat.get("turn_first")
                t_last = feat.get("turn_last")
                name = feat.get("name")
                spd = feat.get("speed_profile", "")
                c_type = feat.get("corner_type", "")
                notes = feat.get("notes", "")
                mentions_drs = 1 if feat.get("mentions_drs") else 0

                cur.execute("""
                    INSERT INTO track_turns (
                        track_slug, turn_order, sector, label, kind, turn_first, turn_last,
                        name, speed_profile, corner_type, notes, mentions_drs, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(track_slug, turn_order) DO UPDATE SET
                        sector = excluded.sector,
                        label = excluded.label,
                        kind = excluded.kind,
                        name = excluded.name,
                        speed_profile = excluded.speed_profile,
                        corner_type = excluded.corner_type,
                        notes = excluded.notes,
                        mentions_drs = excluded.mentions_drs,
                        updated_at = excluded.updated_at
                """, (
                    slug, order, sector, label, kind, t_first, t_last,
                    name, spd, c_type, notes, mentions_drs, now
                ))
                imported += 1

            conn.commit()

        return {"count": imported, "total_features": len(features)}

    def match_turns_to_track_model(
        self,
        track_slug: str,
        track_model: TrackModel,
        sector2_start_m: float = 0.0,
        sector3_start_m: float = 0.0,
    ) -> list[dict[str, Any]]:
        """Matches official key turns from track_turns to TrackModel corners using sector, order, and speeds."""
        with sqlite3.connect(self.tracks_db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM track_turns WHERE track_slug = ? ORDER BY turn_order ASC",
                (track_slug,)
            ).fetchall()
        turns = [dict(r) for r in rows]
        if not turns or not track_model.corners:
            return turns

        length = getattr(track_model, "track_length_m", getattr(track_model, "length_m", 5000.0))
        s2 = sector2_start_m if sector2_start_m > 0 else (length * 0.33)
        s3 = sector3_start_m if sector3_start_m > 0 else (length * 0.66)

        def corner_sector(c: Corner) -> int:
            apex_d = getattr(c, "apex_dist_m", getattr(c, "apex_m", 0.0))
            if apex_d < s2:
                return 1
            elif apex_d < s3:
                return 2
            return 3

        corners_by_sector: dict[int, list[Corner]] = {1: [], 2: [], 3: []}
        for c in track_model.corners:
            corners_by_sector[corner_sector(c)].append(c)

        now = time.time()
        matches: list[dict[str, Any]] = []

        with sqlite3.connect(self.tracks_db_path) as conn:
            cur = conn.cursor()

            for sec in (1, 2, 3):
                sec_turns = [t for t in turns if t["sector"] == sec and t["kind"] == "corner"]
                sec_corners = corners_by_sector[sec]

                if not sec_corners:
                    continue

                for idx, t in enumerate(sec_turns):
                    if t.get("match_confirmed"):
                        continue

                    # Match by relative order in sector
                    c_idx = int(round(idx * (len(sec_corners) - 1) / max(1, len(sec_turns) - 1)))
                    c_idx = max(0, min(len(sec_corners) - 1, c_idx))
                    matched_c = sec_corners[c_idx]

                    # If turn has Low speed profile, find closest low speed corner nearby
                    if t.get("speed_profile") == "Low" and len(sec_corners) > 1:
                        low_corners = sorted(
                            sec_corners,
                            key=lambda c: getattr(c, "field_median_min_speed_kph", getattr(c, "apex_speed_kph", 100.0))
                        )
                        matched_c = low_corners[0]

                    t["matched_corner_number"] = matched_c.number
                    matches.append(t)

                    cur.execute("""
                        UPDATE track_turns
                        SET matched_corner_number = ?, updated_at = ?
                        WHERE id = ?
                    """, (matched_c.number, now, t["id"]))

                    # If turn has a proper name (e.g. Copse), apply to track model corner
                    if t.get("name"):
                        matched_c.name = t["name"]

            conn.commit()

        return turns

    def confirm_turn_match(self, track_slug: str, turn_id: int, corner_number: int) -> bool:
        """Confirms or overrides a user-selected corner match."""
        now = time.time()
        with sqlite3.connect(self.tracks_db_path) as conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE track_turns
                SET matched_corner_number = ?, match_confirmed = 1, updated_at = ?
                WHERE id = ? AND track_slug = ?
            """, (corner_number, now, turn_id, track_slug))
            conn.commit()
            return cur.rowcount > 0

    def get_track_turns(self, track_slug: str) -> list[dict[str, Any]]:
        """Returns turn list with current matches for the given track slug."""
        with sqlite3.connect(self.tracks_db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM track_turns WHERE track_slug = ? ORDER BY turn_order ASC",
                (track_slug,)
            ).fetchall()
            return [dict(r) for r in rows]

    # -------------------------------------------------------------------------
    # 5. Persona Lines Merge
    # -------------------------------------------------------------------------
    def merge_persona_lines(self) -> dict[str, Any]:
        """Merges missing events and lines from seed lines/cocky.yaml into lines/cocky.yaml."""
        seed_lines_file = self.data_dir / "lines" / "cocky.yaml"
        if not seed_lines_file.is_file() or not self.lines_path.is_file():
            return {"merged": False, "reason": "lines file missing"}

        with open(seed_lines_file, "r", encoding="utf-8") as f:
            seed_data = yaml.safe_load(f) or {}

        with open(self.lines_path, "r", encoding="utf-8") as f:
            proj_data = yaml.safe_load(f) or {}

        added_events = 0
        added_lines = 0

        for ev, lines in seed_data.items():
            if not isinstance(lines, list):
                continue
            if ev not in proj_data:
                proj_data[ev] = lines
                added_events += 1
            else:
                existing_texts = {l.get("text", "") if isinstance(l, dict) else str(l) for l in proj_data[ev]}
                for l in lines:
                    txt = l.get("text", "") if isinstance(l, dict) else str(l)
                    if txt and txt not in existing_texts:
                        proj_data[ev].append(l)
                        added_lines += 1

        # Write merged yaml
        with open(self.lines_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(proj_data, f, sort_keys=False, allow_unicode=True)

        return {"merged": True, "added_events": added_events, "added_lines": added_lines}

    # -------------------------------------------------------------------------
    # Master Import & Status
    # -------------------------------------------------------------------------
    def import_all(self, reimport: bool = False) -> dict[str, Any]:
        """Runs complete seed data import workflow."""
        r_tracks = self.import_tracks(reimport=reimport)
        r_setups = self.import_setups(reimport=reimport)
        r_notes = self.import_track_notes(reimport=reimport)
        r_turns = self.import_track_turns(reimport=reimport)
        r_lines = self.merge_persona_lines()

        return {
            "tracks": r_tracks,
            "setups": r_setups,
            "track_notes": r_notes,
            "track_turns": r_turns,
            "persona_lines": r_lines,
        }

    def get_status(self) -> dict[str, Any]:
        """Returns import status, setup counts, flags, and unconfirmed items for Settings UI."""
        with sqlite3.connect(self.tracks_db_path) as conn:
            conn.row_factory = sqlite3.Row
            tracks_total = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
            tracks_confirmed = conn.execute("SELECT COUNT(*) FROM tracks WHERE confirmed = 1").fetchone()[0]
            unconfirmed_tracks = [
                dict(r) for r in conn.execute(
                    "SELECT slug, name, game_track_id FROM tracks WHERE confirmed = 0 ORDER BY name ASC"
                ).fetchall()
            ]
            turns_total = conn.execute("SELECT COUNT(*) FROM track_turns").fetchone()[0]

            meta = dict(conn.execute("SELECT key, val FROM seed_meta").fetchall())

        with sqlite3.connect(self.setups_db_path) as conn:
            conn.row_factory = sqlite3.Row
            setups_total = conn.execute("SELECT COUNT(*) FROM setups").fetchone()[0]
            base_setups_total = conn.execute("SELECT COUNT(*) FROM setups WHERE source = 'derived'").fetchone()[0]
            imported_setups_total = conn.execute("SELECT COUNT(*) FROM setups WHERE source = 'imported'").fetchone()[0]

        return {
            "tracks_total": tracks_total,
            "tracks_confirmed": tracks_confirmed,
            "unconfirmed_tracks": unconfirmed_tracks,
            "turns_total": turns_total,
            "setups_total": setups_total,
            "imported_setups_total": imported_setups_total,
            "base_setups_total": base_setups_total,
            "last_import_time": meta.get("last_import_time"),
            "flags_count": int(meta.get("flags_count", 0)),
        }

    # -------------------------------------------------------------------------
    # Setup Query Intent Helper
    # -------------------------------------------------------------------------
    def get_setup_for_track_and_team(
        self,
        track_identifier: str | int,
        team_name: Optional[str] = None,
        car_setup_params: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Finds the sheet setup for a track and team (falls back to Base setup).

        Returns comparison in clicks against current car setup.
        """
        # Resolve track_slug
        slug = str(track_identifier).lower()
        with sqlite3.connect(self.tracks_db_path) as conn:
            conn.row_factory = sqlite3.Row
            if isinstance(track_identifier, int) or str(track_identifier).isdigit():
                r = conn.execute("SELECT slug, name FROM tracks WHERE game_track_id = ?", (int(track_identifier),)).fetchone()
            else:
                r = conn.execute("SELECT slug, name FROM tracks WHERE slug = ? OR name LIKE ?", (slug, f"%{slug}%")).fetchone()

            if r:
                slug = r["slug"]
                track_name = r["name"]
            else:
                track_name = slug.title()

        with sqlite3.connect(self.setups_db_path) as conn:
            conn.row_factory = sqlite3.Row

            target_row = None
            if team_name:
                target_row = conn.execute(
                    "SELECT * FROM setups WHERE track_slug = ? AND team_name LIKE ? AND source = 'imported'",
                    (slug, f"%{team_name}%")
                ).fetchone()

            # Fall back to Base setup
            if not target_row:
                target_row = conn.execute(
                    "SELECT * FROM setups WHERE track_slug = ? AND source = 'derived'",
                    (slug,)
                ).fetchone()

            if not target_row:
                return {
                    "found": False,
                    "text": f"No sheet setup found for {track_name}.",
                }

            row_dict = dict(target_row)
            sheet_params = json.loads(row_dict["params"])
            sheet_team = row_dict["team_name"]

            # Format comparison text
            diff_text = ""
            if car_setup_params:
                diffs: list[str] = []
                for k in ("front_wing", "rear_wing", "brake_bias", "on_throttle"):
                    if k in sheet_params and k in car_setup_params:
                        delta = sheet_params[k] - car_setup_params[k]
                        if delta > 0:
                            diffs.append(f"+{delta} {k.replace('_', ' ')}")
                        elif delta < 0:
                            diffs.append(f"{delta} {k.replace('_', ' ')}")
                if diffs:
                    diff_text = f" Compared to your car: {', '.join(diffs)}."

            fw = sheet_params.get("front_wing", 0)
            rw = sheet_params.get("rear_wing", 0)
            bb = sheet_params.get("brake_bias", 50)
            text = (
                f"From the imported sheet (untested in game): {sheet_team} setup for {track_name} "
                f"runs {fw} front wing, {rw} rear wing, {bb}% brake bias.{diff_text}"
            )

            return {
                "found": True,
                "track_name": track_name,
                "team_name": sheet_team,
                "source": row_dict["source"],
                "params": sheet_params,
                "text": text,
            }
