"""Show exactly what the game is sending: packet types, rates, sizes and format.

    python inspect_telemetry.py                    # listen live on 127.0.0.1:20777 for 20 s
    python inspect_telemetry.py --seconds 60
    python inspect_telemetry.py --file session.bin # analyse a recording made with main.py --record

Close the race engineer app first when listening live (only one program can use the port).
"""
import argparse
import socket
import struct
import time
from collections import defaultdict

from f1telemetry import packets

NAMES = {0: "Motion", 1: "Session", 2: "LapData", 3: "Event", 4: "Participants", 5: "CarSetups",
         6: "CarTelemetry", 7: "CarStatus", 8: "FinalClassification", 9: "LobbyInfo", 10: "CarDamage",
         11: "SessionHistory", 12: "TyreSets", 13: "MotionEx", 14: "TimeTrial", 15: "LapPositions",
         16: "CarTelemetry2"}
# Sizes from EA's 2026 Season Pack spec (docs/udp_spec_2026.h)
EXPECTED_2026 = {0: 1325, 1: 926, 2: 1399, 3: 45, 4: 1470, 5: 1233, 6: 1448, 7: 1445, 8: 1134, 9: 1062,
                 10: 1133, 11: 1460, 12: 231, 13: 273, 14: 104, 15: 1231, 16: 269}


def frames_live(host, port, seconds):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind((host, port))
    s.settimeout(0.5)
    end = time.monotonic() + seconds
    print(f"Listening on {host}:{port} for {seconds} s, drive on track now...")
    while time.monotonic() < end:
        try:
            buf, _ = s.recvfrom(4096)
            yield time.monotonic(), buf
        except socket.timeout:
            pass


def frames_file(path):
    frame = struct.Struct("<dI")
    with open(path, "rb") as f:
        while hdr := f.read(frame.size):
            t, n = frame.unpack(hdr)
            yield t, f.read(n)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=20777)
    ap.add_argument("--seconds", type=float, default=20)
    a = ap.parse_args()

    src = frames_file(a.file) if a.file else frames_live(a.host, a.port, a.seconds)
    count, sizes, events = defaultdict(int), defaultdict(set), defaultdict(int)
    first = last = None
    formats, versions, player = set(), set(), set()
    session_info, my_team = None, None
    for t, buf in src:
        h = packets.parse_header(buf)
        if h is None:
            continue
        first = t if first is None else first
        last = t
        formats.add(h.packet_format)
        versions.add((h.game_year, buf[3], buf[4]))
        player.add(h.player_idx)
        count[h.packet_id] += 1
        sizes[h.packet_id].add(len(buf))
        if h.packet_id == 3:
            events[buf[29:33].decode("ascii", "replace")] += 1
        if h.packet_id in (1, 4) and h.player_idx < 24:
            _, data = packets.parse(buf)
            if data is not None and h.packet_id == 1:
                session_info = (data.track_id, buf[29 + 8], data.session_type, data.total_laps)  # m_formula is byte 8 after the header
            elif data is not None and h.packet_id == 4 and h.player_idx < len(data):
                p = data[h.player_idx]
                my_team = (p.team_id, p.name)

    if not count:
        print("No packets received. Check the game's UDP settings, and that nothing else is using the port.")
        return
    span = max(last - first, 1e-6)
    print(f"\nPacket format: {sorted(formats)}   game year/version: {sorted(versions)}   player car index: {sorted(player)}")
    print(f"{'id':>3} {'packet':<20} {'count':>7} {'rate Hz':>8} {'size':>14}  check")
    for pid in sorted(count):
        sz = ", ".join(str(x) for x in sorted(sizes[pid]))
        exp = EXPECTED_2026.get(pid)
        ok = "" if 2026 not in formats or exp is None else ("ok" if sizes[pid] == {exp} else f"EXPECTED {exp}")
        print(f"{pid:>3} {NAMES.get(pid, '?'):<20} {count[pid]:>7} {count[pid] / span:>8.1f} {sz:>14}  {ok}")
    if session_info:
        print(f"\nSession: track_id={session_info[0]}  formula={session_info[1]} (13 = F1 26 cars)  "
              f"session_type={session_info[2]}  total_laps={session_info[3]}")
    if my_team:
        print(f"Player: team_id={my_team[0]}  name={my_team[1]!r}")
    if events:
        print("\nEvents:", ", ".join(f"{k} x{v}" for k, v in sorted(events.items())))


if __name__ == "__main__":
    main()
