"""Replay Manager for recorded F1 UDP telemetry sessions (Part of Prompt 23).

Loads .bin and .bin.zst session recordings, builds keyframe indexes for instant scrubbing,
detects timeline markers (overtakes, pit stops, SC/VSC periods, yellow flags/incidents),
and provides async playback control (1x, 2x, 4x, 8x speed) feeding live track map and state.
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Optional

from f1telemetry import packets
from f1telemetry.packets import HEADER, PacketId
from f1telemetry.state import SAFETY_CAR, TRACKS

log = logging.getLogger("engineer.replay")

FRAME = struct.Struct("<dI")  # timestamp (double), payload length (uint32)


@dataclass
class ReplayMarker:
    time_s: float
    marker_type: str  # "overtake" | "pit" | "sc" | "vsc" | "incident"
    label: str
    lap: int = 0
    car_idx: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "time_s": round(self.time_s, 2),
            "marker_type": self.marker_type,
            "label": self.label,
            "description": self.label,
            "lap": self.lap,
            "car_idx": self.car_idx,
        }


@dataclass
class Keyframe:
    time_s: float
    offset: int


class ReplaySession:
    """Represents an open, indexed recorded session."""
    def __init__(self, path: str, stream: BinaryIO, size_bytes: int):
        self.path = path
        self.stream = stream
        self.size_bytes = size_bytes
        self.duration_s: float = 0.0
        self.track_id: int = 0
        self.track_name: str = "Unknown Track"
        self.session_type: str = "Race"
        self.keyframes: list[Keyframe] = []
        self.markers: list[ReplayMarker] = []
        self.total_packets: int = 0
        self.first_timestamp: float = 0.0

    def close(self):
        try:
            self.stream.close()
        except Exception:
            pass


class ReplayController:
    """Manages recording playback, scrubber seeking, speed multipliers, and broadcast synchronization."""
    def __init__(self, engineer: Any = None, broadcaster: Any = None):
        self.engineer = engineer
        self._broadcaster = broadcaster
        self.session: Optional[ReplaySession] = None
        self.playing: bool = False
        self.speed: float = 1.0
        self.current_time_s: float = 0.0
        self._playback_task: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    @property
    def is_loaded(self) -> bool:
        return self.session is not None

    def list_recordings(self, search_dirs: list[str | Path] | None = None) -> list[dict[str, Any]]:
        """Finds all available .bin and .bin.zst recordings."""
        if search_dirs is None:
            search_dirs = ["data/raw", "data", "."]

        found = []
        seen_paths = set()

        for d in search_dirs:
            p = Path(d)
            if not p.exists() or not p.is_dir():
                continue
            for ext in ("*.bin", "*.bin.zst"):
                for f in p.glob(ext):
                    abs_p = str(f.resolve())
                    if abs_p in seen_paths:
                        continue
                    seen_paths.add(abs_p)
                    stat = f.stat()
                    found.append({
                        "path": str(f),
                        "filename": f.name,
                        "size_bytes": stat.st_size,
                        "size_mb": round(stat.st_size / (1024 * 1024), 2),
                        "modified_at": stat.st_mtime,
                    })

        found.sort(key=lambda x: x["modified_at"], reverse=True)
        return found

    def load_recording(self, file_path_or_bytes: str | bytes | Path) -> dict[str, Any]:
        """Loads a session, decompresses if necessary, and constructs keyframe & marker index."""
        self.pause()
        if self.session:
            self.session.close()
            self.session = None

        if isinstance(file_path_or_bytes, bytes):
            stream = io.BytesIO(file_path_or_bytes)
            size = len(file_path_or_bytes)
            path_str = "in-memory-recording.bin"
        else:
            path_str = str(file_path_or_bytes)
            p = Path(path_str)
            if not p.exists():
                raise FileNotFoundError(f"Recording file not found: {path_str}")

            if path_str.endswith(".zst"):
                import zstandard as zstd
                dctx = zstd.ZstdDecompressor()
                with open(p, "rb") as fh:
                    decompressed = dctx.decompress(fh.read())
                stream = io.BytesIO(decompressed)
                size = len(decompressed)
            else:
                fh = open(p, "rb")
                stream = fh
                size = p.stat().st_size

        sess = ReplaySession(path=path_str, stream=stream, size_bytes=size)
        self._index_session(sess)
        self.session = sess
        self.current_time_s = 0.0

        # Seek to start
        self.seek(0.0)

        return self.get_status()

    def _index_session(self, sess: ReplaySession):
        """Single-pass index builder: extracts duration, keyframes, and markers."""
        f = sess.stream
        f.seek(0)
        first_t: float | None = None
        last_keyframe_t = -999.0
        last_sc_status = 0
        last_yellow_active = False
        prev_pit_statuses: dict[int, int] = {}

        pkt_count = 0
        last_t = 0.0

        while True:
            offset = f.tell()
            hdr = f.read(FRAME.size)
            if len(hdr) < FRAME.size:
                break
            t, length = FRAME.unpack(hdr)
            buf = f.read(length)
            if len(buf) < length:
                break

            if first_t is None:
                first_t = t
                sess.first_timestamp = first_t

            rel_t = max(0.0, t - first_t)
            last_t = rel_t
            pkt_count += 1

            # Keyframe index every ~1.5s
            if rel_t - last_keyframe_t >= 1.5 or not sess.keyframes:
                sess.keyframes.append(Keyframe(time_s=rel_t, offset=offset))
                last_keyframe_t = rel_t

            # Scan packet content for markers
            if len(buf) >= HEADER.size:
                try:
                    h = packets.parse_header(buf)
                    if not h:
                        continue
                    pid = h.packet_id
                    # 1. Session packet (Track & Safety Car & Incidents)
                    if pid == PacketId.SESSION:
                        sess_obj = packets.parse_session(buf, h)
                        if sess_obj:
                            sess.track_id = sess_obj.track_id
                            sess.track_name = TRACKS.get(sess_obj.track_id, f"Track {sess_obj.track_id}")
                            sc = sess_obj.safety_car
                            if sc != last_sc_status:
                                if sc == 1:
                                    sess.markers.append(ReplayMarker(rel_t, "sc", "Safety Car Deployed"))
                                elif sc == 2:
                                    sess.markers.append(ReplayMarker(rel_t, "vsc", "Virtual Safety Car"))
                                elif sc == 0 and last_sc_status in (1, 2):
                                    sess.markers.append(ReplayMarker(rel_t, "sc", "Safety Car Ending"))
                                last_sc_status = sc

                            # Check yellow flags in marshal zones
                            any_yellow = any(mz[1] == 3 for mz in getattr(sess_obj, "marshal_zones", []))
                            if any_yellow and not last_yellow_active:
                                sess.markers.append(ReplayMarker(rel_t, "incident", "Yellow Flag in Sector"))
                            last_yellow_active = any_yellow

                    # 2. Event packet (Overtakes, Penalties)
                    elif pid == PacketId.EVENT and len(buf) >= HEADER.size + 4:
                        code = buf[HEADER.size : HEADER.size + 4]
                        if code == b"OTKE":
                            if len(buf) >= HEADER.size + 6:
                                o_idx, b_idx = struct.unpack_from("<BB", buf, HEADER.size + 4)
                                sess.markers.append(ReplayMarker(rel_t, "overtake", f"Overtake: Car {o_idx} passed {b_idx}", car_idx=o_idx))
                            else:
                                sess.markers.append(ReplayMarker(rel_t, "overtake", "Overtake"))
                        elif code == b"PENA":
                            sess.markers.append(ReplayMarker(rel_t, "incident", "Penalty Issued"))
                        elif code in (b"COLL", b"SSTA"):
                            sess.markers.append(ReplayMarker(rel_t, "incident", "Incident"))

                    # 3. Lap data (Pit stops)
                    elif pid == PacketId.LAP_DATA:
                        laps = packets.parse_lap_data(buf, h)
                        if laps:
                            for c_idx, l_data in enumerate(laps):
                                cur_pit = l_data.pit_status
                                old_pit = prev_pit_statuses.get(c_idx, 0)
                                if cur_pit in (1, 2) and old_pit == 0:
                                    sess.markers.append(ReplayMarker(rel_t, "pit", f"Car P{l_data.position} Entered Pit", lap=l_data.lap_num, car_idx=c_idx))
                                prev_pit_statuses[c_idx] = cur_pit
                except Exception:
                    pass

        sess.duration_s = last_t
        sess.total_packets = pkt_count
        f.seek(0)
        log.info("Indexed replay session: %s, duration=%.1fs, keyframes=%d, markers=%d", sess.path, sess.duration_s, len(sess.keyframes), len(sess.markers))

    def seek(self, target_time_s: float):
        """Instantly seeks to target_time_s using the keyframe index."""
        if not self.session:
            return

        target_time_s = max(0.0, min(self.session.duration_s, target_time_s))
        self.current_time_s = target_time_s

        # Find closest keyframe <= target_time_s
        keyframes = self.session.keyframes
        best_kf = keyframes[0] if keyframes else Keyframe(0.0, 0)
        for kf in keyframes:
            if kf.time_s <= target_time_s:
                best_kf = kf
            else:
                break

        f = self.session.stream
        f.seek(best_kf.offset)

        # Fast forward to target_time_s
        first_t = self.session.first_timestamp
        while True:
            cur_pos = f.tell()
            hdr = f.read(FRAME.size)
            if len(hdr) < FRAME.size:
                break
            t, length = FRAME.unpack(hdr)
            buf = f.read(length)
            rel_t = max(0.0, t - first_t)

            if rel_t > target_time_s:
                # Rewind back to before this packet
                f.seek(cur_pos)
                break

            if self.engineer:
                self.engineer.on_packet(buf)

        # Trigger immediate positions frame broadcast if engineer is available
        self._broadcast_current_positions()

    def _broadcast_current_positions(self):
        if not self.engineer:
            return
        bcast = self._broadcaster or getattr(self.engineer, "positions_broadcaster", None)
        if not bcast:
            return

        from positions import encode_positions_frame, extract_car_positions
        track_id = self.session.track_id if self.session else 0
        state = self.engineer.state
        track_model = getattr(self.engineer, "track_model", None)
        track_outline = getattr(self.engineer, "track_map", None)
        outline = track_outline.current_outline if track_outline else None

        cars = extract_car_positions(state, track_model, outline)
        seq = bcast.next_seq()
        ts_ms = int(self.current_time_s * 1000) & 0xFFFFFFFF
        frame = encode_positions_frame(seq, ts_ms, track_id, cars)
        bcast.broadcast_frame(frame)

    def play(self, speed: float = 1.0):
        """Starts or resumes playback."""
        if not self.session:
            return
        self.speed = max(0.25, min(16.0, float(speed)))
        self.playing = True

        if self._playback_task is None or self._playback_task.done():
            try:
                loop = asyncio.get_running_loop()
                self._playback_task = loop.create_task(self._playback_loop())
            except RuntimeError:
                pass

    def pause(self):
        """Pauses playback."""
        self.playing = False
        if self._playback_task and not self._playback_task.done():
            self._playback_task.cancel()
            self._playback_task = None

    async def _playback_loop(self):
        """Background playback coroutine."""
        if not self.session:
            return
        f = self.session.stream
        first_t = self.session.first_timestamp

        last_pos_broadcast = 0.0

        try:
            while self.playing:
                hdr = f.read(FRAME.size)
                if len(hdr) < FRAME.size:
                    # End of file reached
                    self.playing = False
                    break

                t, length = FRAME.unpack(hdr)
                buf = f.read(length)
                if len(buf) < length:
                    self.playing = False
                    break

                pkt_time = max(0.0, t - first_t)
                time_diff = pkt_time - self.current_time_s

                if time_diff > 0:
                    delay = time_diff / self.speed
                    if delay > 0.001:
                        await asyncio.sleep(min(delay, 0.5))

                self.current_time_s = pkt_time

                if self.engineer:
                    self.engineer.on_packet(buf)

                # Broadcast position frame up to 20Hz
                wall_now = time.monotonic()
                if wall_now - last_pos_broadcast >= 0.045:
                    last_pos_broadcast = wall_now
                    self._broadcast_current_positions()

        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.exception("Error in replay playback loop: %s", e)
        finally:
            self.playing = False

    def get_status(self) -> dict[str, Any]:
        """Returns JSON status dictionary for frontend."""
        if not self.session:
            return {
                "loaded": False,
                "playing": False,
                "speed": self.speed,
                "current_time_s": 0.0,
                "duration_s": 0.0,
                "track_id": 0,
                "track_name": "No session loaded",
                "markers": [],
            }

        return {
            "loaded": True,
            "path": self.session.path,
            "filename": Path(self.session.path).name,
            "track_id": self.session.track_id,
            "track_name": self.session.track_name,
            "session_type": self.session.session_type,
            "duration_s": round(self.session.duration_s, 2),
            "current_time_s": round(self.current_time_s, 2),
            "playing": self.playing,
            "speed": self.speed,
            "total_keyframes": len(self.session.keyframes),
            "markers": [m.to_dict() for m in self.session.markers],
        }
