"""Parsers for the F1 25 UDP telemetry formats: 2025 and the 2026 Season Pack (m_packetFormat 2026).

Only the packets and fields a race engineer needs are decoded. The 2026 layouts are taken from
EA's official "F1 25: 2026 Season Pack" spec (docs/udp_spec_2026.h): 24 cars per array, a 1-byte
engine temperature, an extra ERS float in CarStatus, 2-byte driver/team ids in Participants,
int16 g-forces in Motion, collision severity, and the new CarTelemetry2 packet (active aero,
overtake mode). The 2025 layouts follow EA's "Data Output from F1 25" spec.

Every per-car parser derives the per-car stride from the packet length, so if a patch
appends fields to a car struct, the leading fields still parse and a mismatch is logged
once instead of silently producing garbage.
"""
from __future__ import annotations

import logging
import struct
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

NUM_CARS = 22  # 2025 format default; use num_cars(header) for the packet being parsed
KNOWN_FORMATS = {2025, 2026}
LATEST_FORMAT = 2026

HEADER = struct.Struct("<HBBBBBQfIIBB")                       # 29 bytes
HEADER_SIZES = {2025: 29, 2026: 29}
MOTION = struct.Struct("<ffffffhhhhhhffffff")                 # 2025: 60 bytes per car
MOTION_2026 = struct.Struct("<ffffff6h3hfff")                 # 2026: 54 bytes per car (int16 g-force / 1000)
LAP = struct.Struct("<IIHBHBHBHBfff" + "B" * 15 + "HHBfB")    # 57 bytes per car
TEL = struct.Struct("<HfffBbHBBH4H4B4BH4f4B")                 # 2025: 60 bytes per car (uint16 engine temp)
TEL_2026 = struct.Struct("<HfffBbHBBH4H4B4BB4f4B")            # 2026: 59 bytes per car (uint8 engine temp)
STATUS = struct.Struct("<BBBBBfffHHBBHBBBbfffBfffB")          # 2025: 55 bytes per car
STATUS_2026 = struct.Struct("<BBBBBfffHHBBHBBBbfffBffffB")    # 2026: 59 bytes per car (+ ERS harvest limit)
TEL2_2026 = struct.Struct("<BBHBBHBB")                        # 2026: 10 bytes per car (CarTelemetry2)
DAMAGE_HEAD = struct.Struct("<4f4B4B")                        # wear, tyre dmg, brake dmg
DAMAGE_PARTS = struct.Struct("<10B")                          # wings .. engine
DAMAGE_BASE_STRIDE = 42                                       # F1 24 size; F1 25/26 adds 4 blister bytes -> 46
SESSION_HEAD = struct.Struct("<BbbBHBbBHHBBBBBB")             # 19 bytes
FORECAST = struct.Struct("<BBBbbbbB")                         # 8 bytes
MARSHAL_ZONES, MARSHAL_SIZE, FORECAST_SLOTS = 21, 5, 64
CAR_SETUP = struct.Struct("<BBBBffffBBBBBBBBBffffBf")         # 50 bytes per car
FINAL_CLASS_DATA_2025 = struct.Struct("<BBBBBBIdBBB8B8B8B")   # 2025: 45 bytes per car
FINAL_CLASS_DATA_2026 = struct.Struct("<BBBBBBBIdBBB8B8B8B")  # 2026: 46 bytes per car (+ resultReason)
FINAL_CLASS_DATA = FINAL_CLASS_DATA_2025
TIME_TRIAL_DATASET = struct.Struct("<BHIIIIBBBBBB")           # 25 bytes
TIME_TRIAL_DATASET_24 = struct.Struct("<BBIIIIBBBBBB")        # 24 bytes
TYRE_SET_DATA = struct.Struct("<BBBBBBBhB")                   # 10 bytes per tyre set


class PacketId:
    MOTION = 0
    SESSION = 1
    LAP_DATA = 2
    EVENT = 3
    PARTICIPANTS = 4
    CAR_SETUPS = 5
    CAR_TELEMETRY = 6
    CAR_STATUS = 7
    FINAL_CLASSIFICATION = 8
    LOBBY_INFO = 9
    CAR_DAMAGE = 10
    SESSION_HISTORY = 11
    TYRE_SETS = 12
    MOTION_EX = 13
    TIME_TRIAL = 14
    LAP_POSITIONS = 15
    CAR_TELEMETRY2 = 16


PACKET_NAMES: dict[int, str] = {
    0: "Motion",
    1: "Session",
    2: "LapData",
    3: "Event",
    4: "Participants",
    5: "CarSetups",
    6: "CarTelemetry",
    7: "CarStatus",
    8: "FinalClassification",
    9: "LobbyInfo",
    10: "CarDamage",
    11: "SessionHistory",
    12: "TyreSets",
    13: "MotionEx",
    14: "TimeTrial",
    15: "LapPositions",
    16: "CarTelemetry2",
}

# Expected packet sizes from EA's 2026 Season Pack spec (docs/udp_spec_2026.h)
EXPECTED_2026: dict[int, int] = {
    0: 1325,
    1: 926,
    2: 1399,
    3: 45,
    4: 1470,
    5: 1233,
    6: 1448,
    7: 1445,
    8: 1134,
    9: 1062,
    10: 1133,
    11: 1460,
    12: 231,
    13: 273,
    14: 104,
    15: 1231,
    16: 269,
}


def num_cars(h: Header | int) -> int:
    """Number of cars in telemetry arrays: 22 for <=2025, 24 for 2026+."""
    fmt = h.packet_format if isinstance(h, Header) else h
    return 22 if fmt <= 2025 else 24


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
    game_major_version: int = 0
    game_minor_version: int = 0
    overall_frame: int = 0
    secondary_player_idx: int = 255


@dataclass(slots=True)
class CarMotion:
    x: float
    z: float
    yaw: float
    g_lat: float
    g_lon: float
    g_vert: float
    y: float = 0.0
    fwd_x: float = 0.0
    fwd_y: float = 0.0
    fwd_z: float = 0.0
    right_x: float = 0.0
    right_y: float = 0.0
    right_z: float = 0.0
    vx: float = 0.0
    vy: float = 0.0
    vz: float = 0.0
    pitch: float = 0.0
    roll: float = 0.0
    forward: tuple = (0.0, 0.0, 0.0)
    right: tuple = (0.0, 0.0, 0.0)


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
    total_warnings: int = 0
    corner_cutting_warnings: int = 0
    grid_position: int = 0
    speed_trap_kph: float = 0.0
    total_distance: float = 0.0
    driver_status: int = 0
    pit_lane_time_ms: int = 0


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
    surface_type: tuple = (0, 0, 0, 0)


@dataclass(slots=True)
class CarTelemetry2:         # 2026 regulations
    active_aero_mode: int            # 0 = corner mode, 1 = straight mode
    active_aero_available: bool
    active_aero_in_m: int            # 0 = not available, else available in X metres
    overtake_available: bool
    overtake_active: bool
    overtake_in_m: int
    regs_2026: bool
    wrong_way: bool


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
    ers_harvest_limit_per_lap: float = 0.0


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
    ai_difficulty: int = 0
    sector2_distance_start: float = 0.0
    sector3_distance_start: float = 0.0
    start_reaction_time: float = 0.0
    corner_cutting_stringency: int = 0
    braking_assist: int = 0
    gearbox_assist: int = 0
    traction_control_assist: int = 0
    anti_lock_brakes_assist: int = 0
    dynamic_racing_line: int = 0
    active_aero_track_status: int = 0
    marshal_zones: list[tuple[float, int]] = field(default_factory=list)
    drs_zones: list[tuple[float, float]] = field(default_factory=list)


@dataclass(slots=True)
class Participant:
    name: str
    team_id: int
    race_number: int
    ai: bool
    your_telemetry: int = 1
    num_colours: int = 0
    livery_colours: list[tuple[int, int, int]] = field(default_factory=list)

    @property
    def primary_color_hex(self) -> str | None:
        if self.livery_colours and len(self.livery_colours) > 0:
            r, g, b = self.livery_colours[0]
            return f"#{r:02x}{g:02x}{b:02x}"
        return None


@dataclass(slots=True)
class Event:
    code: str
    buttons: int | None = None
    vehicle_idx: int | None = None
    lap_time: float | None = None
    sc_type: int | None = None
    sc_event: int | None = None
    penalty_type: int | None = None
    infringement_type: int | None = None
    penalty_time: int | None = None
    other_vehicle_idx: int | None = None
    places_gained: int | None = None
    overtaking_vehicle_idx: int | None = None
    being_overtaken_vehicle_idx: int | None = None
    vehicle_1_idx: int | None = None
    vehicle_2_idx: int | None = None
    severity: int | None = None      # COLL: 0 low, 1 medium, 2 high (2026)
    flashback_frame: int | None = None
    flashback_time: float | None = None
    lap_num: int | None = None
    raw_bytes: bytes | None = None


@dataclass(slots=True)
class FinalClassification:
    position: int
    num_laps: int
    grid_position: int
    points: int
    num_pit_stops: int
    result_status: int
    best_lap_time_ms: int
    total_race_time: float
    penalties_time: int
    num_penalties: int
    result_reason: int = 0


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


@dataclass(slots=True)
class TimeTrialDataSet:
    car_idx: int
    team_id: int
    lap_time_ms: int
    sector1_ms: int
    sector2_ms: int
    sector3_ms: int
    traction_control: int
    gearbox_assist: int
    anti_lock_brakes: int
    equal_car_performance: bool
    custom_setup: bool
    valid: bool


@dataclass(slots=True)
class TimeTrialData:
    player_session_best: TimeTrialDataSet
    personal_best: TimeTrialDataSet
    rival: TimeTrialDataSet


@dataclass(slots=True)
class TyreSet:
    actual_compound: int
    visual_compound: int
    wear: int
    available: bool
    recommended_session: int
    life_span: int
    usable_life: int
    lap_delta_ms: int
    fitted: bool


@dataclass(slots=True)
class TyreSetsData:
    car_idx: int
    fitted_idx: int
    sets: list[TyreSet]


@dataclass(slots=True)
class LapPositionsData:
    num_laps: int
    lap_start: int
    positions: list[list[int]]


_warned: set = set()
_startup_logged = False
_checked_packet_sizes: set[int] = set()


def _warn_once(key, msg, *args):
    if key not in _warned:
        _warned.add(key)
        log.warning(msg, *args)


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
    fmt, year, maj, min_v, ver, pid, uid, t, frame, overall, player, sec = HEADER.unpack_from(buf)
    return Header(
        packet_format=fmt,
        game_year=year,
        packet_version=ver,
        packet_id=pid,
        session_uid=uid,
        session_time=t,
        frame=frame,
        player_idx=player,
        game_major_version=maj,
        game_minor_version=min_v,
        overall_frame=overall,
        secondary_player_idx=sec,
    )


def parse_motion(buf: bytes, h: Header):
    n = num_cars(h)
    if h.packet_format >= 2026:
        stride = _stride(buf, "Motion", MOTION_2026.size, cars=n)
        if stride is None:
            return None
        cars = []
        for i in range(n):
            f = MOTION_2026.unpack_from(buf, HEADER.size + i * stride)
            fwd = tuple(v / 32767.0 for v in f[6:9])
            rt = tuple(v / 32767.0 for v in f[9:12])
            cars.append(CarMotion(
                x=f[0], y=f[1], z=f[2],
                vx=f[3], vy=f[4], vz=f[5],
                fwd_x=fwd[0], fwd_y=fwd[1], fwd_z=fwd[2],
                right_x=rt[0], right_y=rt[1], right_z=rt[2],
                g_lat=f[12] / 1000.0,
                g_lon=f[13] / 1000.0,
                g_vert=f[14] / 1000.0,
                yaw=f[15], pitch=f[16], roll=f[17],
                forward=fwd, right=rt,
            ))
        return cars
    else:
        trailer = 120 if len(buf) >= HEADER.size + n * MOTION.size + 120 else 0
        stride = _stride(buf, "Motion", MOTION.size, trailer=trailer, cars=n)
        if stride is None:
            return None
        cars = []
        for i in range(n):
            f = MOTION.unpack_from(buf, HEADER.size + i * stride)
            fwd = tuple(v / 32767.0 for v in f[6:9])
            rt = tuple(v / 32767.0 for v in f[9:12])
            cars.append(CarMotion(
                x=f[0], y=f[1], z=f[2],
                vx=f[3], vy=f[4], vz=f[5],
                fwd_x=fwd[0], fwd_y=fwd[1], fwd_z=fwd[2],
                right_x=rt[0], right_y=rt[1], right_z=rt[2],
                g_lat=f[12], g_lon=f[13], g_vert=f[14],
                yaw=f[15], pitch=f[16], roll=f[17],
                forward=fwd, right=rt,
            ))
        return cars


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
            penalties_s=f[19], result_status=f[26],
            sector1_ms=f[3] * 60_000 + f[2],
            sector2_ms=f[5] * 60_000 + f[4],
            total_warnings=f[20],
            corner_cutting_warnings=f[21],
            grid_position=f[24],
            speed_trap_kph=float(f[28]) if len(f) > 28 and f[28] is not None else 0.0,
            total_distance=float(f[11]),
            driver_status=f[25],
            pit_lane_time_ms=f[27] if len(f) > 27 and f[27] is not None else 0))
    return cars


def parse_car_telemetry(buf: bytes, h: Header):
    n = num_cars(h)
    layout = TEL if h.packet_format <= 2025 else TEL_2026
    stride = _stride(buf, "CarTelemetry", layout.size, trailer=3, cars=n)
    if stride is None:
        return None
    cars = []
    for i in range(n):
        f = layout.unpack_from(buf, HEADER.size + i * stride)
        cars.append(CarTelemetry(
            speed_kph=f[0], throttle=f[1], steer=round(f[2], 3), brake=f[3], gear=f[5], rpm=f[6], drs=bool(f[7]),
            brake_temps=f[10:14], tyre_surface_temps=f[14:18], tyre_inner_temps=f[18:22],
            tyre_pressures=tuple(round(p, 2) for p in f[23:27]),
            surface_type=tuple(f[27:31]) if len(f) > 27 else (0, 0, 0, 0)))
    return cars


def parse_car_status(buf: bytes, h: Header):
    n = num_cars(h)
    layout = STATUS if h.packet_format <= 2025 else STATUS_2026
    stride = _stride(buf, "CarStatus", layout.size, cars=n)
    if stride is None:
        return None
    cars = []
    for i in range(n):
        f = layout.unpack_from(buf, HEADER.size + i * stride)
        harvest_limit = float(f[23]) if (h.packet_format >= 2026 and len(f) > 23) else 0.0
        cars.append(CarStatus(
            fuel_mix=f[2], brake_bias=f[3], fuel_in_tank=f[5], fuel_capacity=f[6],
            fuel_laps_delta=f[7], drs_allowed=bool(f[11]), actual_compound=f[13],
            visual_compound=f[14], tyre_age_laps=f[15], fia_flag=f[16],
            ers_store_j=f[19], ers_mode=f[20],
            ers_harvest_limit_per_lap=harvest_limit))
    return cars


def parse_car_damage(buf: bytes, h: Header):
    n = num_cars(h)
    stride = _stride(buf, "CarDamage", DAMAGE_BASE_STRIDE, ok=(42, 46), cars=n)
    if stride is None:
        return None
    extra = stride - DAMAGE_BASE_STRIDE   # 4 in F1 25/26 (tyre blisters, before the wing fields)
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
    ai_off = off + 3 + FORECAST_SLOTS * FORECAST.size + 1
    ai_diff = buf[ai_off] if len(buf) > ai_off else 0

    s2_start = 0.0
    s3_start = 0.0
    start_reaction = 0.0
    cc_stringency = 0
    braking_assist = 0
    gearbox_assist = 0
    tc_assist = 0
    abs_assist = 0
    dyn_line = 0
    active_aero_track_status = 0

    if len(buf) >= 926 or (h.packet_format >= 2026 and len(buf) >= base + 721):
        braking_assist = buf[pit + 4] if len(buf) > pit + 4 else 0
        gearbox_assist = buf[pit + 5] if len(buf) > pit + 5 else 0
        dyn_line = buf[pit + 10] if len(buf) > pit + 10 else 0
        if len(buf) > base + 693:
            cc_stringency = buf[base + 693]
        if len(buf) >= base + 721:
            s2_start, s3_start = struct.unpack_from("<ff", buf, base + 713)
            active_aero_track_status = buf[base + 721]
        if len(buf) >= base + 891:
            start_reaction = struct.unpack_from("<f", buf, base + 885)[0]
            abs_assist = buf[base + 889]
            tc_assist = buf[base + 890]

    num_mz = min(s[15] if len(s) > 15 else 0, MARSHAL_ZONES)
    marshal_zones = []
    for mz_i in range(num_mz):
        mz_start, mz_flag = struct.unpack_from("<fb", buf, base + SESSION_HEAD.size + mz_i * MARSHAL_SIZE)
        marshal_zones.append((float(round(mz_start, 4)), int(mz_flag)))

    drs_zones = []
    if len(buf) >= base + 885:
        num_drs = min(buf[base + 852], 4) if len(buf) > base + 852 else 0
        for drs_i in range(num_drs):
            d_start, d_end = struct.unpack_from("<ff", buf, base + 853 + drs_i * 8)
            if d_end > d_start:
                drs_zones.append((float(round(d_start, 4)), float(round(d_end, 4))))

    return Session(
        weather=s[0], track_temp=s[1], air_temp=s[2], total_laps=s[3], track_length_m=s[4],
        session_type=s[5], track_id=s[6], time_left_s=s[8], pit_speed_limit=s[10],
        paused=bool(s[11]), safety_car=buf[off], forecast=sorted(forecast, key=lambda x: x.minutes_ahead),
        pit_ideal_lap=buf[pit], pit_latest_lap=buf[pit + 1], pit_rejoin_pos=buf[pit + 2],
        formula=s[7], ai_difficulty=ai_diff,
        sector2_distance_start=s2_start,
        sector3_distance_start=s3_start,
        start_reaction_time=start_reaction,
        corner_cutting_stringency=cc_stringency,
        braking_assist=braking_assist,
        gearbox_assist=gearbox_assist,
        traction_control_assist=tc_assist,
        anti_lock_brakes_assist=abs_assist,
        dynamic_racing_line=dyn_line,
        active_aero_track_status=active_aero_track_status,
        marshal_zones=marshal_zones,
        drs_zones=drs_zones,
    )


def parse_participants(buf: bytes, h: Header):
    n = num_cars(h)
    stride = _stride(buf, "Participants", 40, lead=1, ok=() if h.packet_format <= 2025 else (60,), cars=n)
    if stride is None:
        return None
    out = []
    for i in range(n):
        o = HEADER.size + 1 + i * stride
        your_tel = 1
        num_col = 0
        liv_col = []
        if h.packet_format >= 2026:
            # 2026: uint8 ai, uint16 driverId, uint16 networkId, uint16 teamId, uint8 myTeam, uint8 raceNumber, uint8 nationality, char name[32], uint8 yourTelemetry, uint8 showOnlineNames, uint16 techLevel, uint8 platform, uint8 numColours, LiveryColour liveryColours[4]
            team = struct.unpack_from("<H", buf, o + 5)[0]
            number = buf[o + 8]
            name_off = 10
            if len(buf) >= o + 60:
                your_tel = buf[o + 42]
                num_col = buf[o + 47]
                for c_idx in range(min(4, num_col)):
                    cb = o + 48 + c_idx * 3
                    liv_col.append((buf[cb], buf[cb + 1], buf[cb + 2]))
        else:
            team = buf[o + 3]
            number = buf[o + 5]
            name_off = 7
        raw = buf[o + name_off : o + name_off + 32].split(b"\0", 1)[0]
        out.append(Participant(
            name=raw.decode("utf-8", "replace"),
            team_id=team,
            race_number=number,
            ai=bool(buf[o]),
            your_telemetry=your_tel,
            num_colours=num_col,
            livery_colours=liv_col,
        ))
    return out


def parse_car_telemetry2(buf: bytes, h: Header):
    n = num_cars(h)
    stride = _stride(buf, "CarTelemetry2", TEL2_2026.size, cars=n)
    if stride is None:
        return None
    cars = []
    for i in range(n):
        f = TEL2_2026.unpack_from(buf, HEADER.size + i * stride)
        cars.append(CarTelemetry2(
            active_aero_mode=f[0],
            active_aero_available=bool(f[1]),
            active_aero_in_m=f[2],
            overtake_available=bool(f[3]),
            overtake_active=bool(f[4]),
            overtake_in_m=f[5],
            regs_2026=bool(f[6]),
            wrong_way=bool(f[7]),
        ))
    return cars


def parse_event(buf: bytes, h: Header):
    p = HEADER.size + 4
    ev = Event(code=buf[HEADER.size:p].decode("ascii", "replace"))
    if ev.code == "BUTN":
        (ev.buttons,) = struct.unpack_from("<I", buf, p)
    elif ev.code == "FTLP":
        ev.vehicle_idx, ev.lap_time = struct.unpack_from("<Bf", buf, p)
    elif ev.code == "SCAR":
        ev.sc_type, ev.sc_event = struct.unpack_from("<BB", buf, p)
    elif ev.code == "PENA":
        if len(buf) >= p + 7:
            (
                ev.penalty_type,
                ev.infringement_type,
                ev.vehicle_idx,
                ev.other_vehicle_idx,
                ev.penalty_time,
                ev.lap_num,
                ev.places_gained,
            ) = struct.unpack_from("<BBBBBBB", buf, p)
            ev.raw_bytes = buf[p:p + 7]
        elif len(buf) >= p + 6:
            (
                ev.penalty_type,
                ev.infringement_type,
                ev.vehicle_idx,
                ev.other_vehicle_idx,
                ev.penalty_time,
                ev.lap_num,
            ) = struct.unpack_from("<BBBBBB", buf, p)
            ev.raw_bytes = buf[p:p + 6]
    elif ev.code == "OVTK":
        if len(buf) >= p + 2:
            ev.overtaking_vehicle_idx, ev.being_overtaken_vehicle_idx = struct.unpack_from("<BB", buf, p)
    elif ev.code == "COLL":
        if len(buf) >= p + 2:
            ev.vehicle_1_idx, ev.vehicle_2_idx = struct.unpack_from("<BB", buf, p)
            if len(buf) >= p + 3:
                ev.severity = buf[p + 2]
    elif ev.code == "FLBK":
        if len(buf) >= p + 8:
            ev.flashback_frame, ev.flashback_time = struct.unpack_from("<If", buf, p)
    elif ev.code == "RCWN":
        if len(buf) >= p + 1:
            (ev.vehicle_idx,) = struct.unpack_from("<B", buf, p)
    elif ev.code == "CHQF":
        pass
    elif ev.code in ("RTMT", "TMPT", "DRSF", "DTSV", "SGSV"):
        if len(buf) >= p + 1:
            ev.vehicle_idx = buf[p]
    return ev


def parse_final_classification(buf: bytes, h: Header):
    n = num_cars(h)
    layout = FINAL_CLASS_DATA_2025 if h.packet_format <= 2025 else FINAL_CLASS_DATA_2026
    stride = _stride(buf, "FinalClassification", layout.size, lead=1, ok=(45, 46), cars=n)
    if stride is None:
        return None
    cars = []
    num_cars_in_buf = buf[HEADER.size] if len(buf) > HEADER.size else 0
    for i in range(n):
        if i >= num_cars_in_buf:
            break
        base = HEADER.size + 1 + i * stride
        f = layout.unpack_from(buf, base)
        if h.packet_format >= 2026:
            cars.append(FinalClassification(
                position=f[0],
                num_laps=f[1],
                grid_position=f[2],
                points=f[3],
                num_pit_stops=f[4],
                result_status=f[5],
                result_reason=f[6],
                best_lap_time_ms=f[7],
                total_race_time=f[8],
                penalties_time=f[9],
                num_penalties=f[10],
            ))
        else:
            cars.append(FinalClassification(
                position=f[0],
                num_laps=f[1],
                grid_position=f[2],
                points=f[3],
                num_pit_stops=f[4],
                result_status=f[5],
                best_lap_time_ms=f[6],
                total_race_time=f[7],
                penalties_time=f[8],
                num_penalties=f[9],
            ))
    return cars


def parse_car_setups(buf: bytes, h: Header):
    n = num_cars(h)
    stride = _stride(buf, "CarSetups", CAR_SETUP.size, trailer=4, cars=n)
    if stride is None:
        return None
    next_fw = 0.0
    trailer_offset = HEADER.size + n * stride
    if len(buf) >= trailer_offset + 4:
        (next_fw,) = struct.unpack_from("<f", buf, trailer_offset)
    cars = []
    for i in range(n):
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


def parse_time_trial(buf: bytes, h: Header):
    stride = _stride(buf, "TimeTrial", 24, ok=(24, 25), cars=3)
    if stride is None:
        return None
    use_25 = (stride >= 25)
    dsets = []
    for i in range(3):
        off = HEADER.size + i * stride
        if use_25:
            car_idx, team_id, lap_ms, s1, s2, s3, tc, gb, abs_b, eq, setup, val = TIME_TRIAL_DATASET.unpack_from(buf, off)
        else:
            car_idx, team_id, lap_ms, s1, s2, s3, tc, gb, abs_b, eq, setup, val = TIME_TRIAL_DATASET_24.unpack_from(buf, off)
        dsets.append(TimeTrialDataSet(
            car_idx=car_idx, team_id=team_id, lap_time_ms=lap_ms,
            sector1_ms=s1, sector2_ms=s2, sector3_ms=s3,
            traction_control=tc, gearbox_assist=gb, anti_lock_brakes=abs_b,
            equal_car_performance=bool(eq), custom_setup=bool(setup), valid=bool(val),
        ))
    return TimeTrialData(
        player_session_best=dsets[0],
        personal_best=dsets[1],
        rival=dsets[2],
    )


def parse_tyre_sets(buf: bytes, h: Header):
    if len(buf) < HEADER.size + 1 + 20 * 10 + 1:
        return None
    car_idx = buf[HEADER.size]
    sets = []
    base = HEADER.size + 1
    for i in range(20):
        off = base + i * 10
        actual, visual, wear, avail, rec_sess, life, usable, delta, fitted = TYRE_SET_DATA.unpack_from(buf, off)
        sets.append(TyreSet(
            actual_compound=actual, visual_compound=visual, wear=wear,
            available=bool(avail), recommended_session=rec_sess,
            life_span=life, usable_life=usable, lap_delta_ms=delta, fitted=bool(fitted)
        ))
    fitted_idx = buf[base + 200]
    return TyreSetsData(car_idx=car_idx, fitted_idx=fitted_idx, sets=sets)


def parse_lap_positions(buf: bytes, h: Header):
    n = num_cars(h)
    if len(buf) < HEADER.size + 2 + 50 * n:
        return None
    num_laps, lap_start = struct.unpack_from("<BB", buf, HEADER.size)
    positions = []
    base = HEADER.size + 2
    for l_idx in range(50):
        lap_pos = list(buf[base + l_idx * n : base + (l_idx + 1) * n])
        positions.append(lap_pos)
    return LapPositionsData(num_laps=num_laps, lap_start=lap_start, positions=positions)


@dataclass
class LapHistory:
    lap_time_ms: int
    sector1_ms: int
    sector2_ms: int
    sector3_ms: int
    lap_valid: bool
    sector1_valid: bool
    sector2_valid: bool
    sector3_valid: bool


@dataclass
class SessionHistory:
    car_idx: int
    num_laps: int
    num_tyre_stints: int
    best_lap_time_lap_num: int
    best_sector1_lap_num: int
    best_sector2_lap_num: int
    best_sector3_lap_num: int
    lap_history: list[LapHistory]


def parse_session_history(buf: bytes, h: Header) -> SessionHistory:
    offset = HEADER_SIZES.get(h.packet_format, 29)
    car_idx, num_laps, num_stints, best_lap_num, best_s1_num, best_s2_num, best_s3_num = struct.unpack_from(
        "<7B", buf, offset
    )
    offset += 7
    lap_history: list[LapHistory] = []
    for _ in range(num_laps):
        if offset + 14 > len(buf):
            break
        (
            lap_time_ms,
            s1_ms_part, s1_min_part,
            s2_ms_part, s2_min_part,
            s3_ms_part, s3_min_part,
            valid_flags
        ) = struct.unpack_from("<I H B H B H B B", buf, offset)
        offset += 14

        s1_total_ms = s1_min_part * 60000 + s1_ms_part
        s2_total_ms = s2_min_part * 60000 + s2_ms_part
        s3_total_ms = s3_min_part * 60000 + s3_ms_part

        lap_history.append(LapHistory(
            lap_time_ms=lap_time_ms,
            sector1_ms=s1_total_ms,
            sector2_ms=s2_total_ms,
            sector3_ms=s3_total_ms,
            lap_valid=bool(valid_flags & 0x01),
            sector1_valid=bool(valid_flags & 0x02),
            sector2_valid=bool(valid_flags & 0x04),
            sector3_valid=bool(valid_flags & 0x08),
        ))

    return SessionHistory(
        car_idx=car_idx,
        num_laps=num_laps,
        num_tyre_stints=num_stints,
        best_lap_time_lap_num=best_lap_num,
        best_sector1_lap_num=best_s1_num,
        best_sector2_lap_num=best_s2_num,
        best_sector3_lap_num=best_s3_num,
        lap_history=lap_history,
    )


_PARSERS = {
    PacketId.MOTION: parse_motion,
    PacketId.SESSION: parse_session,
    PacketId.LAP_DATA: parse_lap_data,
    PacketId.EVENT: parse_event,
    PacketId.PARTICIPANTS: parse_participants,
    PacketId.CAR_SETUPS: parse_car_setups,
    PacketId.CAR_TELEMETRY: parse_car_telemetry,
    PacketId.CAR_STATUS: parse_car_status,
    PacketId.FINAL_CLASSIFICATION: parse_final_classification,
    PacketId.CAR_DAMAGE: parse_car_damage,
    PacketId.SESSION_HISTORY: parse_session_history,
    PacketId.TIME_TRIAL: parse_time_trial,
    PacketId.TYRE_SETS: parse_tyre_sets,
    PacketId.LAP_POSITIONS: parse_lap_positions,
    PacketId.CAR_TELEMETRY2: parse_car_telemetry2,
}


PENALTY_TYPES: dict[int, str] = {
    0: "Drive through",
    1: "Stop Go",
    2: "Grid penalty",
    3: "Penalty reminder",
    4: "Time penalty",
    5: "Warning",
    6: "Disqualified",
    7: "Removed from formation lap",
    8: "Parked too long timer",
    9: "Tyre regulations",
    10: "This lap invalidated",
    11: "This and next lap invalidated",
    12: "This lap invalidated without reason",
    13: "This and next lap invalidated without reason",
    14: "This and previous lap invalidated",
    15: "This and previous lap invalidated without reason",
    16: "Retired",
    17: "Black flag timer",
}

INFRINGEMENT_TYPES: dict[int, str] = {
    0: "Blocking by slow driving",
    1: "Blocking by wrong way driving",
    2: "Reversing off the start line",
    3: "Big Collision",
    4: "Small Collision",
    5: "Collision failed to hand back position single",
    6: "Collision failed to hand back position multiple",
    7: "Corner cutting gained time",
    8: "Corner cutting overtake single",
    9: "Corner cutting overtake multiple",
    10: "Crossed pit exit lane",
    11: "Ignoring blue flags",
    12: "Ignoring yellow flags",
    13: "Ignoring drive through",
    14: "Too many drive throughs",
    15: "Drive through reminder servable",
    16: "Safety car car infraction",
    17: "Safety car exceeding allowed pace",
    18: "Virtual safety car exceeding allowed pace",
    19: "Pit lane speed infraction",
    20: "Parked too long in garage",
}


def parse(buf: bytes):
    """Return (header, parsed) for a datagram; parsed is None for packets we skip."""
    global _startup_logged
    h = parse_header(buf)
    if h is None:
        return None, None

    if not _startup_logged:
        _startup_logged = True
        log.info(
            "UDP telemetry stream connected: format=%d, game_year=%d, packet_version=%d",
            h.packet_format, h.game_year, h.packet_version
        )

    # Startup self-check: check first packet of each packet_id against 2026 spec table
    if h.packet_format >= 2026:
        if h.packet_id not in _checked_packet_sizes:
            _checked_packet_sizes.add(h.packet_id)
            expected = EXPECTED_2026.get(h.packet_id)
            if expected is not None and len(buf) != expected:
                pname = PACKET_NAMES.get(h.packet_id, f"PacketId_{h.packet_id}")
                log.warning(
                    "Packet size mismatch for %s (id %d): received %d bytes, expected %d bytes from 2026 spec",
                    pname, h.packet_id, len(buf), expected
                )

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
