"""Parsers for the F1 25 UDP telemetry formats: 2025 and the 2026 Season Pack (m_packetFormat 2026).

Only the packets and fields a race engineer needs are decoded. The 2026 layouts are taken from
EA's official "F1 25: 2026 Season Pack" spec (docs/udp_spec_2026.h): 24 cars per array, a 1-byte
engine temperature, an extra ERS float in CarStatus, 2-byte driver/team ids in Participants,
int16 g-forces in Motion, collision severity, and the new CarTelemetry2 packet (active aero,
overtake mode). The 2025 layouts are from the F1 25 spec as known before the season pack.

Every per-car parser derives the per-car stride from the packet length, so if a patch
(or the 2026 season update) appends fields to a car struct, the leading fields still
parse and a mismatch is logged once instead of silently producing garbage.
"""
from __future__ import annotations

import logging
import struct
from dataclasses import dataclass

log = logging.getLogger(__name__)

NUM_CARS = 22            # 2025 format; use num_cars(header) for the packet being parsed
KNOWN_FORMATS = {2025, 2026}
LATEST_FORMAT = 2026

HEADER = struct.Struct("<HBBBBBQfIIBB")                       # 29 bytes
LAP = struct.Struct("<IIHBHBHBHBfff" + "B" * 15 + "HHBfB")    # 57 bytes per car
TEL = struct.Struct("<HfffBbHBBH4H4B4BH4f4B")                 # 2025: 60 bytes per car
STATUS = struct.Struct("<BBBBBfffHHBBHBBBbfffBfffB")          # 2025: 55 bytes per car
TEL_2026 = struct.Struct("<HfffBbHBBH4H4B4BB4f4B")            # 2026: 59 (engine temp is uint8)
STATUS_2026 = struct.Struct("<BBBBBfffHHBBHBBBbfffBffffB")    # 2026: 59 (+ ERS harvest limit)
MOTION_2026 = struct.Struct("<ffffff6h3hfff")                 # 2026: 54 (int16 g-force / 1000)
TEL2_2026 = struct.Struct("<BBHBBHBB")                        # 2026: 10 (CarTelemetry2)
DAMAGE_HEAD = struct.Struct("<4f4B4B")                        # wear, tyre dmg, brake dmg
DAMAGE_PARTS = struct.Struct("<10B")                          # wings .. engine
DAMAGE_BASE_STRIDE = 42                                       # F1 24 size; F1 25 adds 4 blister bytes
SESSION_HEAD = struct.Struct("<BbbBHBbBHHBBBBBB")             # 19 bytes
FORECAST = struct.Struct("<BBBbbbbB")                         # 8 bytes
MARSHAL_ZONES, MARSHAL_SIZE, FORECAST_SLOTS = 21, 5, 64


class PacketId:
    MOTION = 0
    SESSION = 1
    LAP_DATA = 2
    EVENT = 3
    PARTICIPANTS = 4
    CAR_TELEMETRY = 6
    CAR_STATUS = 7
    CAR_DAMAGE = 10
    CAR_TELEMETRY2 = 16


def udp_action(n: int) -> int:
    """Bit for the in-game 'UDP Action n' binding (1-12) in BUTN events."""
    return 0x00100000 << (n - 1)


@dataclass(slots=True)
class Header:
    packet_format: int
    game_year: int
    packet_version: int
    packet_id: int
    session_uid: int
    session_time: float
    frame: int
    player_idx: int


@dataclass(slots=True)
class LapData:
    last_lap_ms: int
    current_lap_ms: int
    gap_to_car_ahead_ms: int
    gap_to_leader_ms: int
    lap_distance: float
    position: int
    lap_num: int
    pit_status: int          # 0 none, 1 pitting, 2 in pit area
    num_pit_stops: int
    sector: int
    lap_invalid: bool
    penalties_s: int
    result_status: int       # 2 active, 3 finished, 4+ out
    total_distance: float = 0.0
    total_warnings: int = 0
    corner_cutting_warnings: int = 0
    grid_position: int = 0
    driver_status: int = 0   # 0 garage, 1 flying lap, 2 in lap, 3 out lap, 4 on track
    pit_lane_time_ms: int = 0
    speed_trap_kph: float = 0.0


@dataclass(slots=True)
class CarTelemetry:          # 4-wheel tuples are in F1 order: RL, RR, FL, FR
    speed_kph: int
    throttle: float
    brake: float
    gear: int
    rpm: int
    drs: bool
    brake_temps: tuple
    tyre_surface_temps: tuple
    tyre_inner_temps: tuple
    tyre_pressures: tuple


@dataclass(slots=True)
class CarStatus:
    fuel_mix: int
    brake_bias: int
    fuel_in_tank: float
    fuel_capacity: float
    fuel_laps_delta: float   # the +/- laps value shown on the MFD
    drs_allowed: bool
    actual_compound: int
    visual_compound: int
    tyre_age_laps: int
    fia_flag: int
    ers_store_j: float
    ers_mode: int


@dataclass(slots=True)
class CarDamage:
    tyre_wear: tuple         # percent, RL, RR, FL, FR
    tyre_damage: tuple
    brake_damage: tuple
    front_left_wing: int
    front_right_wing: int
    rear_wing: int
    floor: int
    diffuser: int
    sidepod: int
    gearbox: int
    engine: int


@dataclass(slots=True)
class ForecastSample:
    session_type: int
    minutes_ahead: int
    weather: int
    track_temp: int
    air_temp: int
    rain_pct: int


@dataclass(slots=True)
class Session:
    weather: int
    track_temp: int
    air_temp: int
    total_laps: int
    track_length_m: int
    session_type: int
    track_id: int
    time_left_s: int
    pit_speed_limit: int
    paused: bool
    safety_car: int          # 0 none, 1 full, 2 virtual, 3 formation lap
    forecast: list
    pit_ideal_lap: int
    pit_latest_lap: int
    pit_rejoin_pos: int


@dataclass(slots=True)
class Participant:
    name: str
    team_id: int
    race_number: int
    ai: bool


@dataclass(slots=True)
class CarMotion:
    x: float
    y: float
    z: float
    vx: float
    vy: float
    vz: float
    forward: tuple       # normalised world forward direction (x, y, z)
    right: tuple         # normalised world right direction (x, y, z)
    g_lat: float
    g_lon: float
    g_vert: float
    yaw: float


@dataclass(slots=True)
class CarTelemetry2:     # 2026 regulations
    active_aero_mode: int            # 0 = corner mode, 1 = straight mode
    active_aero_available: bool
    active_aero_in_m: int            # 0 = not available, else available in X metres
    overtake_available: bool
    overtake_active: bool
    overtake_in_m: int
    regs_2026: bool
    wrong_way: bool


@dataclass(slots=True)
class Event:
    code: str
    buttons: int | None = None
    vehicle_idx: int | None = None
    lap_time: float | None = None
    sc_type: int | None = None
    sc_event: int | None = None
    other_idx: int | None = None
    severity: int | None = None          # COLL: 0 low, 1 medium, 2 high (2026)
    penalty_type: int | None = None
    infringement_type: int | None = None
    time: int | None = None              # PENA: 255 = not applicable
    lap_num: int | None = None


_warned: set = set()


def _warn_once(key, msg, *args):
    if key not in _warned:
        _warned.add(key)
        log.warning(msg, *args)


def num_cars(h: Header) -> int:
    return 22 if h.packet_format <= 2025 else 24


def _stride(buf: bytes, name: str, expected: int, lead: int = 0, trailer: int = 0, ok=None,
            cars: int = NUM_CARS) -> int | None:
    body = len(buf) - HEADER.size - lead - trailer
    stride = body // cars
    if stride < expected:
        _warn_once((name, "short"), "%s packet too short (%d bytes); skipping", name, len(buf))
        return None
    if ok is None:
        ok = (expected,)
    if ok and (stride not in ok or body % cars):
        _warn_once((name, "size"), "%s packet is %d bytes, expected car stride %d; parsing leading fields only",
                   name, len(buf), expected)
    return stride


def parse_header(buf: bytes) -> Header | None:
    if len(buf) < HEADER.size:
        return None
    fmt, year, _maj, _min, ver, pid, uid, t, frame, _overall, player, _second = HEADER.unpack_from(buf)
    return Header(fmt, year, ver, pid, uid, t, frame, player)


def parse_lap_data(buf: bytes, h: Header):
    n = num_cars(h)
    stride = _stride(buf, "LapData", LAP.size, trailer=2, cars=n)
    if stride is None:
        return None
    cars = []
    for i in range(n):
        f = LAP.unpack_from(buf, HEADER.size + i * stride)
        cars.append(LapData(
            last_lap_ms=f[0], current_lap_ms=f[1],
            gap_to_car_ahead_ms=f[7] * 60_000 + f[6],
            gap_to_leader_ms=f[9] * 60_000 + f[8],
            lap_distance=f[10], position=f[13], lap_num=f[14], pit_status=f[15],
            num_pit_stops=f[16], sector=f[17], lap_invalid=bool(f[18]),
            penalties_s=f[19], result_status=f[26], total_distance=f[11],
            total_warnings=f[20], corner_cutting_warnings=f[21], grid_position=f[24],
            driver_status=f[25], pit_lane_time_ms=f[28], speed_trap_kph=f[31]))
    return cars


def parse_car_telemetry(buf: bytes, h: Header):
    n, layout = num_cars(h), (TEL if h.packet_format <= 2025 else TEL_2026)
    stride = _stride(buf, "CarTelemetry", layout.size, trailer=3, cars=n)
    if stride is None:
        return None
    cars = []
    for i in range(n):
        f = layout.unpack_from(buf, HEADER.size + i * stride)
        cars.append(CarTelemetry(
            speed_kph=f[0], throttle=f[1], brake=f[3], gear=f[5], rpm=f[6], drs=bool(f[7]),
            brake_temps=f[10:14], tyre_surface_temps=f[14:18], tyre_inner_temps=f[18:22],
            tyre_pressures=tuple(round(p, 2) for p in f[23:27])))
    return cars


def parse_car_status(buf: bytes, h: Header):
    n, layout = num_cars(h), (STATUS if h.packet_format <= 2025 else STATUS_2026)
    stride = _stride(buf, "CarStatus", layout.size, cars=n)
    if stride is None:
        return None
    cars = []
    for i in range(n):
        f = layout.unpack_from(buf, HEADER.size + i * stride)
        cars.append(CarStatus(
            fuel_mix=f[2], brake_bias=f[3], fuel_in_tank=f[5], fuel_capacity=f[6],
            fuel_laps_delta=f[7], drs_allowed=bool(f[11]), actual_compound=f[13],
            visual_compound=f[14], tyre_age_laps=f[15], fia_flag=f[16],
            ers_store_j=f[19], ers_mode=f[20]))
    return cars


def parse_car_damage(buf: bytes, h: Header):
    n = num_cars(h)
    stride = _stride(buf, "CarDamage", DAMAGE_BASE_STRIDE, ok=(42, 46), cars=n)
    if stride is None:
        return None
    extra = stride - DAMAGE_BASE_STRIDE   # 4 in F1 25 (tyre blisters, before the wing fields)
    if extra not in (0, 4):
        _warn_once(("CarDamage", "extra"), "Unexpected CarDamage stride %d; part damage may be misread", stride)
    cars = []
    for i in range(n):
        base = HEADER.size + i * stride
        w = DAMAGE_HEAD.unpack_from(buf, base)
        p = DAMAGE_PARTS.unpack_from(buf, base + DAMAGE_HEAD.size + extra)
        cars.append(CarDamage(
            tyre_wear=tuple(round(x, 1) for x in w[0:4]), tyre_damage=w[4:8], brake_damage=w[8:12],
            front_left_wing=p[0], front_right_wing=p[1], rear_wing=p[2], floor=p[3],
            diffuser=p[4], sidepod=p[5], gearbox=p[8], engine=p[9]))
    return cars


def parse_session(buf: bytes, h: Header):
    base = HEADER.size
    if len(buf) < base + 656:
        _warn_once(("Session", "short"), "Session packet too short (%d bytes)", len(buf))
        return None
    s = SESSION_HEAD.unpack_from(buf, base)
    off = base + SESSION_HEAD.size + MARSHAL_ZONES * MARSHAL_SIZE   # -> safety car status
    n_fc = min(buf[off + 2], FORECAST_SLOTS)
    forecast = []
    for j in range(n_fc):
        f = FORECAST.unpack_from(buf, off + 3 + j * FORECAST.size)
        if f[0] == s[5]:   # only samples for the current session
            forecast.append(ForecastSample(f[0], f[1], f[2], f[3], f[5], f[7]))
    pit = off + 3 + FORECAST_SLOTS * FORECAST.size + 2 + 12   # skip accuracy, AI level, 3 link ids
    return Session(
        weather=s[0], track_temp=s[1], air_temp=s[2], total_laps=s[3], track_length_m=s[4],
        session_type=s[5], track_id=s[6], time_left_s=s[8], pit_speed_limit=s[10],
        paused=bool(s[11]), safety_car=buf[off], forecast=sorted(forecast, key=lambda x: x.minutes_ahead),
        pit_ideal_lap=buf[pit], pit_latest_lap=buf[pit + 1], pit_rejoin_pos=buf[pit + 2])


def parse_participants(buf: bytes, h: Header):
    n = num_cars(h)
    stride = _stride(buf, "Participants", 40, lead=1, ok=() if h.packet_format <= 2025 else (60,), cars=n)
    if stride is None:
        return None
    out = []
    for i in range(n):
        o = HEADER.size + 1 + i * stride
        if h.packet_format >= 2026:   # uint8 ai, uint16 driverId, uint16 networkId, uint16 teamId, ...
            team, number, name_off = struct.unpack_from("<H", buf, o + 5)[0], buf[o + 8], 10
        else:
            team, number, name_off = buf[o + 3], buf[o + 5], 7
        raw = buf[o + name_off:o + name_off + 32].split(b"\0", 1)[0]
        out.append(Participant(name=raw.decode("utf-8", "replace"), team_id=team,
                               race_number=number, ai=bool(buf[o])))
    return out


def parse_motion(buf: bytes, h: Header):
    if h.packet_format <= 2025:
        _warn_once(("Motion", "2025"), "Motion parsing is only implemented for format 2026")
        return None
    n = num_cars(h)
    stride = _stride(buf, "Motion", MOTION_2026.size, cars=n)
    if stride is None:
        return None
    cars = []
    for i in range(n):
        f = MOTION_2026.unpack_from(buf, HEADER.size + i * stride)
        cars.append(CarMotion(
            x=f[0], y=f[1], z=f[2], vx=f[3], vy=f[4], vz=f[5],
            forward=tuple(v / 32767.0 for v in f[6:9]), right=tuple(v / 32767.0 for v in f[9:12]),
            g_lat=f[12] / 1000.0, g_lon=f[13] / 1000.0, g_vert=f[14] / 1000.0, yaw=f[15]))
    return cars


def parse_car_telemetry2(buf: bytes, h: Header):
    n = num_cars(h)
    stride = _stride(buf, "CarTelemetry2", TEL2_2026.size, cars=n)
    if stride is None:
        return None
    cars = []
    for i in range(n):
        f = TEL2_2026.unpack_from(buf, HEADER.size + i * stride)
        cars.append(CarTelemetry2(f[0], bool(f[1]), f[2], bool(f[3]), bool(f[4]), f[5], bool(f[6]), bool(f[7])))
    return cars


def parse_event(buf: bytes, h: Header):
    p = HEADER.size + 4
    ev = Event(code=buf[HEADER.size:p].decode("ascii", "replace"))
    if ev.code == "BUTN":
        (ev.buttons,) = struct.unpack_from("<I", buf, p)
    elif ev.code == "FTLP":
        ev.vehicle_idx, ev.lap_time = struct.unpack_from("<Bf", buf, p)
    elif ev.code == "SCAR":
        ev.sc_type, ev.sc_event = struct.unpack_from("<BB", buf, p)   # event: 0 deployed, 1 returning, 2 returned, 3 resume
    elif ev.code == "PENA":
        (ev.penalty_type, ev.infringement_type, ev.vehicle_idx, ev.other_idx,
         ev.time, ev.lap_num) = struct.unpack_from("<BBBBBB", buf, p)
    elif ev.code == "OVTK":
        ev.vehicle_idx, ev.other_idx = struct.unpack_from("<BB", buf, p)   # overtaking, overtaken
    elif ev.code == "COLL":
        ev.vehicle_idx, ev.other_idx = struct.unpack_from("<BB", buf, p)
        if h.packet_format >= 2026:
            ev.severity = buf[p + 2]
    elif ev.code in ("RTMT", "TMPT", "RCWN", "DTSV", "SGSV"):
        ev.vehicle_idx = buf[p]
    return ev


_PARSERS = {
    PacketId.MOTION: parse_motion,
    PacketId.CAR_TELEMETRY2: parse_car_telemetry2,
    PacketId.SESSION: parse_session,
    PacketId.LAP_DATA: parse_lap_data,
    PacketId.EVENT: parse_event,
    PacketId.PARTICIPANTS: parse_participants,
    PacketId.CAR_TELEMETRY: parse_car_telemetry,
    PacketId.CAR_STATUS: parse_car_status,
    PacketId.CAR_DAMAGE: parse_car_damage,
}


def parse(buf: bytes):
    """Return (header, parsed) for a datagram; parsed is None for packets we skip."""
    h = parse_header(buf)
    if h is None:
        return None, None
    if h.packet_format not in KNOWN_FORMATS:
        _warn_once(("fmt", h.packet_format), "Packet format %s not verified; parsing with the %s layouts",
                   h.packet_format, LATEST_FORMAT)
    parser = _PARSERS.get(h.packet_id)
    if parser is None:
        return h, None
    try:
        return h, parser(buf, h)
    except (struct.error, IndexError) as e:
        _warn_once(("err", h.packet_id), "Failed to parse packet id %d: %s", h.packet_id, e)
        return h, None
