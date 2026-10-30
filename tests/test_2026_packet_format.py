"""Tests for F1 2026 Season Pack packet formats and 24-car arrays."""
import logging
import struct
import pytest

from f1telemetry import packets
from f1telemetry.deterministic import Callouts, route
from f1telemetry.packets import (
    CAR_SETUP,
    DAMAGE_HEAD,
    DAMAGE_PARTS,
    EXPECTED_2026,
    FINAL_CLASS_DATA_2026,
    FORECAST,
    HEADER,
    LAP,
    MOTION_2026,
    PACKET_NAMES,
    SESSION_HEAD,
    STATUS_2026,
    TEL2_2026,
    TEL_2026,
    TYRE_SET_DATA,
    Header,
    PacketId,
    num_cars,
    parse,
    parse_header,
)
from f1telemetry.spotter import Spotter, SpotterState
from f1telemetry.state import RaceState
from personality import PersonalityEngine


def header_2026(pid: int, player: int = 0, uid: int = 9999) -> bytes:
    """Builds a 2026 specification header (29 bytes)."""
    return HEADER.pack(2026, 26, 1, 0, 1, pid, uid, 123.45, 1000, 1000, player, 255)


def build_motion_2026(player: int = 0) -> bytes:
    """Motion packet: 29 hdr + 24 * 54 = 1325 bytes."""
    hdr = header_2026(PacketId.MOTION, player=player)
    cars = b""
    for i in range(24):
        # x, y, z, vx, vy, vz, 3 fwd, 3 rt, 3 g (quantised / 1000), yaw, pitch, roll
        row = (
            float(i * 10), 1.0, float(i * 5),
            50.0, 0.0, 0.0,
            0, 0, 32767,
            32767, 0, 0,
            1500, -500, 200,  # 1.5g lat, -0.5g lon, 0.2g vert
            0.12, 0.01, -0.02
        )
        cars += MOTION_2026.pack(*row)
    return hdr + cars


def build_session_2026(player: int = 0) -> bytes:
    """Session packet: 926 bytes."""
    hdr = header_2026(PacketId.SESSION, player=player)
    b = bytearray(926 - HEADER.size)
    # SESSION_HEAD: 19 bytes
    # weather, track_temp, air_temp, total_laps, track_length, session_type, track_id, formula(13=F1 26), time_left
    SESSION_HEAD.pack_into(b, 0, 1, 35, 25, 53, 5800, 10, 7, 13, 3600, 7200, 80, 0, 0, 0, 0, 0)
    # Safety car byte at offset 19 + 21*5 = 124
    b[124] = 0  # no safety car
    b[126] = 1  # 1 weather sample
    FORECAST.pack_into(b, 127, 10, 15, 2, 36, 0, 26, 0, 10)

    # Pit stops at base 653 (relative to header: 653)
    b[653], b[654], b[655] = 18, 22, 5

    # Corner-cutting stringency at offset 693: 1 = strict
    b[693] = 1

    # Sector 2 and 3 start distances at offset 713
    struct.pack_into("<ff", b, 713, 1850.5, 3920.0)

    # Active aero status at 721
    b[721] = 0  # full

    # startReactionTime at 885 (relative to header: 885)
    struct.pack_into("<f", b, 885, 0.21)
    b[889] = 1  # ABS assist
    b[890] = 2  # TC assist

    return hdr + bytes(b)


def build_lap_data_2026(player: int = 0) -> bytes:
    """LapData packet: 29 hdr + 24 * 57 + 2 trailer = 1399 bytes."""
    hdr = header_2026(PacketId.LAP_DATA, player=player)
    cars = b""
    for i in range(24):
        row = [0] * 33
        row[0] = 85000 + i * 500  # last_lap_ms
        row[1] = 42000            # current_lap_ms
        row[6] = 1200 + i * 100   # gap to ahead
        row[10] = 1500.0          # lap_distance
        row[11] = 5000.0          # total_distance
        row[13] = i + 1           # position
        row[14] = 3               # lap_num
        row[26] = 2               # result_status = active
        cars += LAP.pack(*row)
    trailer = b"\x00\x00"
    return hdr + cars + trailer


def build_event_2026_coll(v1: int, v2: int, severity: int) -> bytes:
    """Event packet: 29 hdr + 4 code + 12 details = 45 bytes."""
    hdr = header_2026(PacketId.EVENT)
    code = b"COLL"
    details = struct.pack("<BBB", v1, v2, severity).ljust(12, b"\x00")
    return hdr + code + details


def build_participants_2026(player: int = 0) -> bytes:
    """Participants packet: 29 hdr + 1 numCars + 24 * 60 = 1470 bytes."""
    hdr = header_2026(PacketId.PARTICIPANTS, player=player)
    body = bytearray(1 + 24 * 60)
    body[0] = 24  # numActiveCars
    for i in range(24):
        o = 1 + i * 60
        # uint8 ai, uint16 driverId, uint16 networkId, uint16 teamId, uint8 myTeam, uint8 raceNumber, uint8 nationality
        ai = 0 if i == player else 1
        driver_id = 100 + i
        net_id = 200 + i
        team_id = 1000 + (i % 10)  # uint16 team ID!
        my_team = 1 if i == player else 0
        race_num = 1 + i
        nat = 1
        struct.pack_into("<BHHHBBB", body, o, ai, driver_id, net_id, team_id, my_team, race_num, nat)
        name = f"Driver {i}".encode("utf-8")
        body[o + 10 : o + 10 + len(name)] = name
    return hdr + bytes(body)


def build_car_setups_2026(player: int = 0) -> bytes:
    """CarSetups packet: 29 hdr + 24 * 50 + 4 trailer = 1233 bytes."""
    hdr = header_2026(PacketId.CAR_SETUPS, player=player)
    cars = b""
    for i in range(24):
        # 23 setup fields
        row = (
            25, 20, 50, 50,
            -3.0, -1.5, 0.05, 0.20,
            8, 6, 8, 6, 35, 45,
            100, 54, 5,
            23.5, 23.5, 24.0, 24.0,
            0, 45.0
        )
        cars += CAR_SETUP.pack(*row)
    trailer = struct.pack("<f", 26.0)
    return hdr + cars + trailer


def build_car_telemetry_2026(player: int = 0) -> bytes:
    """CarTelemetry packet: 29 hdr + 24 * 59 + 3 trailer = 1448 bytes."""
    hdr = header_2026(PacketId.CAR_TELEMETRY, player=player)
    cars = b""
    for i in range(24):
        # TEL_2026: uint8 engine temp
        row = (
            295, 1.0, 0.0, 0.0, 0, 7, 11500, 1,
            60, 0x0FFF,
            500, 500, 500, 500,
            98, 97, 95, 96,
            102, 101, 99, 100,
            92,  # engine temperature (uint8)
            23.5, 23.5, 24.0, 24.0,
            0, 0, 0, 0
        )
        cars += TEL_2026.pack(*row)
    trailer = bytes(3)
    return hdr + cars + trailer


def build_car_status_2026(player: int = 0) -> bytes:
    """CarStatus packet: 29 hdr + 24 * 59 = 1445 bytes."""
    hdr = header_2026(PacketId.CAR_STATUS, player=player)
    cars = b""
    for i in range(24):
        # STATUS_2026: includes ersHarvestLimitPerLap float
        row = (
            0, 0, 1, 54, 0,
            42.5, 110.0, 1.2,
            12500, 4000, 8,
            1, 0,
            17, 17, 12,
            0, 750000.0, 150000.0, 2500000.0,
            1, 100000.0, 50000.0, 2000000.0, 500000.0,
            0
        )
        cars += STATUS_2026.pack(*row)
    return hdr + cars


def build_final_classification_2026(player: int = 0) -> bytes:
    """FinalClassification packet: 29 hdr + 1 numCars + 24 * 46 = 1134 bytes."""
    hdr = header_2026(PacketId.FINAL_CLASSIFICATION, player=player)
    body = bytearray(1 + 24 * 46)
    body[0] = 24
    for i in range(24):
        o = 1 + i * 46
        # position, numLaps, gridPosition, points, numPitStops, resultStatus, resultReason, bestLapTimeMS, totalRaceTime, penaltiesTime, numPenalties
        stints = [17] * 8
        stints_vis = [17] * 8
        end_laps = [15] * 8
        row = (
            i + 1, 50, i + 1, 25 - i if i < 10 else 0,
            2, 3, 2,  # resultStatus=3 (finished), resultReason=2 (finished)
            83450, 4800.123, 0, 0, 8,  # numTyreStints = 8
            *stints, *stints_vis, *end_laps
        )
        FINAL_CLASS_DATA_2026.pack_into(body, o, *row)
    return hdr + bytes(body)


def build_car_damage_2026(player: int = 0) -> bytes:
    """CarDamage packet: 29 hdr + 24 * 46 = 1133 bytes."""
    hdr = header_2026(PacketId.CAR_DAMAGE, player=player)
    cars = b""
    for i in range(24):
        w = (15.0, 16.0, 22.0, 20.0, 0, 0, 0, 0, 0, 0, 0, 0)
        parts = (0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
        cars += DAMAGE_HEAD.pack(*w) + bytes(4) + DAMAGE_PARTS.pack(*parts) + bytes(8)
    return hdr + cars


def build_tyre_sets_2026() -> bytes:
    """TyreSets packet: 29 hdr + 1 carIdx + 20 * 10 + 1 fitted = 231 bytes."""
    hdr = header_2026(PacketId.TYRE_SETS)
    body = bytearray(1 + 200 + 1)
    body[0] = 0  # carIdx
    for i in range(20):
        o = 1 + i * 10
        # actual, visual, wear, available, rec_sess, life, usable, delta, fitted
        TYRE_SET_DATA.pack_into(body, o, 17, 17, 10 * (i % 5), 1, 0, 25, 30, i * 200, 1 if i == 0 else 0)
    body[201] = 0  # fittedIdx
    return hdr + bytes(body)


def build_lap_positions_2026() -> bytes:
    """LapPositions packet: 29 hdr + 2 numLaps/lapStart + 50 * 24 = 1231 bytes."""
    hdr = header_2026(PacketId.LAP_POSITIONS)
    body = bytearray(2 + 50 * 24)
    body[0] = 10  # numLaps
    body[1] = 0   # lapStart
    for lap in range(50):
        for car in range(24):
            body[2 + lap * 24 + car] = (car + lap) % 24 + 1
    return hdr + bytes(body)


def build_car_telemetry2_2026(
    player: int = 0,
    aero_mode: int = 0,
    aero_avail: bool = False,
    aero_dist: int = 0,
    ot_avail: bool = False,
    ot_active: bool = False,
    ot_dist: int = 0,
    regs_2026: bool = True,
    wrong_way: bool = False,
) -> bytes:
    """CarTelemetry2 packet: 29 hdr + 24 * 10 = 269 bytes."""
    hdr = header_2026(PacketId.CAR_TELEMETRY2, player=player)
    cars = b""
    for i in range(24):
        if i == player:
            cars += TEL2_2026.pack(
                aero_mode, int(aero_avail), aero_dist,
                int(ot_avail), int(ot_active), ot_dist,
                int(regs_2026), int(wrong_way)
            )
        else:
            cars += TEL2_2026.pack(0, 0, 0, 0, 0, 0, 1, 0)
    return hdr + cars


# =========================================================================
# TEST SUITE
# =========================================================================

def test_2026_exact_packet_sizes_and_parsing():
    """Validates that synthetic 2026 packets match exact EA UDP 2026 spec byte counts."""
    packet_generators = {
        PacketId.MOTION: build_motion_2026(),
        PacketId.SESSION: build_session_2026(),
        PacketId.LAP_DATA: build_lap_data_2026(),
        PacketId.EVENT: build_event_2026_coll(0, 1, 2),
        PacketId.PARTICIPANTS: build_participants_2026(),
        PacketId.CAR_SETUPS: build_car_setups_2026(),
        PacketId.CAR_TELEMETRY: build_car_telemetry_2026(),
        PacketId.CAR_STATUS: build_car_status_2026(),
        PacketId.FINAL_CLASSIFICATION: build_final_classification_2026(),
        PacketId.CAR_DAMAGE: build_car_damage_2026(),
        PacketId.TYRE_SETS: build_tyre_sets_2026(),
        PacketId.LAP_POSITIONS: build_lap_positions_2026(),
        PacketId.CAR_TELEMETRY2: build_car_telemetry2_2026(),
    }

    for pid, buf in packet_generators.items():
        expected_len = EXPECTED_2026[pid]
        pname = PACKET_NAMES[pid]
        assert len(buf) == expected_len, f"{pname} len {len(buf)} != expected {expected_len}"
        hdr, parsed = parse(buf)
        assert hdr is not None, f"Failed to parse header for {pname}"
        assert hdr.packet_format == 2026
        assert parsed is not None, f"Failed to parse payload for {pname}"


def test_24_cars_end_to_end_and_car_index_23():
    """Validates that 24 cars parse end-to-end and car index 23 works in state, timing tower, spotter."""
    player_car = 23
    state = RaceState()

    # Feed participants (car 23 is Player)
    p_buf = build_participants_2026(player=player_car)
    h, parts = parse(p_buf)
    state.update(h, parts)
    assert len(state.participants) == 24
    assert state.name(23) == "23"

    # Feed lap data
    l_buf = build_lap_data_2026(player=player_car)
    h, lap = parse(l_buf)
    state.update(h, lap)
    assert len(state.lap) == 24
    assert state.me is not None
    assert state.me.position == 24

    # Timing tower contains all 24 cars
    tower = state.timing_tower()
    assert len(tower) == 24
    player_row = next(r for r in tower if r["is_player"])
    assert player_row["car_idx"] == 23

    # Feed motion with car 23
    m_buf = build_motion_2026(player=player_car)
    h, mot = parse(m_buf)
    state.update(h, mot)
    assert len(state.motion) == 24
    assert state.my_motion is not None
    assert round(state.my_motion.g_lat, 2) == 1.5

    # Feed telemetry & status
    t_buf = build_car_telemetry_2026(player=player_car)
    h, tel = parse(t_buf)
    state.update(h, tel)
    assert len(state.tel) == 24
    assert state.my_tel.speed_kph == 295

    s_buf = build_car_status_2026(player=player_car)
    h, stat = parse(s_buf)
    state.update(h, stat)
    assert len(state.status) == 24
    assert state.my_status.ers_harvest_limit_per_lap == 2000000.0

    # Spotter with 24 cars
    spotter = Spotter()
    # No crash with 24 cars
    phrase = spotter.process(state)
    assert phrase is None or isinstance(phrase, str)


def test_participants_uint16_team_ids_and_offsets():
    """Validates that Participants packet decodes uint16 driver/team IDs and name at offset 10."""
    p_buf = build_participants_2026(player=0)
    h, participants = parse(p_buf)
    assert len(participants) == 24
    p0 = participants[0]
    assert p0.name == "Driver 0"
    assert p0.team_id == 1000
    p1 = participants[1]
    assert p1.name == "Driver 1"
    assert p1.team_id == 1001


def test_collision_severity():
    """Validates that COLL event decodes severity and personality triggers medium/high only."""
    # Severity 0: Low
    buf_low = build_event_2026_coll(0, 1, 0)
    h_low, ev_low = parse(buf_low)
    assert ev_low.severity == 0

    # Severity 1: Medium
    buf_med = build_event_2026_coll(0, 1, 1)
    h_med, ev_med = parse(buf_med)
    assert ev_med.severity == 1

    # Severity 2: High
    buf_high = build_event_2026_coll(0, 1, 2)
    h_high, ev_high = parse(buf_high)
    assert ev_high.severity == 2

    # Personality behavior
    state = RaceState()
    h, parts = parse(build_participants_2026(player=0))
    state.update(h, parts)
    personality = PersonalityEngine(lines_dir="lines")

    # Low severity: returns None (no roast)
    res_low = personality.on_signal(("event", ev_low), state)
    assert res_low is None

    # Medium severity: triggers roast
    res_med = personality.on_signal(("event", ev_med), state)
    assert res_med is not None
    text = res_med.text if hasattr(res_med, "text") else str(res_med)
    assert "Contact" in text or "mirror" in text or len(text) > 0


def test_car_telemetry2_wrong_way_and_overtake_mode():
    """Validates that CarTelemetry2 triggers priority 0 wrong-way call and overtake mode call."""
    state = RaceState()
    callouts = Callouts()

    # 1. Wrong way flag set
    buf_ww = build_car_telemetry2_2026(player=0, wrong_way=True)
    h, tel2 = parse(buf_ww)
    signals = state.update(h, tel2)
    assert ("wrong_way",) in signals

    calls = callouts.process(("wrong_way",), state)
    assert len(calls) == 1
    assert calls[0].priority == 0
    assert calls[0].text == "Wrong way!"

    # 2. Overtake mode available
    buf_ot = build_car_telemetry2_2026(player=0, ot_avail=True, ot_dist=150)
    h, tel2 = parse(buf_ot)
    signals = state.update(h, tel2)
    assert ("overtake_available", 150) in signals

    calls_ot = callouts.process(("overtake_available", 150), state)
    assert len(calls_ot) == 1
    assert "Overtake mode available" in calls_ot[0].text


def test_session_2026_fields_and_snapshot():
    """Validates 2026 Session fields, active aero snapshot, and DRS replacement."""
    state = RaceState()
    sess_buf = build_session_2026(player=0)
    h, sess = parse(sess_buf)
    state.update(h, sess)

    assert state.session.formula == 13  # F1 26
    assert state.session.sector2_distance_start == 1850.5
    assert state.session.sector3_distance_start == 3920.0
    assert state.session.start_reaction_time == pytest.approx(0.21, 0.01)
    assert state.session.corner_cutting_stringency == 1

    # Feed Tel2 with straight mode
    tel2_buf = build_car_telemetry2_2026(player=0, aero_mode=1, aero_avail=True, regs_2026=True)
    h, tel2 = parse(tel2_buf)
    state.update(h, tel2)

    snap = state.snapshot()
    assert snap["regs_2026"] is True
    assert snap["active_aero"]["mode"] == "straight"
    assert snap["drs"]["open"] is True

    # DRS question via route()
    reply = route("How is the DRS?", state)
    assert reply.kind == "answer"
    assert "Straight mode" in reply.text


def test_startup_size_mismatch_warning(caplog):
    """Validates that startup self-check logs a warning naming the packet on size mismatch."""
    packets._checked_packet_sizes.discard(PacketId.MOTION)
    with caplog.at_level(logging.WARNING):
        # Motion packet truncated by 10 bytes
        corrupted = build_motion_2026()[:-10]
        parse(corrupted)
        assert any("Packet size mismatch for Motion" in rec.message for rec in caplog.records)
