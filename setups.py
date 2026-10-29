"""Car setup library for F1 25.

Captures, stores, ranks, and shows car setups per car class, team, track, and conditions.
Setups come strictly from (a) the game's own CarSetups packet, or (b) explicit user imports.
Setup values are NEVER invented or scraped.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import re
import sqlite3
import statistics
import time
from pathlib import Path
from typing import Any, Optional
import yaml

log = logging.getLogger("engineer.setups")

SCHEMA_VERSION = 1
SETUP_PARAMS_YAML_PATH = Path(__file__).parent / "setup_params.yaml"


def load_setup_param_definitions(yaml_path: str | Path | None = None) -> dict[str, Any]:
    """Loads parameter and group definitions from setup_params.yaml."""
    p = Path(yaml_path) if yaml_path else SETUP_PARAMS_YAML_PATH
    if not p.is_file():
        return {"version": 1, "groups": [], "parameters": []}
    with open(p, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data


def get_setup_schema(
    car_class: int = 0,
    storage: Optional[Any] = None,
    yaml_path: str | Path | None = None,
) -> dict[str, Any]:
    """Generates the merged form schema combining setup_params.yaml and confirmed ranges."""
    defs = load_setup_param_definitions(yaml_path)
    ranges: dict[str, dict[str, float]] = {}
    if storage is not None and hasattr(storage, "get_param_ranges"):
        ranges = storage.get_param_ranges(car_class)

    group_dicts = sorted(defs.get("groups", []), key=lambda g: g.get("display_order", 0) if isinstance(g, dict) else 0)
    groups = [g["name"] if isinstance(g, dict) else str(g) for g in group_dicts]
    raw_params = sorted(defs.get("parameters", []), key=lambda p: p.get("display_order", 0))

    params_out: list[dict[str, Any]] = []
    for p in raw_params:
        key = canonical_param_name(p.get("key", ""))
        p_copy = dict(p)
        p_copy["key"] = key
        if key in ranges:
            r = ranges[key]
            p_copy["has_confirmed_range"] = True
            p_copy["min"] = r["min"]
            p_copy["max"] = r["max"]
            p_copy["step"] = r.get("step", p.get("step", 1))
        else:
            p_copy["has_confirmed_range"] = False
            p_copy["min"] = None
            p_copy["max"] = None
        params_out.append(p_copy)

    return {
        "version": defs.get("version", 1),
        "car_class": car_class,
        "groups": groups,
        "group_details": group_dicts,
        "parameters": params_out,
    }


def validate_setup_input(
    params: dict[str, Any],
    car_class: int = 0,
    storage: Optional[Any] = None,
    yaml_path: str | Path | None = None,
) -> dict[str, Any]:
    """Validates user-entered setup parameters.

    Rules:
    - Every field is optional. Empty stay empty: never fill defaults or guesses.
    - If range is confirmed: flag typed values outside [min, max] in out_of_range.
    - If range is not confirmed: allow any number, record in unconfirmed_ranges;
      NEVER block saving because a range is unknown.
    """
    ranges: dict[str, dict[str, float]] = {}
    if storage is not None and hasattr(storage, "get_param_ranges"):
        ranges = storage.get_param_ranges(car_class)

    clean_params: dict[str, Any] = {}
    out_of_range: dict[str, dict[str, float]] = {}
    unconfirmed_ranges: list[str] = []
    unknown: list[str] = []

    for k, v in params.items():
        if v is None or v == "":
            continue
        canon = canonical_param_name(k)
        if canon not in STANDARD_PARAMS and canon != "next_front_wing_value":
            unknown.append(k)

        try:
            val_num = float(v)
            if canon in INT_PARAMS:
                clean_val = int(round(val_num))
            elif canon in FLOAT_PARAMS:
                clean_val = round(val_num, 3)
            else:
                clean_val = val_num
        except (ValueError, TypeError):
            continue

        clean_params[canon] = clean_val

        if canon in ranges:
            r = ranges[canon]
            if clean_val < r["min"] or clean_val > r["max"]:
                out_of_range[canon] = {
                    "value": clean_val,
                    "min": r["min"],
                    "max": r["max"],
                }
        else:
            unconfirmed_ranges.append(canon)

    missing = [p for p in STANDARD_PARAMS if p not in clean_params]

    return {
        "valid": len(out_of_range) == 0,
        "clean_params": clean_params,
        "missing": missing,
        "out_of_range": out_of_range,
        "unconfirmed_ranges": unconfirmed_ranges,
        "unknown": unknown,
    }


STANDARD_PARAMS = [
    # Aerodynamics
    "front_wing",
    "rear_wing",
    # Transmission
    "on_throttle",
    "off_throttle",
    "engine_braking",
    # Suspension Geometry
    "front_camber",
    "rear_camber",
    "front_toe",
    "rear_toe",
    # Suspension
    "front_suspension",
    "rear_suspension",
    "front_anti_roll_bar",
    "rear_anti_roll_bar",
    "front_suspension_height",
    "rear_suspension_height",
    # Brakes
    "brake_pressure",
    "brake_bias",
    # Tyres
    "front_left_tyre_pressure",
    "front_right_tyre_pressure",
    "rear_left_tyre_pressure",
    "rear_right_tyre_pressure",
    # Weight & Fuel
    "ballast",
    "fuel_load",
]

PARAM_ALIASES = {
    "frontwing": "front_wing",
    "rearwing": "rear_wing",
    "front_wing_aero": "front_wing",
    "rear_wing_aero": "rear_wing",
    "onthrottle": "on_throttle",
    "offthrottle": "off_throttle",
    "diff_on_throttle": "on_throttle",
    "diff_off_throttle": "off_throttle",
    "differential_on_throttle": "on_throttle",
    "differential_off_throttle": "off_throttle",
    "enginebraking": "engine_braking",
    "engine_brake": "engine_braking",
    "frontcamber": "front_camber",
    "rearcamber": "rear_camber",
    "fronttoe": "front_toe",
    "reartoe": "rear_toe",
    "frontsuspension": "front_suspension",
    "rearsuspension": "rear_suspension",
    "frontantirollbar": "front_anti_roll_bar",
    "rearantirollbar": "rear_anti_roll_bar",
    "front_arb": "front_anti_roll_bar",
    "rear_arb": "rear_anti_roll_bar",
    "frontsuspensionheight": "front_suspension_height",
    "rearsuspensionheight": "rear_suspension_height",
    "front_ride_height": "front_suspension_height",
    "rear_ride_height": "rear_suspension_height",
    "brakepressure": "brake_pressure",
    "brakebias": "brake_bias",
    "rearlefttyrepressure": "rear_left_tyre_pressure",
    "rearrighttyrepressure": "rear_right_tyre_pressure",
    "frontlefttyrepressure": "front_left_tyre_pressure",
    "frontrighttyrepressure": "front_right_tyre_pressure",
    "rear_left_pressure": "rear_left_tyre_pressure",
    "rear_right_pressure": "rear_right_tyre_pressure",
    "front_left_pressure": "front_left_tyre_pressure",
    "front_right_pressure": "front_right_tyre_pressure",
    "fuelload": "fuel_load",
    "fuel": "fuel_load",
}

INT_PARAMS = {
    "front_wing", "rear_wing", "on_throttle", "off_throttle", "engine_braking",
    "front_suspension", "rear_suspension", "front_anti_roll_bar", "rear_anti_roll_bar",
    "front_suspension_height", "rear_suspension_height", "brake_pressure", "brake_bias",
    "ballast",
}

FLOAT_PARAMS = {
    "front_camber", "rear_camber", "front_toe", "rear_toe",
    "front_left_tyre_pressure", "front_right_tyre_pressure",
    "rear_left_tyre_pressure", "rear_right_tyre_pressure",
    "fuel_load", "next_front_wing_value",
}


def canonical_param_name(key: str) -> str:
    """Normalizes key into canonical snake_case parameter name."""
    raw = key.strip().lower()
    cleaned = re.sub(r"[\s\-_]+", "_", raw)
    if cleaned in PARAM_ALIASES:
        return PARAM_ALIASES[cleaned]
    no_under = cleaned.replace("_", "")
    if no_under in PARAM_ALIASES:
        return PARAM_ALIASES[no_under]
    return cleaned


def compute_setup_fingerprint(params: dict[str, Any]) -> str:
    """Computes deterministic SHA-256 fingerprint from setup parameter values."""
    canonical: dict[str, Any] = {}
    for k in sorted(params.keys()):
        canon_k = canonical_param_name(k)
        v = params[k]
        if v is None:
            continue
        try:
            if canon_k in INT_PARAMS:
                canonical[canon_k] = int(round(float(v)))
            elif canon_k in FLOAT_PARAMS:
                canonical[canon_k] = round(float(v), 4)
            elif isinstance(v, (int, float)):
                canonical[canon_k] = round(float(v), 4)
            else:
                canonical[canon_k] = str(v)
        except (ValueError, TypeError):
            canonical[canon_k] = str(v)
    raw = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def validate_setup_params(
    params: dict[str, Any],
    car_class: int = 0,
    ranges: Optional[dict[str, dict[str, float]]] = None,
) -> dict[str, Any]:
    """Validates setup params against setup_param_ranges without inventing missing values.

    Returns:
        {
            "valid": bool,
            "params": dict,           # clean typed params
            "missing": list[str],     # standard params missing from setup
            "out_of_range": dict,     # {param: {"value": val, "min": min, "max": max}}
            "unknown": list[str],     # extra/unknown params
        }
    """
    ranges = ranges or {}
    clean: dict[str, Any] = {}
    out_of_range: dict[str, dict[str, float]] = {}
    unknown: list[str] = []

    for k, v in params.items():
        if v is None or v == "":
            continue
        canon = canonical_param_name(k)
        if canon not in STANDARD_PARAMS and canon != "next_front_wing_value":
            unknown.append(k)

        try:
            val_num = float(v)
            if canon in INT_PARAMS:
                clean[canon] = int(round(val_num))
            elif canon in FLOAT_PARAMS:
                clean[canon] = round(val_num, 3)
            else:
                clean[canon] = val_num
        except (ValueError, TypeError):
            continue

        # Range check if range is configured in setup_param_ranges
        if canon in ranges:
            r = ranges[canon]
            val_check = clean[canon]
            if val_check < r["min"] or val_check > r["max"]:
                out_of_range[canon] = {
                    "value": val_check,
                    "min": r["min"],
                    "max": r["max"],
                }

    missing = [p for p in STANDARD_PARAMS if p not in clean]

    # Valid if no out_of_range errors (missing values are allowed and left empty)
    is_valid = len(out_of_range) == 0

    return {
        "valid": is_valid,
        "params": clean,
        "missing": missing,
        "out_of_range": out_of_range,
        "unknown": unknown,
    }


def generate_csv_template() -> str:
    """Generates a downloadable CSV template containing all standard setup parameters."""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["parameter", "value", "unit_and_notes"])
    for p in STANDARD_PARAMS:
        note = ""
        if "wing" in p or "suspension" in p or "anti_roll" in p:
            note = "clicks / integer"
        elif "throttle" in p or "brake" in p:
            note = "% (percent)"
        elif "camber" in p or "toe" in p:
            note = "degrees (°)"
        elif "pressure" in p:
            note = "psi"
        elif "fuel" in p:
            note = "kg"
        writer.writerow([p, "", note])
    return output.getvalue()


def import_from_csv(csv_text: str) -> dict[str, Any]:
    """Parses a CSV string (parameter, value) into a params dictionary."""
    reader = csv.reader(io.StringIO(csv_text.strip()))
    params: dict[str, Any] = {}
    for row in reader:
        if not row or len(row) < 2:
            continue
        key_raw = row[0].strip()
        val_raw = row[1].strip()
        if not key_raw or key_raw.lower() in ("parameter", "param", "key", "#"):
            continue
        if not val_raw:
            continue
        canon = canonical_param_name(key_raw)
        try:
            val_num = float(val_raw)
            if canon in INT_PARAMS:
                params[canon] = int(round(val_num))
            elif canon in FLOAT_PARAMS:
                params[canon] = round(val_num, 3)
            else:
                params[canon] = val_num
        except ValueError:
            continue
    return params


def import_from_json(json_text_or_dict: str | dict) -> dict[str, Any]:
    """Parses JSON text or dict into setup dictionary."""
    if isinstance(json_text_or_dict, str):
        data = json.loads(json_text_or_dict)
    else:
        data = dict(json_text_or_dict)

    params_in = data.get("params", data)
    clean_params: dict[str, Any] = {}
    for k, v in params_in.items():
        if k in ("name", "car_class", "track_id", "conditions", "session_type", "source_note", "source", "fingerprint", "schema_version"):
            continue
        if v is None or v == "":
            continue
        canon = canonical_param_name(k)
        try:
            val_num = float(v)
            if canon in INT_PARAMS:
                clean_params[canon] = int(round(val_num))
            elif canon in FLOAT_PARAMS:
                clean_params[canon] = round(val_num, 3)
            else:
                clean_params[canon] = val_num
        except (ValueError, TypeError):
            continue

    return {
        "name": data.get("name", ""),
        "car_class": data.get("car_class", 0),
        "track_id": data.get("track_id", 0),
        "conditions": data.get("conditions", "dry"),
        "session_type": data.get("session_type", "race"),
        "source_note": data.get("source_note", ""),
        "params": clean_params,
    }


SYSTEM_PROMPT_SETUP_EXTRACTOR = (
    "You are an F1 25 car setup extractor. Extract car setup values from the user's text into a strict JSON dictionary.\n"
    "Only return a single JSON object where keys are standard setup parameters.\n"
    "Valid parameter keys are:\n"
    "front_wing, rear_wing, on_throttle, off_throttle, engine_braking,\n"
    "front_camber, rear_camber, front_toe, rear_toe,\n"
    "front_suspension, rear_suspension, front_anti_roll_bar, rear_anti_roll_bar,\n"
    "front_suspension_height, rear_suspension_height, brake_pressure, brake_bias,\n"
    "front_left_tyre_pressure, front_right_tyre_pressure, rear_left_tyre_pressure, rear_right_tyre_pressure,\n"
    "ballast, fuel_load.\n"
    "DO NOT invent, assume, or guess any values that are not explicitly stated in the text.\n"
    "If a parameter is not mentioned, OMIT IT completely from the JSON.\n"
    "Return ONLY valid raw JSON with NO markdown formatting, NO backticks, and NO conversational text."
)


async def parse_free_text_setup(text: str, llm_provider_or_client: Any) -> dict[str, Any]:
    """Uses LLM provider to extract setup params from unstructured text into a dict."""
    prompt = text.strip()
    response_text = ""

    # Check if object has stream or complete
    if hasattr(llm_provider_or_client, "complete"):
        response_text = await llm_provider_or_client.complete(SYSTEM_PROMPT_SETUP_EXTRACTOR, prompt)
    elif hasattr(llm_provider_or_client, "stream"):
        async for chunk in llm_provider_or_client.stream(SYSTEM_PROMPT_SETUP_EXTRACTOR, prompt):
            response_text += chunk
    else:
        raise ValueError("Provided LLM client has neither stream nor complete method")

    # Strip code fences if model enclosed in markdown
    cleaned = response_text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```$", "", cleaned)
    cleaned = cleaned.strip()

    # Find JSON dict if extra text surrounds it
    m = re.search(r"(\{.*\})", cleaned, re.DOTALL)
    if m:
        cleaned = m.group(1)

    raw_dict = json.loads(cleaned)
    # Filter and normalize
    clean_params: dict[str, Any] = {}
    for k, v in raw_dict.items():
        if v is None or v == "":
            continue
        canon = canonical_param_name(k)
        if canon in STANDARD_PARAMS:
            try:
                num = float(v)
                if canon in INT_PARAMS:
                    clean_params[canon] = int(round(num))
                elif canon in FLOAT_PARAMS:
                    clean_params[canon] = round(num, 3)
                else:
                    clean_params[canon] = num
            except (ValueError, TypeError):
                continue

    return clean_params


def export_as_json(setup: dict[str, Any]) -> str:
    """Exports setup in standard library JSON format."""
    payload = {
        "id": setup.get("id"),
        "fingerprint": setup.get("fingerprint"),
        "name": setup.get("name", ""),
        "car_class": setup.get("car_class", 0),
        "team_id": setup.get("team_id", 0),
        "track_id": setup.get("track_id", 0),
        "conditions": setup.get("conditions", "dry"),
        "session_type": setup.get("session_type", "race"),
        "game_format": setup.get("game_format", 2025),
        "source": setup.get("source", "imported"),
        "source_note": setup.get("source_note", ""),
        "source_link": setup.get("source_link", ""),
        "notes": setup.get("notes", ""),
        "created_at": setup.get("created_at", time.time()),
        "schema_version": setup.get("schema_version", SCHEMA_VERSION),
        "params": setup.get("params", {}),
    }
    return json.dumps(payload, indent=2)


def export_as_sheet(setup: dict[str, Any]) -> str:
    """Exports setup as human-readable plain text matching F1 25 in-game setup screens."""
    params = setup.get("params", {})
    p = lambda k, unit="": f"{params[k]}{unit}" if k in params and params[k] is not None else "--"

    lines = [
        "============================================================",
        f" F1 25 CAR SETUP SHEET: {setup.get('name', 'Untitled Setup')}",
        "============================================================",
        f"Track: {setup.get('track_name', setup.get('track_id', '--'))} | Conditions: {setup.get('conditions', '--').upper()} | Session: {setup.get('session_type', '--').upper()}",
        f"Source: {setup.get('source', '--')} ({setup.get('source_note', '')})",
        f"Fingerprint: {setup.get('fingerprint', '--')}",
        "------------------------------------------------------------",
        " 1. AERODYNAMICS",
        f"    Front Wing Aero:                 {p('front_wing')}",
        f"    Rear Wing Aero:                  {p('rear_wing')}",
        "",
        " 2. TRANSMISSION",
        f"    Diff Adjustment On-Throttle:     {p('on_throttle', '%')}",
        f"    Diff Adjustment Off-Throttle:    {p('off_throttle', '%')}",
        f"    Engine Braking:                  {p('engine_braking', '%')}",
        "",
        " 3. SUSPENSION GEOMETRY",
        f"    Front Camber:                    {p('front_camber', '°')}",
        f"    Rear Camber:                     {p('rear_camber', '°')}",
        f"    Front Toe-Out:                   {p('front_toe', '°')}",
        f"    Rear Toe-In:                     {p('rear_toe', '°')}",
        "",
        " 4. SUSPENSION",
        f"    Front Suspension:                {p('front_suspension')}",
        f"    Rear Suspension:                 {p('rear_suspension')}",
        f"    Front Anti-Roll Bar:             {p('front_anti_roll_bar')}",
        f"    Rear Anti-Roll Bar:              {p('rear_anti_roll_bar')}",
        f"    Front Ride Height:               {p('front_suspension_height')}",
        f"    Rear Ride Height:                {p('rear_suspension_height')}",
        "",
        " 5. BRAKES",
        f"    Brake Pressure:                  {p('brake_pressure', '%')}",
        f"    Front Brake Bias:                {p('brake_bias', '%')}",
        "",
        " 6. TYRES",
        f"    Front Left Tyre Pressure:        {p('front_left_tyre_pressure', ' psi')}",
        f"    Front Right Tyre Pressure:       {p('front_right_tyre_pressure', ' psi')}",
        f"    Rear Left Tyre Pressure:         {p('rear_left_tyre_pressure', ' psi')}",
        f"    Rear Right Tyre Pressure:        {p('rear_right_tyre_pressure', ' psi')}",
        "",
        " 7. WEIGHT & FUEL",
        f"    Ballast:                         {p('ballast')}",
        f"    Fuel Load:                       {p('fuel_load', ' kg')}",
        "============================================================",
    ]
    return "\n".join(lines)


def diff_setups(setup_a: dict[str, Any], setup_b: dict[str, Any]) -> dict[str, Any]:
    """Computes a side-by-side diff between two setups."""
    params_a = setup_a.get("params", {}) if setup_a else {}
    params_b = setup_b.get("params", {}) if setup_b else {}

    all_keys = list(STANDARD_PARAMS)
    for k in list(params_a.keys()) + list(params_b.keys()):
        if k not in all_keys:
            all_keys.append(k)

    diffs: dict[str, Any] = {}
    changed_count = 0
    for k in all_keys:
        val_a = params_a.get(k)
        val_b = params_b.get(k)
        changed = (val_a != val_b)
        delta = None
        if isinstance(val_a, (int, float)) and isinstance(val_b, (int, float)):
            delta = round(val_b - val_a, 3)
            if isinstance(val_a, int) and isinstance(val_b, int):
                delta = int(round(delta))

        if changed:
            changed_count += 1

        diffs[k] = {
            "val_a": val_a,
            "val_b": val_b,
            "changed": changed,
            "delta": delta,
        }

    return {
        "setup_a_name": setup_a.get("name", "Setup A") if setup_a else "None",
        "setup_b_name": setup_b.get("name", "Setup B") if setup_b else "None",
        "changed_count": changed_count,
        "diffs": diffs,
    }


class SetupsStorage:
    """Thread-safe SQLite storage for the car setup library."""

    def __init__(self, db_path: str = "setups.db"):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.execute("PRAGMA synchronous = NORMAL;")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS setups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    fingerprint TEXT UNIQUE NOT NULL,
                    car_class INTEGER NOT NULL DEFAULT 0,
                    team_id INTEGER NOT NULL DEFAULT 0,
                    track_id INTEGER NOT NULL DEFAULT 0,
                    conditions TEXT NOT NULL DEFAULT 'dry',
                    session_type TEXT NOT NULL DEFAULT 'race',
                    game_format INTEGER NOT NULL DEFAULT 2025,
                    source TEXT NOT NULL,
                    source_note TEXT DEFAULT '',
                    source_link TEXT DEFAULT '',
                    notes TEXT DEFAULT '',
                    name TEXT DEFAULT '',
                    created_at REAL NOT NULL,
                    params TEXT NOT NULL,
                    schema_version INTEGER NOT NULL DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS setup_param_ranges (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    param_name TEXT NOT NULL,
                    car_class INTEGER NOT NULL DEFAULT 0,
                    min_val REAL NOT NULL,
                    max_val REAL NOT NULL,
                    step_val REAL DEFAULT 1.0,
                    UNIQUE(param_name, car_class)
                );

                CREATE INDEX IF NOT EXISTS idx_setups_lookup ON setups(track_id, car_class, conditions, session_type);
                CREATE INDEX IF NOT EXISTS idx_setups_fp ON setups(fingerprint);
            """)
            # Migration check for existing databases
            try:
                conn.execute("ALTER TABLE setups ADD COLUMN source_link TEXT DEFAULT ''")
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute("ALTER TABLE setups ADD COLUMN notes TEXT DEFAULT ''")
            except sqlite3.OperationalError:
                pass
            conn.commit()

    def save_captured_setup(
        self,
        params: dict[str, Any],
        car_class: int,
        team_id: int,
        track_id: int,
        conditions: str,
        session_type: str,
        game_format: int = 2025,
        name: str = "",
    ) -> dict[str, Any]:
        """Saves player setup captured from telemetry. Returns existing setup if fingerprint matches."""
        clean = {k: v for k, v in params.items() if v is not None and v != ""}
        fp = compute_setup_fingerprint(clean)

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("SELECT * FROM setups WHERE fingerprint = ?", (fp,))
            row = cur.fetchone()
            if row:
                return self._row_to_dict(row)

            default_name = name or f"Captured {conditions.capitalize()} ({fp[:6]})"
            now = time.time()
            cur.execute("""
                INSERT INTO setups (
                    fingerprint, car_class, team_id, track_id, conditions,
                    session_type, game_format, source, source_note, source_link,
                    notes, name, created_at, params, schema_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'captured', 'Captured from CarSetups packet', '', '', ?, ?, ?, ?)
            """, (
                fp, car_class, team_id, track_id, conditions.lower(),
                session_type.lower(), game_format, default_name, now,
                json.dumps(clean, sort_keys=True), SCHEMA_VERSION
            ))
            new_id = cur.lastrowid
            conn.commit()

            cur.execute("SELECT * FROM setups WHERE id = ?", (new_id,))
            return self._row_to_dict(cur.fetchone())

    def save_imported_setup(
        self,
        params: dict[str, Any],
        car_class: int,
        track_id: int,
        conditions: str,
        session_type: str,
        source_note: str = "",
        name: str = "",
        team_id: int = 0,
        game_format: int = 2025,
        source_link: str = "",
        notes: str = "",
    ) -> dict[str, Any]:
        """Saves user-imported setup. If identical fingerprint exists, updates metadata or returns existing."""
        clean = {k: v for k, v in params.items() if v is not None and v != ""}
        fp = compute_setup_fingerprint(clean)

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("SELECT * FROM setups WHERE fingerprint = ?", (fp,))
            row = cur.fetchone()
            if row:
                # Update metadata if provided
                if name or source_note or source_link or notes:
                    cur.execute("""
                        UPDATE setups
                        SET name = COALESCE(NULLIF(?, ''), name),
                            source_note = COALESCE(NULLIF(?, ''), source_note),
                            source_link = COALESCE(NULLIF(?, ''), source_link),
                            notes = COALESCE(NULLIF(?, ''), notes)
                        WHERE fingerprint = ?
                    """, (name, source_note, source_link, notes, fp))
                    conn.commit()
                    cur.execute("SELECT * FROM setups WHERE fingerprint = ?", (fp,))
                    return self._row_to_dict(cur.fetchone())
                return self._row_to_dict(row)

            default_name = name or f"Imported {conditions.capitalize()} ({fp[:6]})"
            now = time.time()
            cur.execute("""
                INSERT INTO setups (
                    fingerprint, car_class, team_id, track_id, conditions,
                    session_type, game_format, source, source_note, source_link,
                    notes, name, created_at, params, schema_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'imported', ?, ?, ?, ?, ?, ?, ?)
            """, (
                fp, car_class, team_id, track_id, conditions.lower(),
                session_type.lower(), game_format, source_note, source_link,
                notes, default_name, now, json.dumps(clean, sort_keys=True), SCHEMA_VERSION
            ))
            new_id = cur.lastrowid
            conn.commit()

            cur.execute("SELECT * FROM setups WHERE id = ?", (new_id,))
            return self._row_to_dict(cur.fetchone())

    def create_custom_setup(
        self,
        params: dict[str, Any],
        car_class: int,
        track_id: int,
        conditions: str,
        session_type: str,
        name: str = "",
        team_id: int = 0,
        source_note: str = "",
        source_link: str = "",
        notes: str = "",
        game_format: int = 2025,
        force: bool = False,
    ) -> tuple[dict[str, Any], bool]:
        """Creates a custom/manual setup.

        Returns:
            (setup_dict, is_duplicate)
        """
        clean = {k: v for k, v in params.items() if v is not None and v != ""}
        fp = compute_setup_fingerprint(clean)

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("SELECT * FROM setups WHERE fingerprint = ?", (fp,))
            row = cur.fetchone()
            if row and not force:
                return self._row_to_dict(row), True

            if row and force:
                cur.execute("""
                    UPDATE setups
                    SET name = COALESCE(NULLIF(?, ''), name),
                        car_class = ?, team_id = ?, track_id = ?,
                        conditions = ?, session_type = ?,
                        source_note = COALESCE(NULLIF(?, ''), source_note),
                        source_link = COALESCE(NULLIF(?, ''), source_link),
                        notes = COALESCE(NULLIF(?, ''), notes),
                        params = ?
                    WHERE fingerprint = ?
                """, (
                    name, car_class, team_id, track_id,
                    conditions.lower(), session_type.lower(),
                    source_note, source_link, notes,
                    json.dumps(clean, sort_keys=True), fp
                ))
                conn.commit()
                cur.execute("SELECT * FROM setups WHERE fingerprint = ?", (fp,))
                return self._row_to_dict(cur.fetchone()), False

            default_name = name or f"Custom {conditions.capitalize()} ({fp[:6]})"
            now = time.time()
            cur.execute("""
                INSERT INTO setups (
                    fingerprint, car_class, team_id, track_id, conditions,
                    session_type, game_format, source, source_note, source_link,
                    notes, name, created_at, params, schema_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'imported', ?, ?, ?, ?, ?, ?, ?)
            """, (
                fp, car_class, team_id, track_id, conditions.lower(),
                session_type.lower(), game_format, source_note, source_link,
                notes, default_name, now, json.dumps(clean, sort_keys=True), SCHEMA_VERSION
            ))
            new_id = cur.lastrowid
            conn.commit()

            cur.execute("SELECT * FROM setups WHERE id = ?", (new_id,))
            return self._row_to_dict(cur.fetchone()), False

    def update_setup(
        self,
        setup_id: int | str,
        data: dict[str, Any],
        allow_fork: bool = False,
    ) -> dict[str, Any]:
        """Updates an existing setup.

        Captured setups (source='captured') are read-only.
        If allow_fork is True, creates a new imported copy.
        Otherwise raises ValueError.
        """
        existing = self.get_setup(setup_id)
        if not existing:
            raise KeyError(f"Setup {setup_id} not found")

        # Captured setup protection
        if existing.get("source") == "captured":
            if not allow_fork:
                raise ValueError("Captured setups are read-only. Editing creates a new imported copy.")

            forked_params = data.get("params", existing["params"])
            clean_params = {k: v for k, v in forked_params.items() if v is not None and v != ""}
            new_name = data.get("name") or f"{existing.get('name', 'Setup')} (Copy)"
            saved, _ = self.create_custom_setup(
                params=clean_params,
                car_class=data.get("car_class", existing["car_class"]),
                team_id=data.get("team_id", existing["team_id"]),
                track_id=data.get("track_id", existing["track_id"]),
                conditions=data.get("conditions", existing["conditions"]),
                session_type=data.get("session_type", existing["session_type"]),
                name=new_name,
                source_note=data.get("source_note") or f"Forked from captured setup {existing['fingerprint'][:6]}",
                source_link=data.get("source_link", existing.get("source_link", "")),
                notes=data.get("notes", existing.get("notes", "")),
                force=True,
            )
            return saved

        new_params = data.get("params", existing["params"])
        clean_params = {k: v for k, v in new_params.items() if v is not None and v != ""}
        new_fp = compute_setup_fingerprint(clean_params)

        name = data.get("name", existing["name"])
        car_class = data.get("car_class", existing["car_class"])
        team_id = data.get("team_id", existing["team_id"])
        track_id = data.get("track_id", existing["track_id"])
        conditions = data.get("conditions", existing["conditions"])
        session_type = data.get("session_type", existing["session_type"])
        source_note = data.get("source_note", existing["source_note"])
        source_link = data.get("source_link", existing.get("source_link", ""))
        notes = data.get("notes", existing.get("notes", ""))

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("""
                UPDATE setups
                SET fingerprint = ?, name = ?, car_class = ?, team_id = ?,
                    track_id = ?, conditions = ?, session_type = ?,
                    source_note = ?, source_link = ?, notes = ?, params = ?
                WHERE id = ?
            """, (
                new_fp, name, car_class, team_id,
                track_id, conditions.lower(), session_type.lower(),
                source_note, source_link, notes,
                json.dumps(clean_params, sort_keys=True), existing["id"]
            ))
            conn.commit()

            cur.execute("SELECT * FROM setups WHERE id = ?", (existing["id"],))
            return self._row_to_dict(cur.fetchone())

    def delete_setup(self, setup_id: int | str) -> bool:
        """Deletes a setup. Captured setups are protected and cannot be deleted."""
        existing = self.get_setup(setup_id)
        if not existing:
            return False
        if existing.get("source") == "captured":
            raise ValueError("Captured setups are read-only and cannot be deleted.")

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM setups WHERE id = ?", (existing["id"],))
            conn.commit()
        return True

    def get_setup(self, setup_id: int | str) -> Optional[dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("SELECT * FROM setups WHERE id = ? OR fingerprint = ?", (str(setup_id), str(setup_id)))
            row = cur.fetchone()
            return self._row_to_dict(row) if row else None

    def get_setup_by_fingerprint(self, fp: str) -> Optional[dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("SELECT * FROM setups WHERE fingerprint = ?", (fp,))
            row = cur.fetchone()
            return self._row_to_dict(row) if row else None

    def list_setups(
        self,
        track_id: Optional[int] = None,
        car_class: Optional[int] = None,
        conditions: Optional[str] = None,
        session_type: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM setups WHERE 1=1"
        args: list[Any] = []
        if track_id is not None:
            query += " AND track_id = ?"
            args.append(track_id)
        if car_class is not None:
            query += " AND car_class = ?"
            args.append(car_class)
        if conditions:
            query += " AND conditions = ?"
            args.append(conditions.lower())
        if session_type:
            query += " AND session_type = ?"
            args.append(session_type.lower())

        query += " ORDER BY id DESC"
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute(query, tuple(args))
            return [self._row_to_dict(r) for r in cur.fetchall()]

    def get_param_ranges(self, car_class: int = 0) -> dict[str, dict[str, float]]:
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute("SELECT param_name, min_val, max_val, step_val FROM setup_param_ranges WHERE car_class = ?", (car_class,))
            ranges: dict[str, dict[str, float]] = {}
            for row in cur.fetchall():
                ranges[row[0]] = {"min": row[1], "max": row[2], "step": row[3]}
            return ranges

    def set_param_range(self, param_name: str, car_class: int, min_val: float, max_val: float, step_val: float = 1.0):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO setup_param_ranges (param_name, car_class, min_val, max_val, step_val)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(param_name, car_class) DO UPDATE SET
                    min_val = excluded.min_val,
                    max_val = excluded.max_val,
                    step_val = excluded.step_val
            """, (param_name, car_class, min_val, max_val, step_val))
            conn.commit()

    def get_observed_ranges(self) -> dict[int, dict[str, dict[str, float]]]:
        """Scans captured setups in the database to compute observed min/max for each parameter."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("SELECT car_class, params FROM setups WHERE source = 'captured'")
            rows = cur.fetchall()

        observed: dict[int, dict[str, list[float]]] = {}
        for r in rows:
            cc = r["car_class"]
            try:
                p_dict = json.loads(r["params"])
            except Exception:
                continue
            if cc not in observed:
                observed[cc] = {}
            for k, v in p_dict.items():
                if isinstance(v, (int, float)):
                    observed[cc].setdefault(k, []).append(float(v))

        result: dict[int, dict[str, dict[str, float]]] = {}
        for cc, params in observed.items():
            result[cc] = {}
            for k, vals in params.items():
                if vals:
                    result[cc][k] = {
                        "min": min(vals),
                        "max": max(vals),
                        "count": len(vals),
                    }
        return result

    def print_observed_ranges(self) -> str:
        """Prints observed parameter min/max values for user verification."""
        obs = self.get_observed_ranges()
        if not obs:
            return "No captured setups found in library yet. Complete telemetry laps to capture observed ranges."

        lines = [
            "============================================================",
            " OBSERVED CAR SETUP PARAMETER RANGES",
            "============================================================",
        ]
        for cc, p_map in sorted(obs.items()):
            lines.append(f"\nCar Class / Formula {cc}:")
            lines.append(f"{'Parameter':<30} {'Min':<10} {'Max':<10} {'Samples':<8}")
            lines.append("-" * 60)
            for p_name in STANDARD_PARAMS:
                if p_name in p_map:
                    info = p_map[p_name]
                    lines.append(f"{p_name:<30} {info['min']:<10.2f} {info['max']:<10.2f} {info['count']:<8}")
            # Any non-standard
            for p_name, info in sorted(p_map.items()):
                if p_name not in STANDARD_PARAMS:
                    lines.append(f"{p_name:<30} {info['min']:<10.2f} {info['max']:<10.2f} {info['count']:<8}")
        lines.append("============================================================")
        return "\n".join(lines)

    def get_ranked_setups(
        self,
        session_storage: Any,
        track_id: int,
        car_class: int,
        conditions: Optional[str] = None,
        session_type: Optional[str] = None,
        current_setup_id: Optional[Any] = None,
        current_fingerprint: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Ranks setups by best valid lap and median valid lap (min 3 laps for median).

        Setups without laps are marked 'untested'.
        """
        # Fetch candidate setups for this track & car class
        setups = self.list_setups(track_id=track_id, car_class=car_class, conditions=conditions, session_type=session_type)
        if not setups and track_id is not None:
            # If no setups strictly match track, check if any setups are known
            pass

        # Also get setups that may have laps on this track in session_storage
        laps_by_setup: dict[str, list[float]] = {}
        all_laps_count: dict[str, int] = {}
        if session_storage is not None:
            laps_by_setup, all_laps_count = self._get_lap_times_from_storage(
                session_storage, track_id, car_class, conditions, session_type
            )

        ranked: list[dict[str, Any]] = []
        for s in setups:
            sid = str(s["id"])
            fp = s["fingerprint"]
            # Look up laps under setup_id or fingerprint
            valid_times = laps_by_setup.get(sid) or laps_by_setup.get(fp) or []
            total_laps = all_laps_count.get(sid) or all_laps_count.get(fp) or len(valid_times)

            best_s = min(valid_times) if valid_times else None
            median_s = statistics.median(valid_times) if len(valid_times) >= 3 else None
            untested = (len(valid_times) == 0)

            is_cur = False
            if current_setup_id is not None and str(current_setup_id) in (sid, fp):
                is_cur = True
            elif current_fingerprint is not None and current_fingerprint == fp:
                is_cur = True

            item = dict(s)
            item.update({
                "best_lap_s": round(best_s, 3) if best_s is not None else None,
                "median_lap_s": round(median_s, 3) if median_s is not None else None,
                "valid_laps_count": len(valid_times),
                "total_laps_count": total_laps,
                "untested": untested,
                "is_current": is_cur,
            })
            ranked.append(item)

        # Sort: tested setups by best_lap_s ascending (then median, then lap count), untested at bottom
        def sort_key(x):
            if x["untested"] or x["best_lap_s"] is None:
                return (1, 999999.0, 999999.0, 0)
            med = x["median_lap_s"] if x["median_lap_s"] is not None else x["best_lap_s"]
            return (0, x["best_lap_s"], med, -x["valid_laps_count"])

        ranked.sort(key=sort_key)
        for i, item in enumerate(ranked):
            item["rank"] = (i + 1) if not item["untested"] else None

        return ranked

    def get_best_setup(
        self,
        session_storage: Any,
        track_id: int,
        car_class: int,
        conditions: Optional[str] = None,
        session_type: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Returns the best-ranked tested setup for this track/conditions."""
        ranked = self.get_ranked_setups(session_storage, track_id, car_class, conditions, session_type)
        for r in ranked:
            if not r["untested"] and r["best_lap_s"] is not None:
                return r
        return None

    def _get_lap_times_from_storage(
        self,
        storage: Any,
        track_id: int,
        formula: int,
        conditions: Optional[str] = None,
        session_type: Optional[str] = None,
    ) -> tuple[dict[str, list[float]], dict[str, int]]:
        """Queries session_storage DB to extract valid lap times per setup_id."""
        db_path = getattr(storage, "db_path", "sessions.db")
        if not Path(db_path).is_file():
            return {}, {}

        valid_times: dict[str, list[float]] = {}
        total_counts: dict[str, int] = {}
        try:
            with sqlite3.connect(db_path) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()

                query = """
                    SELECT l.setup_id, l.lap_time_s, l.is_valid, s.weather_at_start
                    FROM laps l
                    JOIN sessions s ON l.session_id = s.id
                    WHERE s.track_id = ? AND s.formula = ?
                      AND l.setup_id IS NOT NULL AND l.setup_id != ''
                """
                args: list[Any] = [track_id, formula]
                cur.execute(query, tuple(args))
                for row in cur.fetchall():
                    sid = str(row["setup_id"])
                    ltime = float(row["lap_time_s"])
                    is_valid = bool(row["is_valid"])
                    weather = int(row["weather_at_start"] or 0)
                    is_wet = weather in (3, 4, 5)

                    if conditions == "dry" and is_wet:
                        continue
                    if conditions == "wet" and not is_wet:
                        continue

                    total_counts[sid] = total_counts.get(sid, 0) + 1
                    if is_valid and ltime > 0:
                        valid_times.setdefault(sid, []).append(ltime)
        except Exception as e:
            log.debug("Could not query laps from session_storage: %s", e)

        return valid_times, total_counts

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        try:
            d["params"] = json.loads(d["params"])
        except Exception:
            d["params"] = {}
        d["source_link"] = d.get("source_link") or ""
        d["notes"] = d.get("notes") or ""
        return d
