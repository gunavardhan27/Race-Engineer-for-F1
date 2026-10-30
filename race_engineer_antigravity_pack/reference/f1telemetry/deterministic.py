"""The deterministic half of the engineer: instant answers and proactive callouts.

Anything that is a lookup or a threshold lives here, so the numbers always come from
telemetry. Open-ended questions (handling, setup, strategy) are routed to the AI agent.
"""
from __future__ import annotations

import heapq
import re
import time
from dataclasses import dataclass, field

from .state import COMPOUNDS, TYRES, WEATHER, RaceState

# Words that mean the driver wants judgment, not a number -> AI agent.
_AI = re.compile(
    r"\b(understeer\w*|oversteer\w*|wash\w*|snap\w*|loose|lock\w*|cook\w*|overheat\w*|setup|balance|"
    r"diff|bias|strategy|should|undercut|overcut|plan|why|what if|advice|help)\b", re.I)

_INTENTS = [(name, re.compile(p, re.I)) for name, p in [
    ("radio_check", r"\bradio check\b|\bcomms? check\b|\bcan you hear me\b|\bdo you (copy|read)\b|\bhow do you read\b"),
    ("pit_window", r"\bpit window\b|\bwindow\b|when (do|can) (i|we) (pit|box)"),
    ("weather", r"\b(rain|weather|forecast|wet|dry)\b"),
    ("fuel", r"\bfuel\b"),
    ("damage", r"\bdamage\w*\b"),
    ("tyres", r"\b(tyres?|tires?|wear|deg)\b"),
    ("laps", r"\blaps?\b.*\b(left|remaining|to go)\b|\bhow many laps\b"),
    ("position", r"\b(position|where am i|what p)\b"),
    ("gap_behind", r"\b(behind|gap back)\b"),
    ("gap_ahead", r"\b(ahead|in front|gap)\b"),
]]

NO_DATA = "No data on that yet."


def sec(ms):
    return f"{ms / 1000:.1f}"


def _trend(t, closing: str, opening: str):
    if t is None:
        return ""
    if abs(t) < 50:
        return " Holding steady."
    return " " + (closing if t < 0 else opening).format(f"{abs(t) / 1000:.1f}")


def _gap_ahead(st: RaceState):
    idx, ms = st.gap_ahead()
    if st.me and st.me.position == 1:
        return "You're leading. " + _gap_behind(st)
    if idx is None:
        return NO_DATA
    return f"{st.name(idx)} ahead, {sec(ms)}." + _trend(st.gap_trend("ahead"), "Gaining {} a lap.", "Losing {} a lap.")


def _gap_behind(st: RaceState):
    idx, ms = st.gap_behind()
    if idx is None:
        return "Nobody behind." if st.me else NO_DATA
    return f"{st.name(idx)} behind, {sec(ms)}." + _trend(
        st.gap_trend("behind"), "He's closing {} a lap.", "You're pulling {} a lap.")


def _fuel(st: RaceState):
    s = st.my_status
    if not s:
        return NO_DATA
    d = s.fuel_laps_delta
    text = f"Fuel {'plus' if d >= 0 else 'minus'} {abs(d):.1f}."
    if d < -0.2:
        text += " Start lifting and coasting."
    burn = st.fuel_per_lap()
    return text + (f" Using {burn:.1f} kilos a lap." if burn else "")


def _tyres(st: RaceState):
    s, d = st.my_status, st.my_damage
    if not s or not d:
        return NO_DATA
    worst = max(d.tyre_wear)
    corner = TYRES[d.tyre_wear.index(worst)]
    text = f"{COMPOUNDS.get(s.visual_compound, 'Tyres').capitalize()}, {s.tyre_age_laps} laps old. " \
           f"Worst is {corner}, {worst:.0f} percent wear."
    rate = st.wear_per_lap()
    return text + (f" About {rate:.1f} percent a lap." if rate else "")


def _damage(st: RaceState):
    if not st.my_damage:
        return NO_DATA
    parts = st.damage_parts()
    if not parts:
        return "No damage showing."
    return " ".join(f"{k.capitalize()} {v} percent." for k, v in sorted(parts.items(), key=lambda kv: -kv[1])[:3])


def _weather(st: RaceState):
    s = st.session
    if not s:
        return NO_DATA
    text = f"{WEATHER.get(s.weather, 'Unknown').capitalize()}, track {s.track_temp} degrees."
    rain = st.next_rain()
    return text + (f" {rain.rain_pct} percent rain in {rain.minutes_ahead} minutes." if rain
                   else " No rain in the forecast.")


def _position(st: RaceState):
    me, s = st.me, st.session
    if not me:
        return NO_DATA
    return f"P{me.position}" + (f", lap {me.lap_num} of {s.total_laps}." if s and s.total_laps else ".")


def _laps(st: RaceState):
    me, s = st.me, st.session
    if not me or not s or not s.total_laps:
        return NO_DATA
    return f"Lap {me.lap_num} of {s.total_laps}, {s.total_laps - me.lap_num} after this one."


def _pit_window(st: RaceState):
    s = st.session
    if not s or not s.pit_ideal_lap:
        return "No pit window showing."
    return f"Window is lap {s.pit_ideal_lap} to {s.pit_latest_lap}, rejoin around P{s.pit_rejoin_pos}."


def _radio_check(st: RaceState):
    if st.me is None:
        return "Loud and clear. No telemetry yet, check the game's UDP settings."
    return "Loud and clear. Telemetry's live."


ANSWERS = {"radio_check": _radio_check, "gap_ahead": _gap_ahead, "gap_behind": _gap_behind, "fuel": _fuel, "tyres": _tyres,
           "damage": _damage, "weather": _weather, "position": _position, "laps": _laps,
           "pit_window": _pit_window}


@dataclass(slots=True)
class Reply:
    kind: str               # "answer" or "ai"
    intent: str | None = None
    text: str | None = None


def route(text: str, st: RaceState) -> Reply:
    """Answer from telemetry if it's a lookup; otherwise hand off to the AI agent."""
    if _AI.search(text):
        return Reply("ai")
    for intent, pat in _INTENTS:
        if pat.search(text):
            return Reply("answer", intent, ANSWERS[intent](st))
    return Reply("ai")


# ---- proactive callouts ----------------------------------------------------
@dataclass(order=True)
class Callout:
    priority: int                     # 0 critical .. 3 nice-to-know; -1 = reply to the driver
    created: float
    text: str = field(compare=False)
    ttl: float = field(compare=False, default=20.0)


def _c(priority, text, ttl=20.0):
    return Callout(priority, time.monotonic(), text, ttl)


class Callouts:
    def __init__(self):
        self._last: dict = {}      # cooldown key -> lap last fired
        self._fired: set = set()   # one-shot keys
        self._prev_damage = None

    def _due(self, key, lap, every):
        last = self._last.get(key)
        if last is not None and lap - last < every:
            return False
        self._last[key] = lap
        return True

    def _once(self, key):
        if key in self._fired:
            return False
        self._fired.add(key)
        return True

    def process(self, sig, st: RaceState) -> list[Callout]:
        kind = sig[0]
        if kind == "new_session":
            self.__init__()
        elif kind == "lap":
            return self._on_lap(sig[1], st)
        elif kind == "damage":
            return self._on_damage(st)
        elif kind == "event":
            return self._on_event(sig[1], st)
        return []

    def _on_lap(self, rec, st: RaceState):
        out, lap = [], rec.lap
        prev = st.laps[-2] if len(st.laps) > 1 else None

        if rec.gap_behind_ms is not None and rec.gap_behind_ms < 1000:
            was_close = (prev and prev.behind_idx == rec.behind_idx and prev.gap_behind_ms is not None
                         and prev.gap_behind_ms < 1000)
            if not was_close:
                out.append(_c(1, f"{st.name(rec.behind_idx)} is within a second, {sec(rec.gap_behind_ms)} behind."))

        t = st.gap_trend("ahead")
        if t is not None and t <= -300 and self._due("chase", lap, 3):
            out.append(_c(3, f"Gaining {abs(t) / 1000:.1f} a lap on {st.name(rec.ahead_idx)}, "
                             f"gap {sec(rec.gap_ahead_ms)}.", ttl=15))

        s = st.my_status
        if s and s.fuel_laps_delta <= -0.2 and self._due("fuel", lap, 3):
            out.append(_c(2, f"Fuel's minus {abs(s.fuel_laps_delta):.1f}. Lift and coast where you can."))

        if rec.tyre_wear:
            worst = max(rec.tyre_wear)
            for threshold in (75, 65, 50):
                if worst >= threshold:
                    if self._once(("wear", rec.stint, threshold)):
                        for lower in (50, 65, 75):
                            if lower < threshold:
                                self._fired.add(("wear", rec.stint, lower))
                        corner = TYRES[rec.tyre_wear.index(worst)]
                        out.append(_c(2, f"Tyre wear {worst:.0f} percent, {corner} is the worst."))
                    break

        sess, current = st.session, lap + 1
        if sess and sess.pit_ideal_lap:
            if current == sess.pit_ideal_lap and self._once(("window", sess.pit_ideal_lap)):
                out.append(_c(1, f"Pit window's open. Latest is lap {sess.pit_latest_lap}."))
            if current == sess.pit_latest_lap and self._once(("window_end", sess.pit_latest_lap)):
                out.append(_c(1, "Pit window closes this lap."))

        rain = st.next_rain(40)
        if rain and rain.minutes_ahead <= 15 and self._due("rain", lap, 4):
            out.append(_c(1, f"Rain coming, {rain.rain_pct} percent in {rain.minutes_ahead} minutes."))
        return out

    def _on_damage(self, st: RaceState):
        d = st.my_damage
        if d is None:
            return []
        parts = {"front left wing": d.front_left_wing, "front right wing": d.front_right_wing,
                 "rear wing": d.rear_wing, "floor": d.floor, "diffuser": d.diffuser, "sidepod": d.sidepod}
        prev, self._prev_damage = self._prev_damage, parts
        if prev is None:
            return []
        return [_c(0, f"Damage, {k} {v} percent.", ttl=30) for k, v in parts.items() if v - prev[k] >= 10]

    def _on_event(self, ev, st: RaceState):
        if ev.code == "SCAR":
            msgs = {(1, 0): "Safety car, safety car.", (1, 1): "Safety car in this lap.",
                    (2, 0): "VSC, VSC.", (2, 1): "VSC ending, be ready."}
            text = msgs.get((ev.sc_type, ev.sc_event))
            return [_c(0, text, ttl=10)] if text else []
        if ev.code == "FTLP" and ev.vehicle_idx == st.player:
            return [_c(3, "That's the fastest lap.", ttl=15)]
        return []


class RadioQueue:
    """Priority queue of things to say. Holds while push-to-talk is down; drops stale items."""

    def __init__(self):
        self._heap: list[Callout] = []
        self.ptt_down = False

    def push(self, c: Callout):
        heapq.heappush(self._heap, c)

    def pop(self) -> Callout | None:
        if self.ptt_down:
            return None
        now = time.monotonic()
        while self._heap:
            c = heapq.heappop(self._heap)
            if now - c.created <= c.ttl:
                return c
        return None
