"""Race engineer runtime: UDP in -> state + callouts -> radio queue out.

    python main.py                              # listen on 127.0.0.1:20777
    python main.py --record session.bin         # also save raw packets for offline dev
    python main.py --replay session.bin --speed 4
    python main.py --redis redis://localhost:6379/0

Type a question and press Enter to test routing without a mic.
STT, TTS and the AI agent plug in at the three marked hooks.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import struct
import sys
import threading
import time

from f1telemetry import packets
from f1telemetry.deterministic import Callout, Callouts, RadioQueue, route
from f1telemetry.state import RaceState

log = logging.getLogger("engineer")
FRAME = struct.Struct("<dI")   # recording: seconds since start, payload length


class Recorder:
    def __init__(self, path):
        self.f = open(path, "wb")
        self.t0 = time.monotonic()

    def write(self, buf):
        self.f.write(FRAME.pack(time.monotonic() - self.t0, len(buf)) + buf)

    def close(self):
        self.f.close()


class Engineer:
    def __init__(self, ptt_action: int, recorder: Recorder | None = None):
        self.state = RaceState(ptt_action=ptt_action)
        self.callouts = Callouts()
        self.radio = RadioQueue()
        self.recorder = recorder

    def on_packet(self, buf: bytes):
        if self.recorder:
            self.recorder.write(buf)
        try:
            h, data = packets.parse(buf)
            if data is None:
                return
            for sig in self.state.update(h, data):
                if sig[0] == "ptt":
                    self.on_ptt(sig[1])
                for c in self.callouts.process(sig, self.state):
                    self.radio.push(c)
        except Exception:
            log.exception("packet handling failed")

    def on_ptt(self, down: bool):
        self.radio.ptt_down = down
        # HOOK 1: start recording on down; on up, transcribe with faster-whisper and call self.ask(text).
        log.info("push-to-talk %s", "DOWN" if down else "UP")

    def ask(self, text: str):
        reply = route(text, self.state)
        if reply.kind == "answer":
            self.say(reply.text)
        else:
            # HOOK 2: send text + json.dumps(self.state.snapshot()) to the AI agent, then self.say(result).
            log.info("-> AI agent: %r", text)
            self.say("Copy, stand by.")

    def say(self, text: str):
        self.radio.push(Callout(-1, time.monotonic(), text))

    async def speaker(self):
        while True:
            c = self.radio.pop()
            if c:
                # HOOK 3: replace with radio click + TTS playback (await it so messages don't overlap).
                print(f"[RADIO] {c.text}", flush=True)
            await asyncio.sleep(0.05)


class Listener(asyncio.DatagramProtocol):
    def __init__(self, on_packet):
        self.on_packet = on_packet

    def datagram_received(self, data, addr):
        self.on_packet(data)


def start_console(engineer: Engineer, loop):
    def run():
        for line in sys.stdin:
            if line.strip():
                loop.call_soon_threadsafe(engineer.ask, line.strip())
    threading.Thread(target=run, daemon=True).start()   # daemon so Ctrl+C exits cleanly


async def replay(path, engineer: Engineer, speed: float):
    start, first = time.monotonic(), None
    with open(path, "rb") as f:
        while hdr := f.read(FRAME.size):
            t, n = FRAME.unpack(hdr)
            buf = f.read(n)
            first = t if first is None else first
            delay = (t - first) / speed - (time.monotonic() - start)
            if delay > 0:
                await asyncio.sleep(delay)
            engineer.on_packet(buf)


async def publish(engineer: Engineer, url: str, hz: float):
    import redis.asyncio as redis
    client = redis.from_url(url)   # one client, reused for every write
    try:
        while True:
            await client.set("current_race_state", json.dumps(engineer.state.snapshot()))
            await asyncio.sleep(1 / hz)
    finally:
        await client.aclose()


async def amain(args):
    recorder = Recorder(args.record) if args.record else None
    engineer = Engineer(args.ptt_action, recorder)
    loop = asyncio.get_running_loop()
    tasks = [asyncio.create_task(engineer.speaker())]
    if args.redis:
        tasks.append(asyncio.create_task(publish(engineer, args.redis, args.redis_hz)))
    if not args.no_console:
        start_console(engineer, loop)
    try:
        if args.replay:
            await replay(args.replay, engineer, args.speed)
            await asyncio.sleep(1)
            print(json.dumps(engineer.state.snapshot(), indent=2))
        else:
            transport, _ = await loop.create_datagram_endpoint(
                lambda: Listener(engineer.on_packet), local_addr=(args.host, args.port))
            log.info("listening on %s:%d (PTT = UDP Action %d)", args.host, args.port, args.ptt_action)
            try:
                await asyncio.Event().wait()
            finally:
                transport.close()
    finally:
        for t in tasks:
            t.cancel()
        if recorder:
            recorder.close()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=20777)
    p.add_argument("--ptt-action", type=int, default=1, help="in-game 'UDP Action' number bound to your PTT button")
    p.add_argument("--record", help="save raw packets to this file")
    p.add_argument("--replay", help="replay a recorded file instead of listening")
    p.add_argument("--speed", type=float, default=1.0)
    p.add_argument("--redis", help="e.g. redis://localhost:6379/0 (optional)")
    p.add_argument("--redis-hz", type=float, default=10)
    p.add_argument("--no-console", action="store_true")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        asyncio.run(amain(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
