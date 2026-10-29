"""FastAPI web dashboard server for live F1 25 telemetry and radio comms."""
from __future__ import annotations

import asyncio
from collections import deque
import hmac
import json
import logging
import re
import secrets
import socket
import time
import zlib
from pathlib import Path
from typing import Any

import qrcode
import uvicorn
from fastapi import Body, FastAPI, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect, status
from fastapi.responses import FileResponse, PlainTextResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from config import AppConfig

log = logging.getLogger("dashboard")
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



class SpeakerTestRequest(BaseModel):
    with_radio_fx: bool = True
    volume: float = 1.0


class AudioDevicesUpdateRequest(BaseModel):
    input_device: str | int | None = None
    output_device: str | int | None = None


class VolumesUpdateRequest(BaseModel):
    engineer: float | None = None
    spotter: float | None = None
    menu_beeps: float | None = None


class PortsUpdateRequest(BaseModel):
    telemetry_port: int | None = None
    dashboard_port: int | None = None
    ask_port: bool | None = None


def build_live_payload(state: RaceState, track_map: TrackMapManager | None = None) -> dict[str, Any]:
    """Combines snapshot(), timing_tower(), motion/car positions, and telemetry extras."""
    payload = state.snapshot()
    is_live = (state.last_packet_time > 0 and (time.monotonic() - state.last_packet_time) < 4.0 and state.session is not None)
    payload["session_live"] = is_live
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
        self._radio_history: deque[dict[str, Any]] = deque(maxlen=100)

    @property
    def client_count(self) -> int:
        return len(self._clients)

    def get_radio_history(self) -> list[dict[str, Any]]:
        return list(self._radio_history)

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

    def broadcast_state(self, payload: dict[str, Any], force: bool = False) -> bool:
        """Pushes state payload if changed or forced. Returns True if broadcast occurred."""
        if not force and not has_changed(payload, self._last_payload):
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
        self._radio_history.append(msg)
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
    config: AppConfig | None = None,
    engineer: Any | None = None,
    usage_guard: Any | None = None,
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
            return FileResponse(f, media_type="text/css", headers={"Cache-Control": "no-cache, must-revalidate"})
        raise HTTPException(status_code=404)

    @app.get("/app.js")
    async def get_script():
        f = STATIC_DIR / "app.js"
        if f.is_file():
            return FileResponse(f, media_type="application/javascript", headers={"Cache-Control": "no-cache, must-revalidate"})
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

    @app.get("/api/session/timeline")
    async def get_session_timeline(
        session_uid: str | None = None,
        token_param: str | None = Query(None, alias="token"),
    ):
        if not verify_token(token_param, token):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Unauthorized")
        uid = session_uid or str(state.session_uid or "")
        if not uid or not storage:
            return {"session_uid": uid, "samples": []}
        chunks = storage.get_timeline(uid)
        samples = []
        for c in chunks:
            try:
                raw_json = zlib.decompress(c["data"]).decode("utf-8")
                samples.extend(json.loads(raw_json))
            except Exception as e:
                log.warning("Failed to decompress timeline chunk: %s", e)
        return {"session_uid": uid, "samples": samples}

    @app.get("/api/session/overtakes")
    async def get_session_overtakes(
        session_uid: str | None = None,
        token_param: str | None = Query(None, alias="token"),
    ):
        if not verify_token(token_param, token):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Unauthorized")
        uid = session_uid or str(state.session_uid or "")
        if not uid or not storage:
            return {"session_uid": uid, "overtakes": []}
        return {"session_uid": uid, "overtakes": storage.get_overtakes(uid)}

    @app.get("/api/sessions/{session_id}/overtakes")
    async def get_session_overtakes_by_id(
        session_id: int,
        token_param: str | None = Query(None, alias="token"),
    ):
        if not verify_token(token_param, token):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Unauthorized")
        if not storage:
            return {"session_id": session_id, "overtakes": []}
        sess = storage.get_session_by_id(session_id)
        if not sess:
            return {"session_id": session_id, "overtakes": []}
        uid = sess.get("session_uid", "")
        return {"session_id": session_id, "session_uid": uid, "overtakes": storage.get_overtakes(uid)}

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

    @app.get("/api/settings/persona")
    async def get_persona_settings(
        token_param: str | None = Query(None, alias="token"),
        authorization: str | None = Header(None),
        x_token: str | None = Header(None),
    ):
        if not check_token(token_param, authorization, x_token):
            raise HTTPException(status_code=403, detail="Forbidden")

        pers_cfg = config.persona if config else None
        name = pers_cfg.name if pers_cfg else "cocky"
        prof = pers_cfg.profanity if pers_cfg else "mild"
        freq = pers_cfg.reaction_frequency if pers_cfg else "normal"
        gap = pers_cfg.min_gap_s if pers_cfg else 20.0

        lines_p = Path("lines")
        available = ["professional"]
        if lines_p.is_dir():
            for f in lines_p.glob("*.yaml"):
                if f.stem not in available:
                    available.append(f.stem)
        if "cocky" not in available:
            available.insert(0, "cocky")

        return {
            "name": name,
            "profanity": prof,
            "reaction_frequency": freq,
            "min_gap_s": gap,
            "available_personas": sorted(available),
            "available_profanities": ["none", "mild", "strong"],
            "available_frequencies": ["low", "normal", "high"],
        }

    @app.post("/api/settings/persona")
    async def update_persona_settings(
        payload: dict = Body(...),
        token_param: str | None = Query(None, alias="token"),
        authorization: str | None = Header(None),
        x_token: str | None = Header(None),
    ):
        if not check_token(token_param, authorization, x_token):
            raise HTTPException(status_code=403, detail="Forbidden")

        name = payload.get("name")
        prof = payload.get("profanity")
        freq = payload.get("reaction_frequency")
        gap = payload.get("min_gap_s")

        if config and config.persona:
            if name is not None:
                config.persona.name = str(name).strip().lower()
            if prof is not None:
                config.persona.profanity = str(prof).strip().lower()
            if freq is not None:
                config.persona.reaction_frequency = str(freq).strip().lower()
            if gap is not None:
                try:
                    config.persona.min_gap_s = float(gap)
                except (ValueError, TypeError):
                    pass

        if engineer:
            if hasattr(engineer, "personality") and engineer.personality:
                if config and config.persona:
                    engineer.personality.config = config.persona
                if name is not None:
                    engineer.personality.reload_persona(str(name).strip().lower())
            if hasattr(engineer, "agent") and engineer.agent:
                if name is not None:
                    engineer.agent.persona_name = str(name).strip().lower()
                if prof is not None:
                    engineer.agent.profanity = str(prof).strip().lower()

        log.info("Live updated persona settings: name=%s, profanity=%s", name, prof)
        return {
            "status": "ok",
            "name": config.persona.name if (config and config.persona) else name,
            "profanity": config.persona.profanity if (config and config.persona) else prof,
            "reaction_frequency": config.persona.reaction_frequency if (config and config.persona) else freq,
            "min_gap_s": config.persona.min_gap_s if (config and config.persona) else gap,
        }

    @app.get("/api/usage")
    async def get_usage_status(
        token_param: str | None = Query(None, alias="token"),
        authorization: str | None = Header(None),
        x_token: str | None = Header(None),
    ):
        if not check_token(token_param, authorization, x_token):
            raise HTTPException(status_code=403, detail="Forbidden")

        guard = usage_guard or (getattr(engineer, "usage_guard", None) if engineer else None)
        if guard:
            return guard.get_status()
        return {
            "date": "",
            "allow_paid": False,
            "max_fraction": 0.8,
            "last_provider": "none",
            "services": {},
        }

    @app.get("/api/usage/history")
    async def get_usage_history(
        days: int = Query(7, ge=1, le=90),
        token_param: str | None = Query(None, alias="token"),
        authorization: str | None = Header(None),
        x_token: str | None = Header(None),
    ):
        if not check_token(token_param, authorization, x_token):
            raise HTTPException(status_code=403, detail="Forbidden")

        guard = usage_guard or (getattr(engineer, "usage_guard", None) if engineer else None)
        if guard:
            return {
                "days": days,
                "history": guard.get_daily_history(days),
                "preflight": guard.run_preflight_check(print_banner=False),
            }
        return {
            "days": days,
            "history": [],
            "preflight": {
                "health": "OK",
                "overall_pct": 0.0,
                "cutoff_pct": 80.0,
                "message": "Local only",
                "seconds_to_reset": 0,
                "countdown_str": "--",
                "services": [],
            },
        }

    @app.post("/api/sound_test/speaker")
    async def post_sound_test_speaker(
        req: SpeakerTestRequest,
        token_param: str | None = Query(None, alias="token"),
        authorization: str | None = Header(None),
        x_token: str | None = Header(None),
    ):
        if not check_token(token_param, authorization, x_token):
            raise HTTPException(status_code=403, detail="Forbidden")

        st_mgr = getattr(engineer, "sound_test", None)
        if st_mgr:
            res = await st_mgr.play_speaker_test(with_radio_fx=req.with_radio_fx, volume=req.volume)
            if res.get("status") == "blocked":
                raise HTTPException(status_code=409, detail=res.get("message", "Use radio check in game instead"))
            return res
        return {"status": "error", "message": "Sound test manager not initialized"}

    @app.post("/api/sound_test/mic/start")
    async def post_sound_test_mic_start(
        token_param: str | None = Query(None, alias="token"),
        authorization: str | None = Header(None),
        x_token: str | None = Header(None),
    ):
        if not check_token(token_param, authorization, x_token):
            raise HTTPException(status_code=403, detail="Forbidden")

        st_mgr = getattr(engineer, "sound_test", None)
        if st_mgr:
            res = st_mgr.start_mic_test()
            if res.get("status") == "blocked":
                raise HTTPException(status_code=409, detail=res.get("message", "Use radio check in game instead"))
            return res
        return {"status": "error", "message": "Sound test manager not initialized"}

    @app.post("/api/sound_test/mic/stop")
    async def post_sound_test_mic_stop(
        token_param: str | None = Query(None, alias="token"),
        authorization: str | None = Header(None),
        x_token: str | None = Header(None),
    ):
        if not check_token(token_param, authorization, x_token):
            raise HTTPException(status_code=403, detail="Forbidden")

        st_mgr = getattr(engineer, "sound_test", None)
        if st_mgr:
            return await st_mgr.stop_mic_test_and_evaluate(play_back=True)
        return {"status": "error", "message": "Sound test manager not initialized"}

    @app.get("/api/sound_test/mic/level")
    async def get_sound_test_mic_level(
        token_param: str | None = Query(None, alias="token"),
        authorization: str | None = Header(None),
        x_token: str | None = Header(None),
    ):
        if not check_token(token_param, authorization, x_token):
            raise HTTPException(status_code=403, detail="Forbidden")

        st_mgr = getattr(engineer, "sound_test", None)
        if st_mgr and st_mgr.capture and hasattr(st_mgr.capture, "get_live_level"):
            return st_mgr.capture.get_live_level()
        return {"peak": 0.0, "rms": 0.0, "peak_db": -100.0, "clipping": False, "is_recording": False}

    @app.get("/api/audio/devices")
    async def get_audio_devices_endpoint(
        token_param: str | None = Query(None, alias="token"),
        authorization: str | None = Header(None),
        x_token: str | None = Header(None),
    ):
        if not check_token(token_param, authorization, x_token):
            raise HTTPException(status_code=403, detail="Forbidden")

        st_mgr = getattr(engineer, "sound_test", None)
        if st_mgr:
            return st_mgr.get_devices()
        from audio_devices import get_audio_devices
        data = get_audio_devices()
        data["current_input"] = config.audio.input_device if config else None
        data["current_output"] = config.audio.output_device if config else None
        return data

    @app.post("/api/audio/devices")
    async def post_audio_devices_endpoint(
        req: AudioDevicesUpdateRequest,
        token_param: str | None = Query(None, alias="token"),
        authorization: str | None = Header(None),
        x_token: str | None = Header(None),
    ):
        if not check_token(token_param, authorization, x_token):
            raise HTTPException(status_code=403, detail="Forbidden")

        st_mgr = getattr(engineer, "sound_test", None)
        if st_mgr:
            return st_mgr.change_devices(input_device=req.input_device, output_device=req.output_device)
        return {"status": "error", "message": "Sound test manager not initialized"}

    @app.get("/api/settings/volumes")
    async def get_volumes_endpoint(
        token_param: str | None = Query(None, alias="token"),
        authorization: str | None = Header(None),
        x_token: str | None = Header(None),
    ):
        if not check_token(token_param, authorization, x_token):
            raise HTTPException(status_code=403, detail="Forbidden")

        if config and hasattr(config, "volumes"):
            return {
                "engineer": config.volumes.engineer,
                "spotter": config.volumes.spotter,
                "menu_beeps": config.volumes.menu_beeps,
            }
        return {"engineer": 1.0, "spotter": 1.0, "menu_beeps": 1.0}

    @app.post("/api/settings/volumes")
    async def post_volumes_endpoint(
        req: VolumesUpdateRequest,
        token_param: str | None = Query(None, alias="token"),
        authorization: str | None = Header(None),
        x_token: str | None = Header(None),
    ):
        if not check_token(token_param, authorization, x_token):
            raise HTTPException(status_code=403, detail="Forbidden")

        st_mgr = getattr(engineer, "sound_test", None)
        if st_mgr:
            return st_mgr.update_volumes(engineer=req.engineer, spotter=req.spotter, menu_beeps=req.menu_beeps)
        if config and hasattr(config, "volumes"):
            if req.engineer is not None:
                config.volumes.engineer = req.engineer
            if req.spotter is not None:
                config.volumes.spotter = req.spotter
            if req.menu_beeps is not None:
                config.volumes.menu_beeps = req.menu_beeps
            return {
                "status": "ok",
                "engineer": config.volumes.engineer,
                "spotter": config.volumes.spotter,
                "menu_beeps": config.volumes.menu_beeps,
            }
        return {"status": "error", "message": "Volumes config unavailable"}

    @app.post("/api/radio_check")
    async def post_in_game_radio_check(
        token_param: str | None = Query(None, alias="token"),
        authorization: str | None = Header(None),
        x_token: str | None = Header(None),
    ):
        if not check_token(token_param, authorization, x_token):
            raise HTTPException(status_code=403, detail="Forbidden")

        if engineer and hasattr(engineer, "start_two_way_radio_check"):
            msg = engineer.start_two_way_radio_check()
            return {"status": "ok", "message": msg}
        return {"status": "error", "message": "Engineer unavailable for radio check"}

    @app.get("/api/settings/ports")
    async def get_ports_endpoint(
        token_param: str | None = Query(None, alias="token"),
        authorization: str | None = Header(None),
        x_token: str | None = Header(None),
    ):
        if not check_token(token_param, authorization, x_token):
            raise HTTPException(status_code=403, detail="Forbidden")

        t_port = config.telemetry.port if config and hasattr(config, "telemetry") else 20777
        d_port = config.dashboard.port if config and hasattr(config, "dashboard") else 8765
        ask = getattr(config.telemetry, "ask_port", False) if config and hasattr(config, "telemetry") else False
        return {
            "telemetry_port": t_port,
            "dashboard_port": d_port,
            "ask_port": ask,
        }

    @app.post("/api/settings/ports")
    async def post_ports_endpoint(
        req: PortsUpdateRequest,
        token_param: str | None = Query(None, alias="token"),
        authorization: str | None = Header(None),
        x_token: str | None = Header(None),
    ):
        if not check_token(token_param, authorization, x_token):
            raise HTTPException(status_code=403, detail="Forbidden")

        if config:
            if req.telemetry_port is not None and 1 <= req.telemetry_port <= 65535:
                config.telemetry.port = req.telemetry_port
            if req.dashboard_port is not None and 1 <= req.dashboard_port <= 65535:
                config.dashboard.port = req.dashboard_port
            if req.ask_port is not None:
                config.telemetry.ask_port = req.ask_port

            if config_path and Path(config_path).exists():
                try:
                    import yaml
                    with open(config_path, "r", encoding="utf-8") as f:
                        raw = yaml.safe_load(f) or {}
                    if "telemetry" not in raw or not isinstance(raw["telemetry"], dict):
                        raw["telemetry"] = {}
                    if req.telemetry_port is not None:
                        raw["telemetry"]["port"] = req.telemetry_port
                    if req.ask_port is not None:
                        raw["telemetry"]["ask_port"] = req.ask_port
                    if "dashboard" not in raw or not isinstance(raw["dashboard"], dict):
                        raw["dashboard"] = {}
                    if req.dashboard_port is not None:
                        raw["dashboard"]["port"] = req.dashboard_port
                    with open(config_path, "w", encoding="utf-8") as f:
                        yaml.safe_dump(raw, f, sort_keys=False)
                except Exception as e:
                    log.warning("Could not persist ports to %s: %s", config_path, e)

            return {
                "status": "ok",
                "telemetry_port": config.telemetry.port,
                "dashboard_port": config.dashboard.port,
                "ask_port": getattr(config.telemetry, "ask_port", False),
                "note": "Port settings saved. Active listener port updates on app restart.",
            }
        return {"status": "error", "message": "Config unavailable"}


    @app.websocket("/ws/live")
    async def ws_live(websocket: WebSocket, token_param: str | None = Query(None, alias="token")):
        if not verify_token(token_param, token):
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Unauthorized")
            return

        await websocket.accept()
        q = broadcaster.register()
        try:
            # Immediately send full current snapshot plus history upon connect
            snap = build_live_payload(state, track_map)
            laps_history = [
                {
                    "lap": l.lap,
                    "lap_time_ms": l.lap_time_ms,
                    "invalid": l.invalid,
                    "position": l.position,
                    "gap_ahead_ms": l.gap_ahead_ms,
                    "gap_behind_ms": l.gap_behind_ms,
                }
                for l in state.laps
            ]
            radio_hist = broadcaster.get_radio_history()
            overtakes_hist = []
            if storage and state.session_uid:
                overtakes_hist = storage.get_overtakes(state.session_uid)

            snap["radio_history"] = radio_hist
            snap["overtakes"] = overtakes_hist
            snap["history"] = {
                "laps": laps_history,
                "radio_log": radio_hist,
                "timing_tower": snap.get("timing_tower", []),
                "overtakes": overtakes_hist,
            }
            await websocket.send_text(json.dumps(snap))

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
        engineer: Any | None = None,
        usage_guard: Any | None = None,
    ):
        self.state = state
        self.config = config
        self.engineer = engineer
        self.usage_guard = usage_guard
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
            config=self.config,
            engineer=self.engineer,
            usage_guard=self.usage_guard,
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
        last_heartbeat = 0.0
        last_1hz_time = 0.0
        chunk_index = 0
        chunk_start_time = time.time()
        timeline_samples: list[dict[str, Any]] = []

        try:
            while True:
                now = time.monotonic()
                wall_now = time.time()
                force_push = (now - last_heartbeat) >= 1.0
                if self.track_map:
                    self.track_map.update_from_state(self.state)
                payload = build_live_payload(self.state, self.track_map)
                guard = self.usage_guard or (getattr(self.engineer, "usage_guard", None) if self.engineer else None)
                if guard:
                    st = guard.get_status()
                    payload["cloud_quota"] = {
                        "pct": st.get("overall_quota_pct", 0.0),
                        "cutoff_pct": st.get("cutoff_pct", 80.0),
                        "countdown_str": st.get("countdown_str", "--"),
                        "last_provider": st.get("last_provider", "none"),
                    }
                pushed = self.broadcaster.broadcast_state(payload, force=force_push)
                if pushed:
                    last_heartbeat = now

                # 1 Hz snapshot timeline recording in compressed 10-second chunks
                if wall_now - last_1hz_time >= 1.0:
                    last_1hz_time = wall_now
                    s_uid = self.state.session_uid
                    if s_uid and s_uid != 0 and self.storage:
                        me = self.state.me
                        tel = self.state.my_telemetry
                        sample = {
                            "t": round(wall_now, 2),
                            "lap": me.lap_num if me else 0,
                            "pos": me.position if me else 0,
                            "speed": tel.speed_kph if tel else 0,
                            "throttle": tel.throttle if tel else 0.0,
                            "brake": tel.brake if tel else 0.0,
                            "steer": tel.steer if tel else 0.0,
                            "gear": tel.gear if tel else 0,
                            "rpm": tel.rpm if tel else 0,
                            "dist": me.lap_distance if me else 0.0,
                            "total_dist": getattr(me, "total_distance", 0.0) if me else 0.0,
                        }
                        timeline_samples.append(sample)
                        if len(timeline_samples) >= 10:
                            raw_b = json.dumps(timeline_samples).encode("utf-8")
                            comp_b = zlib.compress(raw_b, level=6)
                            self.storage.save_timeline_chunk(
                                s_uid, chunk_index, chunk_start_time, wall_now, len(timeline_samples), comp_b
                            )
                            chunk_index += 1
                            timeline_samples = []
                            chunk_start_time = wall_now

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
