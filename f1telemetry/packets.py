"""Parsers for the F1 25 UDP telemetry format (m_packetFormat 2025).

Only the packets and fields a race engineer needs are decoded. Layouts follow EA's
"Data Output from F1 25" spec; check them against the spec for your game build.

Every per-car parser derives the per-car stride from the packet length, so if a patch
(or the 2026 season update) appends fields to a car struct, the leading fields still
parse and a mismatch is logged once instead of silently producing garbage.
"""
from __future__ import annotations

import logging
import struct
from dataclasses import dataclass

log = logging.getLogger(__name__)

NUM_CARS = 22
KNOWN_FORMATS = {2025}

HEADER = struct.Struct("<HBBBBBQfIIBB")                       # 29 bytes
MOTION = struct.Struct("<ffffffhhhhhhffffff")                 # 60 bytes per car
LAP = struct.Struct("<IIHBHBHBHBfff" + "B" * 15 + "HHBfB")    # 57 bytes per car
TEL = struct.Struct("<HfffBbHBBH4H4B4BH4f4B")                 # 60 bytes per car
STATUS = struct.Struct("<BBBBBfffHHBBHBBBbfffBfffB")          # 55 bytes per car
DAMAGE_HEAD = struct.Struct("<4f4B4B")                        # wear, tyre dmg, brake dmg
DAMAGE_PARTS = struct.Struct("<10B")                          # wings .. engine
DAMAGE_BASE_STRIDE = 42                                       # F1 24 size; F1 25 adds 4 blister bytes
SESSION_HEAD = struct.Struct("<BbbBHBbBHHBBBBBB")             # 19 bytes
FORECAST = struct.Struct("<BBBbbbbB")                         # 8 bytes
MARSHAL_ZONES, MARSHAL_SIZE, FORECAST_SLOTS = 21, 5, 64
CAR_SETUP = struct.Struct("<BBBBffffBBBBBBBBBffffBf")         # 50 bytes per car


class PacketId:
    MOTION = 0
    SESSION = 1
    LAP_DATA = 2
    EVENT = 3
    PARTICIPANTS = 4
    CAR_SETUPS = 5
    CAR_TELEMETRY = 6
    CAR_STATUS = 7
    CAR_DAMAGE = 10


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
class CarMotion:
    x: float
    z: float
    yaw: float
    g_lat: float
    g_lon: float
    g_vert: float


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
    sector1_ms: int = 0
    sector2_ms: int = 0


@dataclass(slots=True)
class CarTelemetry:          # 4-wheel tuples are in F1 order: RL, RR, FL, FR
    speed_kph: int
    throttle: float
    steer: float
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
    formula: int = 0


@dataclass(slots=True)
class Participant:
    name: str
    team_id: int
    race_number: int
    ai: bool


@dataclass(slots=True)
class Event:
    code: str
    buttons: int | None = None
    vehicle_idx: int | None = None
    lap_time: float | None = None
    sc_type: int | None = None
    sc_event: int | None = None


@dataclass(slots=True)
class CarSetup:
    front_wing: int
    rear_wing: int
    on_throttle: int
    off_throttle: int
    front_camber: float
    rear_camber: float
    front_toe: float
    rear_toe: float
    front_suspension: int
    rear_suspension: int
    front_anti_roll_bar: int
    rear_anti_roll_bar: int
    front_suspension_height: int
    rear_suspension_height: int
    brake_pressure: int
    brake_bias: int
    engine_braking: int
    rear_left_tyre_pressure: float
    rear_right_tyre_pressure: float
    front_left_tyre_pressure: float
    front_right_tyre_pressure: float
    ballast: int
    fuel_load: float
    next_front_wing_value: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "front_wing": int(self.front_wing),
            "rear_wing": int(self.rear_wing),
            "on_throttle": int(self.on_throttle),
            "off_throttle": int(self.off_throttle),
            "front_camber": round(float(self.front_camber), 3),
            "rear_camber": round(float(self.rear_camber), 3),
            "front_toe": round(float(self.front_toe), 3),
            "rear_toe": round(float(self.rear_toe), 3),
            "front_suspension": int(self.front_suspension),
            "rear_suspension": int(self.rear_suspension),
            "front_anti_roll_bar": int(self.front_anti_roll_bar),
            "rear_anti_roll_bar": int(self.rear_anti_roll_bar),
            "front_suspension_height": int(self.front_suspension_height),
            "rear_suspension_height": int(self.rear_suspension_height),
            "brake_pressure": int(self.brake_pressure),
            "brake_bias": int(self.brake_bias),
            "engine_braking": int(self.engine_braking),
            "rear_left_tyre_pressure": round(float(self.rear_left_tyre_pressure), 2),
            "rear_right_tyre_pressure": round(float(self.rear_right_tyre_pressure), 2),
            "front_left_tyre_pressure": round(float(self.front_left_tyre_pressure), 2),
            "front_right_tyre_pressure": round(float(self.front_right_tyre_pressure), 2),
            "ballast": int(self.ballast),
            "fuel_load": round(float(self.fuel_load), 2),
            "next_front_wing_value": round(float(self.next_front_wing_value), 2),
        }


_warned: set = set()


def _warn_once(key, msg, *args):
    if key not in _warned:
        _warned.add(key)
        log.warning(msg, *args)


def _stride(buf: bytes, name: str, expected: int, lead: int = 0, trailer: int = 0, ok=None) -> int | None:
    body = len(buf) - HEADER.size - lead - trailer
    stride = body // NUM_CARS
    if stride < expected:
        _warn_once((name, "short"), "%s packet too short (%d bytes); skipping", name, len(buf))
        return None
    if ok is None:
        ok = (expected,)
    if ok and (stride not in ok or body % NUM_CARS):
        _warn_once((name, "size"), "%s packet is %d bytes, expected car stride %d; parsing leading fields only",
                   name, len(buf), expected)
    return stride


def parse_header(buf: bytes) -> Header | None:
    if len(buf) < HEADER.size:
        return None
    fmt, year, _maj, _min, ver, pid, uid, t, frame, _overall, player, _second = HEADER.unpack_from(buf)
    return Header(fmt, year, ver, pid, uid, t, frame, player)


def parse_motion(buf: bytes, h: Header):
    trailer = 120 if len(buf) >= HEADER.size + NUM_CARS * MOTION.size + 120 else 0
    stride = _stride(buf, "Motion", MOTION.size, trailer=trailer)
    if stride is None:
        return None
    cars = []
    for i in range(NUM_CARS):
        f = MOTION.unpack_from(buf, HEADER.size + i * stride)
        # f[0]: x, f[2]: z, f[12]: g_lat, f[13]: g_lon, f[14]: g_vert, f[15]: yaw
        cars.append(CarMotion(
            x=f[0], z=f[2], yaw=f[15],
            g_lat=f[12], g_lon=f[13], g_vert=f[14]))
    return cars


def parse_lap_data(buf: bytes, h: Header):
    stride = _stride(buf, "LapData", LAP.size, trailer=2)
    if stride is None:
        return None
    cars = []
    for i in range(NUM_CARS):
        f = LAP.unpack_from(buf, HEADER.size + i * stride)
        cars.append(LapData(
            last_lap_ms=f[0], current_lap_ms=f[1],
            gap_to_car_ahead_ms=f[7] * 60_000 + f[6],
            gap_to_leader_ms=f[9] * 60_000 + f[8],
            lap_distance=f[10], position=f[13], lap_num=f[14], pit_status=f[15],
            num_pit_stops=f[16], sector=f[17], lap_invalid=bool(f[18]),
            penalties_s=f[19], result_status=f[26],
            sector1_ms=f[3] * 60_000 + f[2],
            sector2_ms=f[5] * 60_000 + f[4]))
    return cars


def parse_car_telemetry(buf: bytes, h: Header):
    stride = _stride(buf, "CarTelemetry", TEL.size, trailer=3)
    if stride is None:
        return None
    cars = []
    for i in range(NUM_CARS):
        f = TEL.unpack_from(buf, HEADER.size + i * stride)
        cars.append(CarTelemetry(
            speed_kph=f[0], throttle=f[1], steer=round(f[2], 3), brake=f[3], gear=f[5], rpm=f[6], drs=bool(f[7]),
            brake_temps=f[10:14], tyre_surface_temps=f[14:18], tyre_inner_temps=f[18:22],
            tyre_pressures=tuple(round(p, 2) for p in f[23:27])))
    return cars


def parse_car_status(buf: bytes, h: Header):
    stride = _stride(buf, "CarStatus", STATUS.size)
    if stride is None:
        return None
    cars = []
    for i in range(NUM_CARS):
        f = STATUS.unpack_from(buf, HEADER.size + i * stride)
        cars.append(CarStatus(
            fuel_mix=f[2], brake_bias=f[3], fuel_in_tank=f[5], fuel_capacity=f[6],
            fuel_laps_delta=f[7], drs_allowed=bool(f[11]), actual_compound=f[13],
            visual_compound=f[14], tyre_age_laps=f[15], fia_flag=f[16],
            ers_store_j=f[19], ers_mode=f[20]))
    return cars


def parse_car_damage(buf: bytes, h: Header):
    stride = _stride(buf, "CarDamage", DAMAGE_BASE_STRIDE, ok=(42, 46))
    if stride is None:
        return None
    extra = stride - DAMAGE_BASE_STRIDE   # 4 in F1 25 (tyre blisters, before the wing fields)
    if extra not in (0, 4):
        _warn_once(("CarDamage", "extra"), "Unexpected CarDamage stride %d; part damage may be misread", stride)
    cars = []
    for i in range(NUM_CARS):
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
        pit_ideal_lap=buf[pit], pit_latest_lap=buf[pit + 1], pit_rejoin_pos=buf[pit + 2],
        formula=s[7])


def parse_participants(buf: bytes, h: Header):
    stride = _stride(buf, "Participants", 40, lead=1, ok=())   # size varies by version; only leading fields used
    if stride is None:
        return None
    name_len = 32 if h.packet_format >= 2025 else 48
    out = []
    for i in range(NUM_CARS):
        o = HEADER.size + 1 + i * stride
        raw = buf[o + 7:o + 7 + name_len].split(b"\0", 1)[0]
        out.append(Participant(name=raw.decode("utf-8", "replace"), team_id=buf[o + 3],
                               race_number=buf[o + 5], ai=bool(buf[o])))
    return out


def parse_event(buf: bytes, h: Header):
    p = HEADER.size + 4
    ev = Event(code=buf[HEADER.size:p].decode("ascii", "replace"))
    if ev.code == "BUTN":
        (ev.buttons,) = struct.unpack_from("<I", buf, p)
    elif ev.code == "FTLP":
        ev.vehicle_idx, ev.lap_time = struct.unpack_from("<Bf", buf, p)
    elif ev.code == "SCAR":
        ev.sc_type, ev.sc_event = struct.unpack_from("<BB", buf, p)   # event: 0 deployed, 1 returning, 2 returned, 3 resume
    elif ev.code in ("RTMT", "TMPT", "DRSF"):
        ev.vehicle_idx = buf[p]
    return ev


def parse_car_setups(buf: bytes, h: Header):
    stride = _stride(buf, "CarSetups", CAR_SETUP.size, trailer=4)
    if stride is None:
        return None
    next_fw = 0.0
    trailer_offset = HEADER.size + NUM_CARS * stride
    if len(buf) >= trailer_offset + 4:
        (next_fw,) = struct.unpack_from("<f", buf, trailer_offset)
    cars = []
    for i in range(NUM_CARS):
        f = CAR_SETUP.unpack_from(buf, HEADER.size + i * stride)
        cars.append(CarSetup(
            front_wing=f[0],
            rear_wing=f[1],
            on_throttle=f[2],
            off_throttle=f[3],
            front_camber=f[4],
            rear_camber=f[5],
            front_toe=f[6],
            rear_toe=f[7],
            front_suspension=f[8],
            rear_suspension=f[9],
            front_anti_roll_bar=f[10],
            rear_anti_roll_bar=f[11],
            front_suspension_height=f[12],
            rear_suspension_height=f[13],
            brake_pressure=f[14],
            brake_bias=f[15],
            engine_braking=f[16],
            rear_left_tyre_pressure=f[17],
            rear_right_tyre_pressure=f[18],
            front_left_tyre_pressure=f[19],
            front_right_tyre_pressure=f[20],
            ballast=f[21],
            fuel_load=f[22],
            next_front_wing_value=next_fw if i == h.player_idx else 0.0,
        ))
    return cars


_PARSERS = {
    PacketId.MOTION: parse_motion,
    PacketId.SESSION: parse_session,
    PacketId.LAP_DATA: parse_lap_data,
    PacketId.EVENT: parse_event,
    PacketId.PARTICIPANTS: parse_participants,
    PacketId.CAR_SETUPS: parse_car_setups,
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
        _warn_once(("fmt", h.packet_format), "Packet format %s not verified; parsing with F1 25 layouts",
                   h.packet_format)
    parser = _PARSERS.get(h.packet_id)
    if parser is None:
        return h, None
    try:
        return h, parser(buf, h)
    except (struct.error, IndexError) as e:
        _warn_once(("err", h.packet_id), "Failed to parse packet id %d: %s", h.packet_id, e)
        return h, None
