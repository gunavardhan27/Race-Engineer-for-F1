"""Feeds hand-built F1 25 packets through the pipeline. Run: python test_synthetic.py"""
import json
import struct

from f1telemetry import packets
from f1telemetry.deterministic import Callouts, route
from f1telemetry.packets import DAMAGE_HEAD, FORECAST, HEADER, LAP, NUM_CARS, SESSION_HEAD, STATUS, TEL, PacketId, udp_action
from f1telemetry.state import RaceState

ME, LEADER, CHASER = 0, 1, 2


def header(pid):
    return HEADER.pack(2025, 25, 1, 0, 1, pid, 42, 0.0, 0, 0, ME, 255)


def lap_packet(lap, cars):            # {idx: (position, gap_ahead_ms, last_lap_ms)}
    body = b""
    for i in range(NUM_CARS):
        row = [0] * 33
        if i in cars:
            row[13], row[6], row[0] = cars[i]
            row[14], row[26] = lap, 2
        body += LAP.pack(*row)
    return header(PacketId.LAP_DATA) + body + b"\xff\xff"


def status_packet(fuel, delta):
    body = b""
    for i in range(NUM_CARS):
        row = [0] * 25
        if i == ME:
            row[5], row[6], row[7], row[14], row[15], row[19] = fuel, 110, delta, 17, 12, 2_000_000
        body += STATUS.pack(*row)
    return header(PacketId.CAR_STATUS) + body


def damage_packet(wear, fl_wing):     # F1 25 stride: 46 bytes (4 blister bytes after brake damage)
    body = b""
    for i in range(NUM_CARS):
        w = wear if i == ME else (0, 0, 0, 0)
        parts = bytes([fl_wing if i == ME else 0]) + bytes(17)
        body += DAMAGE_HEAD.pack(*w, *[0] * 8) + bytes(4) + parts
    return header(PacketId.CAR_DAMAGE) + body


def telemetry_packet():
    row = [250, 1.0, 0.0, 0.0, 0, 7, 11000, 0, 50, 0, *[500] * 4, 95, 96, 92, 93, *[100] * 4, 105, *[23.5] * 4, *[0] * 4]
    return header(PacketId.CAR_TELEMETRY) + TEL.pack(*row) * NUM_CARS + bytes(3)


def motion_packet(cars=None, trailer=120):
    """Synthetic Motion packet (PacketId.MOTION = 0).
    cars can be a dict: {idx: (x, z, yaw, g_lat, g_lon, g_vert)}
    """
    body = b""
    cars = cars or {}
    for i in range(NUM_CARS):
        if i in cars:
            x, z, yaw, g_lat, g_lon, g_vert = cars[i]
            row = (x, 0.0, z, 0.0, 0.0, 0.0, 0, 0, 32767, 32767, 0, 0, g_lat, g_lon, g_vert, yaw, 0.0, 0.0)
        else:
            row = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0, 0, 32767, 32767, 0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
        body += packets.MOTION.pack(*row)
    return header(PacketId.MOTION) + body + bytes(trailer)


def session_packet():
    b = bytearray(700)
    SESSION_HEAD.pack_into(b, 0, 2, 32, 24, 30, 5891, 15, 7, 0, 3600, 7200, 80, 0, 0, 0, 0, 0)
    b[126] = 1
    FORECAST.pack_into(b, 127, 15, 10, 3, 31, 0, 23, 0, 60)
    b[653], b[654], b[655] = 6, 9, 12
    return header(PacketId.SESSION) + bytes(b)


def participants_packet(names, stride=57):
    b = bytearray(1 + NUM_CARS * stride)
    b[0] = len(names)
    for i, n in names.items():
        o = 1 + i * stride
        b[o + 7:o + 7 + len(n)] = n.encode()
    return header(PacketId.PARTICIPANTS) + bytes(b)


def event(code, payload=b""):
    return header(PacketId.EVENT) + code.encode() + payload.ljust(12, b"\0")


def test_synthetic():
    st, co, said, sigs = RaceState(ptt_action=1), Callouts(), [], []

    def feed(buf):
        h, data = packets.parse(buf)
        assert data is not None, h
        for sig in st.update(h, data):
            sigs.append(sig)
            said.extend(c.text for c in co.process(sig, st))

    feed(participants_packet({ME: "Player", LEADER: "LECLERC", CHASER: "NORRIS"}))
    feed(session_packet())
    feed(motion_packet({ME: (120.5, 340.2, 1.57, 1.8, -2.5, 1.0)}))
    feed(telemetry_packet())
    feed(status_packet(42.0, -0.1))
    feed(damage_packet((30, 31, 40, 38), 0))
    feed(lap_packet(4, {LEADER: (1, 0, 0), ME: (2, 1500, 0), CHASER: (3, 1300, 0)}))
    feed(lap_packet(5, {LEADER: (1, 0, 0), ME: (2, 1400, 91500), CHASER: (3, 1200, 0)}))
    feed(status_packet(40.2, -0.3))
    feed(damage_packet((40, 41, 55, 52), 0))
    feed(lap_packet(6, {LEADER: (1, 0, 0), ME: (2, 1100, 91234), CHASER: (3, 900, 0)}))
    feed(damage_packet((40, 41, 55, 52), 25))
    feed(event("BUTN", struct.pack("<I", udp_action(1))))
    feed(event("BUTN", struct.pack("<I", 0)))
    feed(event("SCAR", bytes([1, 0])))

    print("Callouts:")
    for t in said:
        print("  ", t)
    assert any("Norris is within a second, 0.9" in t for t in said)
    assert any("Gaining 0.3 a lap on Leclerc" in t for t in said)
    assert any("Fuel's minus 0.3" in t for t in said)
    assert any("Tyre wear 55 percent, front left" in t for t in said)
    assert any("Pit window's open" in t for t in said)
    assert any("Rain coming, 60 percent in 10 minutes" in t for t in said)
    assert any("front left wing 25" in t for t in said)
    assert "Safety car, safety car." in said
    assert [s for s in sigs if s[0] == "ptt"] == [("ptt", True), ("ptt", False)]
    assert st.my_motion is not None
    assert round(st.my_motion.x, 2) == 120.5 and round(st.my_motion.z, 2) == 340.2
    assert round(st.my_motion.g_lat, 2) == 1.8 and round(st.my_motion.g_lon, 2) == -2.5

    print("\nDriver questions:")
    for q in ["What's the gap ahead?", "gap behind", "how's the fuel", "how are the tyres",
              "any rain coming?", "I'm washing out at Copse, front left is cooking", "should we box now?"]:
        r = route(q, st)
        print(f"   {q!r:55} -> {r.kind:6} {r.text or ''}")

    assert "Leclerc ahead, 1.1. Gaining 0.3" in route("What's the gap ahead?", st).text
    assert route("I'm washing out at Copse, front left is cooking", st).kind == "ai"
    assert route("should we box now?", st).kind == "ai"

    snap = st.snapshot()
    json.dumps(snap)
    print("\nSnapshot:", json.dumps(snap, indent=1)[:900], "...")


if __name__ == "__main__":
    test_synthetic()
    print("\nAll checks passed.")

