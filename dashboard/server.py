"""FastAPI web dashboard server for live F1 25 telemetry and radio comms."""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import re
import secrets
import socket
import time
from pathlib import Path
from typing import Any

import qrcode
import uvicorn
from fastapi import FastAPI, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect, status
from fastapi.responses import FileResponse, PlainTextResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from config import AppConfig
from f1telemetry.state import COMPOUNDS, TRACKS, RaceState
from trackmap import TrackMapManager
from storage import SessionStorage
from analysis import analyze_corners, analyze_stint, compute_delta_trace
from knowledge import KnowledgeBase, track_to_slug
from setups import (
    SetupsStorage, diff_setups, export_as_json, export_as_sheet,
    generate_csv_template, get_setup_schema, import_from_csv, import_from_json,
    parse_free_text_setup, validate_setup_input, validate_setup_params,
    compute_setup_fingerprint,
)

FORMULAS = {
    0: "F1 Modern",
    1: "F1 Classic",
    2: "F2",
    3: "F1 Generic",
    4: "Beta",
    5: "Supercars",
    6: "Esports",
    7: "F2 2021",
}

SESSION_TYPES = {
    0: "Unknown",
    1: "Practice 1",
    2: "Practice 2",
    3: "Practice 3",
    4: "Short Practice",
    5: "Qualifying 1",
    6: "Qualifying 2",
    7: "Qualifying 3",
    8: "Short Qualifying",
    9: "One-Shot Qualifying",
    10: "Race",
    11: "Race 2",
    12: "Race 3",
    13: "Time Trial",
}

STATIC_DIR = Path(__file__).parent / "static"


class SetupCreateRequest(BaseModel):
    name: str = ""
    car_class: int = 0
    team_id: int = 0
    track_id: int = 0
    conditions: str = "dry"
    session_type: str = "race"
    source_note: str = ""
    source_link: str = ""
    notes: str = ""
    params: dict[str, Any] = Field(default_factory=dict)
    force: bool = False


class SetupUpdateRequest(BaseModel):
    name: str | None = None
    car_class: int | None = None
    team_id: int | None = None
    track_id: int | None = None
    conditions: str | None = None
    session_type: str | None = None
    source_note: str | None = None
    source_link: str | None = None
    notes: str | None = None
    params: dict[str, Any] | None = None
    allow_fork: bool = False


class CornerItem(BaseModel):
    number: int
    name: str
    description: str = ""


class TrackSectionsSave(BaseModel):
    title: str = ""
    corners: list[CornerItem] | None = None
    overtaking: str = ""
    tyres: str = ""
    setup_notes: str = ""


class TrackSaveRequest(BaseModel):
    raw: str | None = None
    raw_markdown: str | None = None
    sections: TrackSectionsSave | None = None
    corners_list: list[CornerItem] | None = None



def build_live_payload(state: RaceState, track_map: TrackMapManager | None = None) -> dict[str, Any]:
    """Combines snapshot(), timing_tower(), motion/car positions, and telemetry extras."""
    payload = state.snapshot()
    payload["timing_tower"] = state.timing_tower()

    # Extra telemetry fields (speed, gear, rpm, drs) for HUD gauges
    tel = state.my_tel
    if tel is not None:
        payload["speed_kph"] = tel.speed_kph
        payload["gear"] = tel.gear
        payload["rpm"] = tel.rpm
        payload["drs"] = tel.drs
        payload["throttle"] = round(tel.throttle, 2)
        payload["brake"] = round(tel.brake, 2)
    else:
        payload["speed_kph"] = None
        payload["gear"] = None
        payload["rpm"] = None
        payload["drs"] = False
        payload["throttle"] = 0.0
        payload["brake"] = 0.0

    # Motion & G-forces for friction circle
    motion = state.my_motion
    if motion is not None:
        payload["g_force"] = {
            "lat": round(motion.g_lat, 2),
            "lon": round(motion.g_lon, 2),
            "vert": round(motion.g_vert, 2),
        }
    else:
        payload["g_force"] = None

    # Track map & car positions
    if track_map is not None:
        payload["car_positions"] = track_map.extract_car_positions(state)
        payload["has_track_outline"] = bool(track_map.current_outline is not None)
    else:
        payload["car_positions"] = []
        payload["has_track_outline"] = False

    payload["track_id"] = state.session.track_id if state.session else None
    payload["last_packet_time"] = state.last_packet_time
    payload["type"] = "telemetry"
    return payload


def payload_fingerprint(payload: dict[str, Any]) -> str:
    """Creates a deterministic string representation of data for change detection."""
    p = dict(payload)
    p.pop("telemetry_age_s", None)
    return json.dumps(p, sort_keys=True)


def has_changed(new_payload: dict[str, Any], old_payload: dict[str, Any] | None) -> bool:
    """Returns True if the new payload differs from the previous payload."""
    if old_payload is None:
        return True
    return payload_fingerprint(new_payload) != payload_fingerprint(old_payload)


class Broadcaster:
    """Manages WebSocket client queues and non-blocking frame distribution.

    A slow client queue has size 1. If full, the old frame is dropped immediately,
    ensuring a slow client never blocks the broadcaster or any other client.
    """
    def __init__(self):
        self._clients: set[asyncio.Queue] = set()
        self._last_payload: dict[str, Any] | None = None

    @property
    def client_count(self) -> int:
        return len(self._clients)

    def register(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1)
        self._clients.add(q)
        if self._last_payload is not None:
            # Send latest state snapshot immediately to newly connected client
            try:
                q.put_nowait(json.dumps(self._last_payload))
            except asyncio.QueueFull:
                pass
        return q

    def unregister(self, q: asyncio.Queue):
        self._clients.discard(q)

    def broadcast_state(self, payload: dict[str, Any]) -> bool:
        """Pushes state payload if changed. Returns True if broadcast occurred."""
        if not has_changed(payload, self._last_payload):
            return False
        self._last_payload = payload
        self.push_message(payload)
        return True

    def broadcast_radio(self, sender: str, text: str, latency_ms: float | None = None):
        """Pushes radio log message to all clients."""
        msg = {
            "type": "radio",
            "sender": sender,
            "text": text,
            "latency_ms": latency_ms,
            "timestamp": time.time(),
        }
        self.push_message(msg)

    def push_message(self, msg: dict[str, Any] | str):
        """Dispatches message to all registered client queues with drop-old policy."""
        text = json.dumps(msg) if isinstance(msg, dict) else msg
        for q in list(self._clients):
            if q.full():
                try:
                    q.get_nowait()  # Drop old frame
                except asyncio.QueueEmpty:
                    pass
            try:
                q.put_nowait(text)
            except asyncio.QueueFull:
                pass


def verify_token(token: str | None, expected_token: str | None) -> bool:
    """Validates shared-secret token with constant-time comparison."""
    if not expected_token:
        return True
    if not token:
        return False
    return hmac.compare_digest(token, expected_token)


def get_lan_ip() -> str:
    """Finds primary local IPv4 address for mobile QR connection."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def save_token_to_yaml(config_path: str | Path, token: str):
    """Safely writes generated token back to config.yaml preserving existing formatting."""
    p = Path(config_path)
    if not p.is_file():
        return
    text = p.read_text(encoding="utf-8")
    if re.search(r"token:\s*(null|none|~|\"\"|'')?", text, re.IGNORECASE):
        new_text = re.sub(
            r"(token:\s*)(null|none|~|\"\"|'')(.*)",
            rf'\1"{token}"\3',
            text,
            count=1,
            flags=re.IGNORECASE,
        )
        p.write_text(new_text, encoding="utf-8")


def ensure_token(cfg: AppConfig, config_path: str = "config.yaml") -> str:
    """Generates random url-safe token if not configured and persists it."""
    if cfg.dashboard.token:
        return cfg.dashboard.token
    token = secrets.token_urlsafe(16)
    cfg.dashboard.token = token
    try:
        save_token_to_yaml(config_path, token)
    except Exception as e:
        log.warning("Could not persist dashboard token to %s: %s", config_path, e)
    return token


def print_dashboard_banner(host: str, port: int, token: str):
    """Prints dashboard URLs and ASCII QR code to terminal for easy phone access."""
    lan_ip = get_lan_ip()
    local_url = f"http://localhost:{port}/?token={token}"
    phone_url = f"http://{lan_ip}:{port}/?token={token}"

    print("\n" + "=" * 62)
    print("           🏁  RACE ENGINEER LIVE DASHBOARD  🏁")
    print("=" * 62)
    print(f"  Local PC  : {local_url}")
    print(f"  Phone/LAN : {phone_url}")
    print("\n  Scan QR Code with your phone camera to open:")
    try:
        qr = qrcode.QRCode(border=1)
        qr.add_data(phone_url)
        qr.print_ascii(invert=True)
    except Exception as e:
        log.warning("Could not print terminal QR code: %s", e)
    print("=" * 62 + "\n", flush=True)


def create_app(
    state: RaceState,
    broadcaster: Broadcaster,
    token: str,
    track_map: TrackMapManager | None = None,
    storage: SessionStorage | None = None,
    setups_storage: SetupsStorage | None = None,
    llm_client: Any | None = None,
    knowledge: KnowledgeBase | None = None,
) -> FastAPI:
    """Creates the FastAPI app instance."""
    app = FastAPI(title="Race Engineer Live Dashboard", docs_url=None, redoc_url=None)
    setups_store = setups_storage or SetupsStorage()
    knowledge_base = knowledge or KnowledgeBase()

    def check_token(
        token_param: str | None = None,
        authorization: str | None = None,
        x_token: str | None = None,
    ) -> bool:
        if not token:
            return True
        provided = token_param
        if not provided and authorization:
            if authorization.lower().startswith("bearer "):
                provided = authorization[7:].strip()
            else:
                provided = authorization.strip()
        if not provided and x_token:
            provided = x_token.strip()
        return verify_token(provided, token)

    # Serve static assets
    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/")
    async def get_index(token_param: str | None = Query(None, alias="token")):
        if not verify_token(token_param, token):
            return PlainTextResponse(
                "Access Denied: Invalid or missing token. Use the URL with ?token=...",
                status_code=status.HTTP_403_FORBIDDEN,
            )
        index_file = STATIC_DIR / "index.html"
        if not index_file.is_file():
            raise HTTPException(status_code=404, detail="index.html not found")
        return FileResponse(index_file)

    @app.get("/style.css")
    async def get_style():
        f = STATIC_DIR / "style.css"
        if f.is_file():
            return FileResponse(f, media_type="text/css")
        raise HTTPException(status_code=404)

    @app.get("/app.js")
    async def get_script():
        f = STATIC_DIR / "app.js"
        if f.is_file():
            return FileResponse(f, media_type="application/javascript")
        raise HTTPException(status_code=404)

    @app.get("/api/track_map")
    async def get_track_map(token_param: str | None = Query(None, alias="token")):
        if not verify_token(token_param, token):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Unauthorized")
        tid = state.session.track_id if state.session else (track_map.current_track_id if track_map else 0)
        if not track_map or not track_map.current_outline:
            return {
                "track_id": tid,
                "has_outline": False,
                "track_length_m": state.session.track_length_m if state.session else 0.0,
                "points": [],
                "sectors": {},
                "finish_line": [],
                "bounds": {"min_x": -100.0, "max_x": 100.0, "min_z": -100.0, "max_z": 100.0},
            }
        return {
            "has_outline": True,
            **track_map.current_outline.to_dict(),
        }

    @app.post("/api/track_map/rebuild")
    async def post_rebuild_track_map(token_param: str | None = Query(None, alias="token")):
        if not verify_token(token_param, token):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Unauthorized")
        if track_map:
            track_map.rebuild()
        return {"status": "rebuilding"}

    @app.get("/api/sessions")
    async def get_sessions(
        token_param: str | None = Query(None, alias="token"),
        track_id: int | None = None,
        formula: int | None = None,
        session_type: int | None = None,
        limit: int = 50,
    ):
        if not verify_token(token_param, token):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Unauthorized")
        if not storage:
            return []
        sessions = storage.get_sessions(track_id=track_id, formula=formula, session_type=session_type, limit=limit)
        for s in sessions:
            s["track_name"] = TRACKS.get(s["track_id"], f"Track {s['track_id']}")
            s["formula_name"] = FORMULAS.get(s["formula"], f"Class {s['formula']}")
            s["session_type_name"] = SESSION_TYPES.get(s["session_type"], f"Session {s['session_type']}")
        return sessions

    @app.get("/api/sessions/{session_id}/laps")
    async def get_session_laps(
        session_id: int,
        token_param: str | None = Query(None, alias="token"),
        valid_only: bool = False,
    ):
        if not verify_token(token_param, token):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Unauthorized")
        if not storage:
            return []
        laps = storage.get_laps_for_session(session_id, valid_only=valid_only)
        for l in laps:
            l["compound_name"] = COMPOUNDS.get(l.get("tyre_compound"), "Unknown")
        return laps

    @app.get("/api/laps/compare")
    async def compare_laps(
        lap1: int = Query(...),
        lap2: int = Query(...),
        token_param: str | None = Query(None, alias="token"),
        max_points: int = 300,
    ):
        if not verify_token(token_param, token):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Unauthorized")
        if not storage:
            raise HTTPException(status_code=404, detail="No storage available")

        l1 = storage.get_lap_by_id(lap1)
        l2 = storage.get_lap_by_id(lap2)
        if not l1 or not l2:
            raise HTTPException(status_code=404, detail="One or both laps not found")

        t1 = storage.get_lap_trace(lap1)
        t2 = storage.get_lap_trace(lap2)
        if not t1 or not t2:
            raise HTTPException(status_code=404, detail="Trace data missing for one or both laps")

        delta_res = compute_delta_trace(t1, t2)
        corners = analyze_corners(t1, t2)

        dists = delta_res["distance"]
        step = max(1, len(dists) // max_points) if len(dists) > max_points else 1

        track_pts = []
        for i in range(0, len(dists), step):
            d_val = float(delta_res["delta"][i])
            track_pts.append({
                "x": float(t1["world_x"][i]),
                "z": float(t1["world_z"][i]),
                "distance": float(dists[i]),
                "delta": round(d_val, 3),
            })

        return {
            "lap1": l1,
            "lap2": l2,
            "telemetry": {
                "distance": [float(x) for x in dists[::step]],
                "delta": [round(float(x), 3) for x in delta_res["delta"][::step]],
                "speed1": [round(float(x), 1) for x in t1["speed"][:len(dists)][::step]],
                "speed2": [round(float(x), 1) for x in t2["speed"][:len(dists)][::step]],
                "throttle1": [round(float(x), 2) for x in t1["throttle"][:len(dists)][::step]],
                "throttle2": [round(float(x), 2) for x in t2["throttle"][:len(dists)][::step]],
                "brake1": [round(float(x), 2) for x in t1["brake"][:len(dists)][::step]],
                "brake2": [round(float(x), 2) for x in t2["brake"][:len(dists)][::step]],
            },
            "corners": corners,
            "track_points": track_pts,
            "total_delta_s": round(float(l1["lap_time_s"] - l2["lap_time_s"]), 3),
        }

    @app.get("/api/sessions/{session_id}/stints")
    async def get_session_stints(
        session_id: int,
        token_param: str | None = Query(None, alias="token"),
    ):
        if not verify_token(token_param, token):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Unauthorized")
        if not storage:
            return []
        laps = storage.get_laps_for_session(session_id)
        if not laps:
            return []
        stints_map: dict[int, list[dict]] = {}
        for l in laps:
            st = l.get("stint", 0)
            stints_map.setdefault(st, []).append(l)

        results = []
        for st_id, st_laps in sorted(stints_map.items()):
            analysis = analyze_stint(st_laps)
            compound_id = st_laps[0].get("tyre_compound") if st_laps else None
            analysis["stint_id"] = st_id
            analysis["compound_name"] = COMPOUNDS.get(compound_id, "Unknown")
            results.append(analysis)
        return results

    # ==================== SETUPS API ENDPOINTS ====================

    @app.get("/api/setups/schema")
    async def get_setups_schema(
        car_class: int = Query(0),
        token_param: str | None = Query(None, alias="token"),
        authorization: str | None = Header(None, alias="Authorization"),
        x_token: str | None = Header(None, alias="X-Token"),
    ):
        if not check_token(token_param, authorization, x_token):
            raise HTTPException(status_code=403, detail="Unauthorized")
        return get_setup_schema(car_class=car_class, storage=setups_store)

    @app.post("/api/setups")
    async def create_setup(
        body: SetupCreateRequest,
        token_param: str | None = Query(None, alias="token"),
        authorization: str | None = Header(None, alias="Authorization"),
        x_token: str | None = Header(None, alias="X-Token"),
    ):
        if not check_token(token_param, authorization, x_token):
            raise HTTPException(status_code=403, detail="Unauthorized")

        val = validate_setup_input(body.params, car_class=body.car_class, storage=setups_store)
        if not val["valid"]:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"message": "Validation failed", "out_of_range": val["out_of_range"]}
            )

        clean_params = val["clean_params"]
        fp = compute_setup_fingerprint(clean_params)
        existing = setups_store.get_setup_by_fingerprint(fp)

        if existing and not body.force:
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content={
                    "saved": False,
                    "duplicate": True,
                    "error": "duplicate_setup",
                    "existing_setup": existing,
                    "fingerprint": fp,
                    "message": f"An identical setup '{existing.get('name', 'Setup')}' already exists in your library.",
                },
            )

        saved, _ = setups_store.create_custom_setup(
            params=clean_params,
            car_class=body.car_class,
            team_id=body.team_id,
            track_id=body.track_id,
            conditions=body.conditions,
            session_type=body.session_type,
            name=body.name,
            source_note=body.source_note,
            source_link=body.source_link,
            notes=body.notes,
            force=True,
        )
        return JSONResponse(
            status_code=status.HTTP_201_CREATED,
            content=saved,
        )

    @app.get("/api/setups")
    async def get_setups(
        track_id: int | None = Query(None),
        car_class: int | None = Query(None),
        conditions: str | None = Query(None),
        session_type: str | None = Query(None),
        token_param: str | None = Query(None, alias="token"),
        authorization: str | None = Header(None, alias="Authorization"),
        x_token: str | None = Header(None, alias="X-Token"),
    ):
        if not check_token(token_param, authorization, x_token):
            raise HTTPException(status_code=403, detail="Unauthorized")

        s = state.session
        cur_track = s.track_id if s else 7
        cur_class = s.formula if s else 0
        cur_weather = s.weather if s else 0
        cur_cond = "wet" if cur_weather in (3, 4, 5) else "dry"
        cur_stype = s.session_type if s else 10
        cur_stype_str = "quali" if 5 <= cur_stype <= 9 else ("race" if 10 <= cur_stype <= 12 else "practice")

        # Fall back to live session values if filter not provided
        eff_track = track_id if track_id is not None else cur_track
        eff_class = car_class if car_class is not None else cur_class
        eff_cond = conditions if conditions is not None else cur_cond
        eff_stype = session_type if session_type is not None else cur_stype_str

        ranked = setups_store.get_ranked_setups(
            storage,
            track_id=eff_track,
            car_class=eff_class,
            conditions=eff_cond,
            session_type=eff_stype,
            current_setup_id=state.current_setup_id,
            current_fingerprint=state.current_setup_fingerprint,
        )

        return {
            "setups": ranked,
            "current_setup_id": state.current_setup_id,
            "current_fingerprint": state.current_setup_fingerprint,
            "current_session": {
                "track_id": cur_track,
                "track_name": TRACKS.get(cur_track, f"Track {cur_track}"),
                "car_class": cur_class,
                "car_class_name": FORMULAS.get(cur_class, f"Class {cur_class}"),
                "conditions": cur_cond,
                "session_type": cur_stype_str,
            },
            "active_filters": {
                "track_id": eff_track,
                "car_class": eff_class,
                "conditions": eff_cond,
                "session_type": eff_stype,
            },
            "tracks": [{"id": k, "name": v} for k, v in sorted(TRACKS.items())],
            "formulas": [{"id": k, "name": v} for k, v in sorted(FORMULAS.items())],
        }

    @app.get("/api/setups/current")
    async def get_current_setup(
        token_param: str | None = Query(None, alias="token"),
        authorization: str | None = Header(None, alias="Authorization"),
        x_token: str | None = Header(None, alias="X-Token"),
    ):
        if not check_token(token_param, authorization, x_token):
            raise HTTPException(status_code=403, detail="Unauthorized")
        my = state.my_setup
        if not my:
            return {"active": False, "setup": None}
        return {
            "active": True,
            "id": state.current_setup_id or "current",
            "fingerprint": state.current_setup_fingerprint,
            "name": "Live Mounted Setup",
            "params": my.to_dict(),
        }

    @app.get("/api/setups/diff")
    async def get_setups_diff(
        id1: str = Query(...),
        id2: str = Query(...),
        token_param: str | None = Query(None, alias="token"),
    ):
        if not verify_token(token_param, token):
            raise HTTPException(status_code=403, detail="Unauthorized")

        def resolve_setup(sid: str) -> dict:
            if sid == "current":
                my = state.my_setup
                return {
                    "id": "current",
                    "name": "Current Car Setup",
                    "params": my.to_dict() if my else {},
                }
            res = setups_store.get_setup(sid)
            if not res:
                raise HTTPException(status_code=404, detail=f"Setup {sid} not found")
            return res

        setup_a = resolve_setup(id1)
        setup_b = resolve_setup(id2)
        return diff_setups(setup_a, setup_b)

    @app.get("/api/setups/template/csv")
    async def get_csv_template():
        tmpl = generate_csv_template()
        return PlainTextResponse(
            tmpl,
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="f1_25_setup_template.csv"'},
        )

    @app.get("/api/setups/{setup_id}/export")
    async def export_setup(
        setup_id: str,
        format: str = Query("sheet"),
        token_param: str | None = Query(None, alias="token"),
    ):
        if not verify_token(token_param, token):
            raise HTTPException(status_code=403, detail="Unauthorized")

        if setup_id == "current":
            my = state.my_setup
            s = state.session
            t_name = TRACKS.get(s.track_id, f"Track {s.track_id}") if s else "Current Track"
            setup = {
                "id": "current",
                "name": "Current Car Setup",
                "fingerprint": state.current_setup_fingerprint or "--",
                "track_name": t_name,
                "conditions": "wet" if (s and s.weather in (3, 4, 5)) else "dry",
                "session_type": "race",
                "source": "captured",
                "source_note": "Direct from telemetry on car",
                "params": my.to_dict() if my else {},
            }
        else:
            setup = setups_store.get_setup(setup_id)
            if not setup:
                raise HTTPException(status_code=404, detail="Setup not found")
            setup["track_name"] = TRACKS.get(setup.get("track_id", 0), f"Track {setup.get('track_id', 0)}")

        if format.lower() == "json":
            return PlainTextResponse(
                export_as_json(setup),
                media_type="application/json",
                headers={"Content-Disposition": f'attachment; filename="setup_{setup_id}.json"'},
            )
        else:
            return PlainTextResponse(
                export_as_sheet(setup),
                media_type="text/plain",
                headers={"Content-Disposition": f'attachment; filename="setup_{setup_id}.txt"'},
            )

    @app.get("/api/setups/{setup_id}")
    async def get_setup_details(
        setup_id: str,
        token_param: str | None = Query(None, alias="token"),
        authorization: str | None = Header(None, alias="Authorization"),
        x_token: str | None = Header(None, alias="X-Token"),
    ):
        if not check_token(token_param, authorization, x_token):
            raise HTTPException(status_code=403, detail="Unauthorized")
        if setup_id == "current":
            my = state.my_setup
            return {
                "id": "current",
                "name": "Live Setup on Car",
                "fingerprint": state.current_setup_fingerprint,
                "params": my.to_dict() if my else {},
            }
        s = setups_store.get_setup(setup_id)
        if not s:
            raise HTTPException(status_code=404, detail="Setup not found")
        return s

    @app.put("/api/setups/{setup_id}")
    async def update_setup(
        setup_id: str,
        body: SetupUpdateRequest,
        token_param: str | None = Query(None, alias="token"),
        authorization: str | None = Header(None, alias="Authorization"),
        x_token: str | None = Header(None, alias="X-Token"),
    ):
        if not check_token(token_param, authorization, x_token):
            raise HTTPException(status_code=403, detail="Unauthorized")

        existing = setups_store.get_setup(setup_id)
        if not existing:
            raise HTTPException(status_code=404, detail="Setup not found")

        if existing.get("source") == "captured" and not body.allow_fork:
            raise HTTPException(
                status_code=400,
                detail="Captured setups are read-only. Editing creates a new imported copy. Set allow_fork=true to save as a copy."
            )

        update_data = body.model_dump(exclude_unset=True)
        if body.params is not None:
            eff_class = body.car_class if body.car_class is not None else existing.get("car_class", 0)
            val = validate_setup_input(body.params, car_class=eff_class, storage=setups_store)
            if not val["valid"]:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail={"message": "Validation failed", "out_of_range": val["out_of_range"]}
                )
            update_data["params"] = val["clean_params"]

        try:
            updated = setups_store.update_setup(setup_id, update_data, allow_fork=body.allow_fork)
            is_forked = existing.get("source") == "captured" and body.allow_fork
            return {
                **updated,
                "updated": True,
                "forked": is_forked,
                "setup": updated,
            }
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.delete("/api/setups/{setup_id}")
    async def delete_setup(
        setup_id: str,
        token_param: str | None = Query(None, alias="token"),
        authorization: str | None = Header(None, alias="Authorization"),
        x_token: str | None = Header(None, alias="X-Token"),
    ):
        if not check_token(token_param, authorization, x_token):
            raise HTTPException(status_code=403, detail="Unauthorized")

        existing = setups_store.get_setup(setup_id)
        if not existing:
            raise HTTPException(status_code=404, detail="Setup not found")

        if existing.get("source") == "captured":
            raise HTTPException(status_code=400, detail="Captured setups are read-only and cannot be deleted.")

        try:
            deleted = setups_store.delete_setup(setup_id)
            return {"deleted": deleted, "id": setup_id}
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.post("/api/setups/import")
    async def import_setup(
        request: Request,
        token_param: str | None = Query(None, alias="token"),
    ):
        if not verify_token(token_param, token):
            raise HTTPException(status_code=403, detail="Unauthorized")

        body = await request.json()
        mode = body.get("mode", "json")
        content = body.get("content", "")
        save_confirmed = bool(body.get("save", False))

        car_class = int(body.get("car_class", state.session.formula if state.session else 0))
        track_id = int(body.get("track_id", state.session.track_id if state.session else 7))
        conditions = body.get("conditions", "dry")
        session_type = body.get("session_type", "race")
        name = body.get("name", "").strip()
        source_note = body.get("source_note", "").strip()

        params: dict[str, Any] = {}
        if mode == "json":
            parsed = import_from_json(content)
            params = parsed.get("params", {})
            name = name or parsed.get("name", "")
            car_class = parsed.get("car_class", car_class)
            track_id = parsed.get("track_id", track_id)
            conditions = parsed.get("conditions", conditions)
            session_type = parsed.get("session_type", session_type)
            source_note = source_note or parsed.get("source_note", "")
        elif mode == "csv":
            params = import_from_csv(content)
        elif mode == "freetext":
            if not llm_client:
                raise HTTPException(status_code=400, detail="No LLM provider available for free text parsing")
            params = await parse_free_text_setup(content, llm_client)
        else:
            raise HTTPException(status_code=400, detail=f"Unsupported import mode: {mode}")

        ranges = setups_store.get_param_ranges(car_class)
        val = validate_setup_params(params, car_class=car_class, ranges=ranges)

        if not save_confirmed:
            # Preview only: highlight missing, out-of-range, and parsed params
            return {
                "saved": False,
                "preview": True,
                "valid": val["valid"],
                "params": val["params"],
                "missing": val["missing"],
                "out_of_range": val["out_of_range"],
                "unknown": val["unknown"],
                "name": name,
                "car_class": car_class,
                "track_id": track_id,
                "conditions": conditions,
                "session_type": session_type,
            }

        # Save only on explicit confirmation
        saved = setups_store.save_imported_setup(
            params=val["params"],
            car_class=car_class,
            track_id=track_id,
            conditions=conditions,
            session_type=session_type,
            source_note=source_note or f"Imported ({mode})",
            name=name or f"Imported {conditions.capitalize()} ({mode})",
        )
    # ==================== TRACKS API ENDPOINTS ====================

    @app.get("/api/tracks")
    async def get_tracks(
        token_param: str | None = Query(None, alias="token"),
        authorization: str | None = Header(None, alias="Authorization"),
        x_token: str | None = Header(None, alias="X-Token"),
    ):
        if not check_token(token_param, authorization, x_token):
            raise HTTPException(status_code=403, detail="Unauthorized")
        return {"tracks": knowledge_base.list_available_tracks()}

    @app.get("/api/tracks/{track}")
    async def get_track_notes(
        track: str,
        token_param: str | None = Query(None, alias="token"),
        authorization: str | None = Header(None, alias="Authorization"),
        x_token: str | None = Header(None, alias="X-Token"),
    ):
        if not check_token(token_param, authorization, x_token):
            raise HTTPException(status_code=403, detail="Unauthorized")
        return knowledge_base.get_track_markdown(track)

    @app.put("/api/tracks/{track}")
    async def save_track_notes(
        track: str,
        body: TrackSaveRequest,
        token_param: str | None = Query(None, alias="token"),
        authorization: str | None = Header(None, alias="Authorization"),
        x_token: str | None = Header(None, alias="X-Token"),
    ):
        if not check_token(token_param, authorization, x_token):
            raise HTTPException(status_code=403, detail="Unauthorized")

        raw_text = body.raw or body.raw_markdown
        if raw_text is not None:
            saved = knowledge_base.save_track_markdown(track, raw_text)
        elif body.sections is not None or body.corners_list is not None:
            sec_dict = body.sections.model_dump() if body.sections else {}
            if body.corners_list is not None:
                sec_dict["corners"] = [c.model_dump() for c in body.corners_list]
            saved = knowledge_base.save_track_markdown(track, sec_dict)
        else:
            raise HTTPException(status_code=400, detail="Missing track content: provide 'raw' markdown or 'sections'")

        cnt = len(saved.get("corners_list") or saved.get("corners") or [])
        return {
            "status": "saved",
            "saved": True,
            "track": track,
            "corners_count": cnt,
            "data": saved,
        }

    @app.websocket("/ws/live")
    async def ws_live(websocket: WebSocket, token_param: str | None = Query(None, alias="token")):
        if not verify_token(token_param, token):
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Unauthorized")
            return

        await websocket.accept()
        q = broadcaster.register()
        try:
            while True:
                msg = await q.get()
                await websocket.send_text(msg)
        except (WebSocketDisconnect, ConnectionResetError):
            pass
        finally:
            broadcaster.unregister(q)

    return app


class DashboardServer:
    """Encapsulates the uvicorn.Server, broadcast task, and broadcaster."""
    def __init__(
        self,
        state: RaceState,
        config: AppConfig,
        config_path: str = "config.yaml",
        track_map: TrackMapManager | None = None,
        storage: SessionStorage | None = None,
        setups_storage: SetupsStorage | None = None,
        llm_client: Any | None = None,
        knowledge: KnowledgeBase | None = None,
    ):
        self.state = state
        self.config = config
        self.token = ensure_token(config, config_path)
        self.broadcaster = Broadcaster()
        self.track_map = track_map or TrackMapManager()
        self.storage = storage
        self.setups_storage = setups_storage or SetupsStorage()
        self.llm_client = llm_client
        self.knowledge = knowledge or KnowledgeBase()
        self.app = create_app(
            self.state,
            self.broadcaster,
            self.token,
            self.track_map,
            self.storage,
            self.setups_storage,
            self.llm_client,
            knowledge=self.knowledge,
        )
        self._server: uvicorn.Server | None = None
        self._server_task: asyncio.Task | None = None
        self._broadcast_task: asyncio.Task | None = None

    def start(self, loop: asyncio.AbstractEventLoop | None = None) -> tuple[asyncio.Task, asyncio.Task]:
        """Starts uvicorn.Server and the broadcast loop inside the existing asyncio loop."""
        host = self.config.dashboard.host
        port = self.config.dashboard.port
        hz = min(max(1.0, self.config.dashboard.hz), 20.0)

        print_dashboard_banner(host, port, self.token)

        uvi_cfg = uvicorn.Config(
            self.app,
            host=host,
            port=port,
            log_level="warning",
            loop="asyncio",
            access_log=False,
        )
        self._server = uvicorn.Server(uvi_cfg)

        self._server_task = asyncio.create_task(self._server.serve())
        self._broadcast_task = asyncio.create_task(self._run_broadcast(hz))
        return self._server_task, self._broadcast_task

    async def _run_broadcast(self, hz: float):
        interval = 1.0 / hz
        try:
            while True:
                if self.track_map:
                    self.track_map.update_from_state(self.state)
                payload = build_live_payload(self.state, self.track_map)
                self.broadcaster.broadcast_state(payload)
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            pass

    async def stop(self):
        if self._broadcast_task and not self._broadcast_task.done():
            self._broadcast_task.cancel()
        if self._server:
            self._server.should_exit = True
        if self._server_task and not self._server_task.done():
            self._server_task.cancel()
