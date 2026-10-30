"""Compact binary car positioning protocol and fast broadcaster for live track map.

Format:
Header (10 bytes):
  magic: b"FP" (2 bytes)
  seq: uint16 (2 bytes)
  timestamp_ms: uint32 (4 bytes)
  track_id: uint8 (1 byte)
  num_cars: uint8 (1 byte)

Per Car (15 bytes):
  car_idx: uint8 (1 byte)
  x_tenths: int32 (4 bytes) - 0.1 m resolution
  z_tenths: int32 (4 bytes) - 0.1 m resolution
  heading_u16: uint16 (2 bytes) - [0, 2*pi) mapped to 0..65535
  speed_kph: uint16 (2 bytes)
  position: uint8 (1 byte)
  status_flags: uint8 (1 byte)

Status Flags:
  bit 0 (0x01): pit
  bit 1 (0x02): retired
  bit 2 (0x04): out_lap
  bit 3 (0x08): player
  bit 4 (0x10): wrong_way
  bit 5 (0x20): estimated (fallback from track model centreline)
  bit 6 (0x40): drs_active
"""
from __future__ import annotations

import asyncio
import logging
import math
import struct
import time
from dataclasses import dataclass
from typing import Any, Optional

from f1telemetry.state import RUNNING, RaceState
from trackmodel import TrackModel
from trackmap import TrackOutline

log = logging.getLogger("engineer.positions")

MAGIC = b"FP"
HEADER_STRUCT = struct.Struct("<2sHIBB")  # 10 bytes: magic(2), seq(2), timestamp_ms(4), track_id(1), num_cars(1)
CAR_STRUCT = struct.Struct("<BiiHHBB")    # 15 bytes: car_idx(1), x(4), z(4), heading(2), speed(2), pos(1), flags(1)

FLAG_PIT = 1 << 0         # 0x01
FLAG_RETIRED = 1 << 1     # 0x02
FLAG_OUT_LAP = 1 << 2     # 0x04
FLAG_PLAYER = 1 << 3      # 0x08
FLAG_WRONG_WAY = 1 << 4   # 0x10
FLAG_ESTIMATED = 1 << 5   # 0x20
FLAG_DRS_ACTIVE = 1 << 6  # 0x40


@dataclass
class CarPosition:
    car_idx: int
    x: float
    z: float
    heading: float  # radians in [0, 2*pi)
    speed_kph: float
    position: int
    flags: int = 0

    @property
    def is_pit(self) -> bool:
        return bool(self.flags & FLAG_PIT)

    @property
    def is_retired(self) -> bool:
        return bool(self.flags & FLAG_RETIRED)

    @property
    def is_out_lap(self) -> bool:
        return bool(self.flags & FLAG_OUT_LAP)

    @property
    def is_player(self) -> bool:
        return bool(self.flags & FLAG_PLAYER)

    @property
    def is_wrong_way(self) -> bool:
        return bool(self.flags & FLAG_WRONG_WAY)

    @property
    def is_estimated(self) -> bool:
        return bool(self.flags & FLAG_ESTIMATED)

    @property
    def is_drs_active(self) -> bool:
        return bool(self.flags & FLAG_DRS_ACTIVE)


def encode_positions_frame(
    seq: int,
    timestamp_ms: int,
    track_id: int,
    cars: list[CarPosition],
) -> bytes:
    """Packs a header and 0..24 car positions into a compact binary frame."""
    num_cars = len(cars)
    buf = bytearray(HEADER_STRUCT.size + num_cars * CAR_STRUCT.size)
    HEADER_STRUCT.pack_into(
        buf,
        0,
        MAGIC,
        seq & 0xFFFF,
        timestamp_ms & 0xFFFFFFFF,
        max(0, min(255, track_id)),
        max(0, min(255, num_cars)),
    )
    offset = HEADER_STRUCT.size
    for c in cars:
        x_tenths = int(round(c.x * 10))
        z_tenths = int(round(c.z * 10))
        norm_h = c.heading % (2 * math.pi)
        if norm_h < 0:
            norm_h += 2 * math.pi
        h_u16 = int(round((norm_h / (2 * math.pi)) * 65535)) % 65536
        spd_u16 = max(0, min(65535, int(round(c.speed_kph))))
        pos_u8 = max(0, min(255, int(c.position)))
        CAR_STRUCT.pack_into(
            buf,
            offset,
            c.car_idx & 0xFF,
            x_tenths,
            z_tenths,
            h_u16,
            spd_u16,
            pos_u8,
            c.flags & 0xFF,
        )
        offset += CAR_STRUCT.size
    return bytes(buf)


def decode_positions_frame(buf: bytes) -> dict[str, Any]:
    """Unpacks a compact binary frame into a Python dict (for testing and clients)."""
    if len(buf) < HEADER_STRUCT.size:
        raise ValueError(f"Buffer too short for header: {len(buf)} < {HEADER_STRUCT.size}")
    magic, seq, timestamp_ms, track_id, num_cars = HEADER_STRUCT.unpack_from(buf, 0)
    if magic != MAGIC:
        raise ValueError(f"Invalid magic: {magic!r}")
    expected_len = HEADER_STRUCT.size + num_cars * CAR_STRUCT.size
    if len(buf) < expected_len:
        raise ValueError(f"Buffer too short for {num_cars} cars: {len(buf)} < {expected_len}")

    cars = []
    offset = HEADER_STRUCT.size
    for _ in range(num_cars):
        idx, x_tenths, z_tenths, h_u16, spd, pos, flags = CAR_STRUCT.unpack_from(buf, offset)
        x = x_tenths / 10.0
        z = z_tenths / 10.0
        heading = (h_u16 / 65535.0) * (2 * math.pi)
        cars.append(
            CarPosition(
                car_idx=idx,
                x=x,
                z=z,
                heading=heading,
                speed_kph=float(spd),
                position=pos,
                flags=flags,
            )
        )
        offset += CAR_STRUCT.size

    return {
        "seq": seq,
        "timestamp_ms": timestamp_ms,
        "track_id": track_id,
        "num_cars": num_cars,
        "cars": cars,
    }


def extract_car_positions(
    state: RaceState,
    track_model: Optional[TrackModel] = None,
    track_outline: Optional[TrackOutline] = None,
) -> list[CarPosition]:
    """Extracts positions for all active cars with fallback to track model centreline."""
    num_cars = 24
    if state.lap:
        num_cars = max(len(state.lap), 20)
    elif state.motion:
        num_cars = len(state.motion)

    positions: list[CarPosition] = []

    for i in range(num_cars):
        m = state.motion[i] if (state.motion and i < len(state.motion)) else None
        lap = state.lap[i] if (state.lap and i < len(state.lap)) else None
        tel = state.tel[i] if (state.tel and i < len(state.tel)) else None

        # Check if car has valid, active Motion data
        has_motion = (
            m is not None
            and (abs(m.x) > 0.01 or abs(m.z) > 0.01)
            and math.isfinite(m.x)
            and math.isfinite(m.z)
        )

        flags = 0
        if i == state.player:
            flags |= FLAG_PLAYER

        # Status flags from lap data
        if lap:
            pos = lap.position if lap.position > 0 else (i + 1)
            if lap.pit_status != 0:
                flags |= FLAG_PIT
            if lap.result_status not in RUNNING:
                flags |= FLAG_RETIRED
            if getattr(lap, "driver_status", 0) == 3 or (lap.pit_status != 0 and lap.lap_num <= 1):
                flags |= FLAG_OUT_LAP
        else:
            pos = i + 1

        if tel and getattr(tel, "drs", 0):
            flags |= FLAG_DRS_ACTIVE

        if has_motion:
            assert m is not None
            x = float(m.x)
            z = float(m.z)
            # Calculate heading from forward vector if available, else yaw
            fwd_x = getattr(m, "fwd_x", 0.0)
            fwd_z = getattr(m, "fwd_z", 0.0)
            if abs(fwd_x) > 1e-4 or abs(fwd_z) > 1e-4:
                heading = math.atan2(fwd_x, fwd_z) % (2 * math.pi)
            else:
                heading = float(getattr(m, "yaw", 0.0)) % (2 * math.pi)

            # Speed
            if tel and tel.speed_kph > 0:
                speed_kph = float(tel.speed_kph)
            else:
                vx = getattr(m, "vx", 0.0)
                vz = getattr(m, "vz", 0.0)
                speed_kph = math.sqrt(vx * vx + vz * vz) * 3.6

            # Check wrong-way against track model centreline
            if track_model and lap and lap.lap_distance >= 0:
                b = track_model.get_bin(lap.lap_distance)
                if b:
                    angle_diff = abs((heading - b.heading + math.pi) % (2 * math.pi) - math.pi)
                    if angle_diff > math.radians(100):  # > 100 deg deviation from track direction
                        flags |= FLAG_WRONG_WAY
        else:
            # Fallback when Motion isn't arriving (garage, spectating, player not in control)
            if not lap or lap.result_status not in RUNNING:
                continue  # Skip inactive cars with no motion

            flags |= FLAG_ESTIMATED
            lap_dist = max(0.0, lap.lap_distance)
            placed = False

            if track_model and track_model.bins:
                b = track_model.get_bin(lap_dist)
                if b:
                    x = b.x
                    z = b.z
                    heading = b.heading % (2 * math.pi)
                    speed_kph = float(tel.speed_kph) if tel else float(b.median_speed_kph)
                    placed = True

            if not placed and track_outline and track_outline.points:
                # Find nearest outline point by lap distance
                best_pt = None
                best_diff = 999999.0
                pts = track_outline.points
                for idx_pt, pt in enumerate(pts):
                    # pt is [x, z, lap_distance, sector]
                    pt_dist = pt[2] if len(pt) > 2 else 0.0
                    d_diff = abs(pt_dist - lap_dist)
                    if d_diff < best_diff:
                        best_diff = d_diff
                        best_pt = (pt, idx_pt)

                if best_pt:
                    cur_pt, cur_idx = best_pt
                    x = float(cur_pt[0])
                    z = float(cur_pt[1])
                    # Heading towards next point
                    next_pt = pts[(cur_idx + 1) % len(pts)]
                    dx = next_pt[0] - cur_pt[0]
                    dz = next_pt[1] - cur_pt[1]
                    heading = math.atan2(dx, dz) % (2 * math.pi)
                    speed_kph = float(tel.speed_kph) if tel else 150.0
                    placed = True

            if not placed:
                continue

        positions.append(
            CarPosition(
                car_idx=i,
                x=x,
                z=z,
                heading=heading,
                speed_kph=speed_kph,
                position=pos,
                flags=flags,
            )
        )

    return positions


def interpolate_positions(p0: CarPosition, p1: CarPosition, t: float) -> CarPosition:
    """Interpolates position linearly and heading by shortest angle."""
    t = max(0.0, min(1.0, t))
    x = p0.x + (p1.x - p0.x) * t
    z = p0.z + (p1.z - p0.z) * t
    spd = p0.speed_kph + (p1.speed_kph - p0.speed_kph) * t

    # Shortest angle heading interpolation
    diff = (p1.heading - p0.heading + math.pi) % (2 * math.pi) - math.pi
    heading = (p0.heading + diff * t) % (2 * math.pi)

    flags = p1.flags if t >= 0.5 else p0.flags
    pos = p1.position if t >= 0.5 else p0.position

    return CarPosition(
        car_idx=p1.car_idx,
        x=x,
        z=z,
        heading=heading,
        speed_kph=spd,
        position=pos,
        flags=flags,
    )


def extrapolate_position(
    pos: CarPosition,
    dt_s: float,
    max_extrap_s: float = 0.200,
) -> CarPosition:
    """Extrapolates car position using velocity up to max_extrap_s (200ms), then holds."""
    dt = max(0.0, min(dt_s, max_extrap_s))
    speed_mps = pos.speed_kph / 3.6
    vx = speed_mps * math.sin(pos.heading)
    vz = speed_mps * math.cos(pos.heading)

    return CarPosition(
        car_idx=pos.car_idx,
        x=pos.x + vx * dt,
        z=pos.z + vz * dt,
        heading=pos.heading,
        speed_kph=pos.speed_kph,
        position=pos.position,
        flags=pos.flags,
    )


class PositionsBroadcaster:
    """Manages WebSocket client queues and non-blocking binary frame distribution.

    A slow client queue has size 1. If full, the old frame is dropped immediately,
    ensuring a slow client never blocks the broadcaster or any other client.
    """
    def __init__(self):
        self._clients: set[asyncio.Queue[bytes]] = set()
        self._seq: int = 0
        self._last_frame: bytes | None = None
        self._dropped_frames: int = 0

    @property
    def client_count(self) -> int:
        return len(self._clients)

    @property
    def dropped_frames(self) -> int:
        return self._dropped_frames

    def register(self) -> asyncio.Queue[bytes]:
        q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=1)
        self._clients.add(q)
        if self._last_frame is not None:
            try:
                q.put_nowait(self._last_frame)
            except asyncio.QueueFull:
                pass
        return q

    def unregister(self, q: asyncio.Queue[bytes]):
        self._clients.discard(q)

    def next_seq(self) -> int:
        self._seq = (self._seq + 1) & 0xFFFF
        return self._seq

    def broadcast_frame(self, frame: bytes):
        self._last_frame = frame
        for q in list(self._clients):
            if q.full():
                try:
                    q.get_nowait()
                    self._dropped_frames += 1
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass
            try:
                q.put_nowait(frame)
            except asyncio.QueueFull:
                self._dropped_frames += 1
