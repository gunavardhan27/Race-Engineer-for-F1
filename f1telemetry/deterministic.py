"""The deterministic half of the engineer: instant answers and proactive callouts.

Anything that is a lookup or a threshold lives here, so the numbers always come from
telemetry. Open-ended questions (handling, setup, strategy) are routed to the AI agent.
"""
from __future__ import annotations

import heapq
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

log = logging.getLogger("engineer.deterministic")
log_detectors = logging.getLogger("engineer.detectors")

from config import DebriefConfig
from . import packets
from .state import COMPOUNDS, TYRES, WEATHER, RaceState

# Words that mean the driver wants judgment, not a number -> AI agent.
_AI = re.compile(
    r"\b(understeer\w*|oversteer\w*|wash\w*|snap\w*|loose|lock\w*|cook\w*|overheat\w*|balance|"
    r"diff|bias|strategy|should|undercut|overcut|plan|why|what if|advice|help)\b", re.I)

_INTENTS = [(name, re.compile(p, re.I)) for name, p in [
    ("pit_window", r"\bpit window\b|\bwindow\b|when (do|can) (i|we) (pit|box)"),
    ("weather", r"\b(rain|weather|forecast|wet|dry)\b"),
    ("fuel", r"\bfuel\b"),
    ("damage", r"\bdamage\w*\b"),
    ("tyres", r"\b(tyres?|tires?|wear|deg)\b"),
    ("laps", r"\blaps?\b.*\b(left|remaining|to go)\b|\bhow many laps\b"),
    ("position", r"\b(position|where am i|what p)\b"),
    ("gap_behind", r"\b(behind|gap back)\b"),
    ("gap_to_pole", r"\b(gap to pole|how far off pole|gap to p1|time to pole)\b"),
    ("gap_ahead", r"\b(ahead|in front|gap)\b"),
    ("am_i_through", r"\b(am i through|are we through|am i safe|are we safe|in the drop zone|eliminated)\b"),
    ("teammate", r"\b(where('s| is) my teammate|where('s| is) (the )?other car|how am i doing vs teammate|how('s| is) my teammate doing|teammate pace|vs teammate)\b"),
    ("car_capability", r"\b(what('s| is) the car capable of|what can the car do|what should we be doing|car potential|expected position)\b"),
    ("delta_best", r"\b(where am i losing time|where am i slow|losing time|how was my (last )?lap|pace vs best|delta to best)\b"),
    ("setup", r"\b(what('s| is)? (my )?setup|what setup am i (on|running)|current setup|best setup( here)?|setup info)\b"),
    ("radio_check", r"\b(radio check|comms check|can you hear me|do you copy)\b"),
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


def _delta_best(st: RaceState):
    comp = st.last_lap_vs_best
    pb = st.personal_best
    if not comp or comp.get("delta_s") is None:
        if pb and pb.get("lap_time_s"):
            return f"Your personal best is {pb['lap_time_s']:.2f}."
        return "No reference lap recorded yet."
    delta = comp["delta_s"]
    sign_str = f"{abs(delta):.1f} slower" if delta > 0 else f"{abs(delta):.1f} faster"
    msg = f"Last lap was {sign_str} than your best."
    wc = comp.get("worst_corner")
    if wc and wc.get("name"):
        c_name = wc["name"]
        c_delta = wc.get("delta_s", 0.0)
        c_spd = wc.get("speed_diff_kph", 0.0)
        if c_delta > 0.05:
            msg += f" Lost {c_delta:.1f} in {c_name}"
            if c_spd < -2.0:
                msg += f", minimum speed was {abs(c_spd):.0f} kph down."
            else:
                msg += "."
    return msg


def _setup(st: RaceState, text: str = "") -> str:
    my = st.my_setup
    best = getattr(st, "best_setup_info", None)
    is_best_query = bool(re.search(r"\bbest\b", text, re.I))

    if is_best_query:
        if best:
            lap_s = best.get("best_lap_s")
            lap_text = f", best lap {lap_s:.2f}" if lap_s else ""
            p = best.get("params", {})
            fw = p.get("front_wing", "--")
            rw = p.get("rear_wing", "--")
            return f"Best setup here is {best.get('name', 'optimal')}{lap_text}. Wings {fw} front, {rw} rear."
        return "No ranked setups for this track yet."

    if my is not None:
        return f"You're on {my.front_wing} front wing, {my.rear_wing} rear wing, brake bias {my.brake_bias} percent, on-throttle diff {my.on_throttle} percent."
    elif best:
        return f"Best setup here is {best.get('name', 'optimal')}. Wings {best.get('params', {}).get('front_wing', '--')} front, {best.get('params', {}).get('rear_wing', '--')} rear."
    return "No setup data on the car yet."


def _radio_check(st: RaceState, text: str = "") -> str:
    is_live = (st.last_packet_time > 0 and (time.monotonic() - st.last_packet_time) < 4.0)
    if is_live:
        return "Loud and clear. Telemetry's live."
    return "Loud and clear. No telemetry yet."


def _gap_to_pole(st: RaceState):
    gap = st.gap_to_pole_s()
    if gap is None:
        return "No pole time set yet."
    if gap <= 0.001:
        return "You're on pole."
    return f"Gap to pole is {gap:.1f}."


def _am_i_through(st: RaceState):
    pos = st.quali_position()
    cutoff = st.elimination_cutoff()
    if pos is None:
        return NO_DATA
    if cutoff is None:
        return f"You're P{pos}. No elimination in this session."
    status = "Safe" if pos <= cutoff else "Danger"
    return f"You're P{pos}, cutoff is P{cutoff}. {status}."


def _teammate(st: RaceState):
    t_idx = st.teammate_idx()
    if t_idx is None:
        return "No teammate in this session."
    t_name = st.teammate_name()
    t_pos = st.lap[t_idx].position if st.lap and t_idx < len(st.lap) else None
    t_best = st.get_car_best_lap_s(t_idx)
    my_best = st.get_car_best_lap_s(st.player)
    msg = f"{t_name} is P{t_pos}." if t_pos else f"{t_name}."
    if my_best and t_best:
        diff = my_best - t_best
        if abs(diff) < 0.05:
            msg += " You're matching his lap times."
        elif diff < 0:
            msg += f" You're {abs(diff):.1f} quicker."
        else:
            msg += f" He's {diff:.1f} quicker."
    elif t_best:
        msg += f" His best is {t_best:.2f}."
    return msg


def _car_capability(st: RaceState):
    t_idx = st.teammate_idx()
    if t_idx is not None:
        t_best = st.get_car_best_lap_s(t_idx)
        if t_best:
            t_name = st.teammate_name()
            return f"{t_name} did a {t_best:.2f}. That's what the car can do."
    exp = getattr(st, "expected_performance", None)
    if exp and exp.get("position") is not None and exp.get("basis") != "none":
        return f"Based on {exp['basis']} data, the car is capable of P{exp['position']}."
    return "Not enough data yet."


ANSWERS = {"gap_ahead": _gap_ahead, "gap_behind": _gap_behind, "fuel": _fuel, "tyres": _tyres,
           "damage": _damage, "weather": _weather, "position": _position, "laps": _laps,
           "pit_window": _pit_window, "delta_best": _delta_best, "setup": _setup,
           "radio_check": _radio_check, "gap_to_pole": _gap_to_pole, "am_i_through": _am_i_through,
           "teammate": _teammate, "car_capability": _car_capability}


@dataclass(slots=True)
class Reply:
    kind: str               # "answer" or "ai"
    intent: str | None = None
    text: str | None = None


def route(text: str, st: RaceState) -> Reply:
    """Answer from telemetry if it's a lookup; otherwise hand off to the AI agent."""
    for intent, pat in _INTENTS:
        if pat.search(text):
            ans = ANSWERS[intent](st, text) if intent == "setup" else ANSWERS[intent](st)
            return Reply("answer", intent, ans)
    if _AI.search(text):
        return Reply("ai")
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


@dataclass
class PendingDebrief:
    lap_num: int
    created_at: float
    text: str


class Callouts:
    def __init__(self, debrief_config: Optional[DebriefConfig] = None):
        self.debrief_config = debrief_config
        self._last: dict = {}      # cooldown key -> lap last fired
        self._fired: set = set()   # one-shot keys
        self._prev_damage = None
        self._last_warning_time: float = 0.0
        self._last_warning_num: int = 0
        self._last_invalidation_time: float = 0.0
        self._last_invalidation_combined: bool = False
        self.pending_debrief: Optional[PendingDebrief] = None
        self.straight_start_time: Optional[float] = None

    def _format_warning(self, n: int) -> str:
        limit = self.debrief_config.track_limits_penalty_at if self.debrief_config else None
        if limit is not None:
            return f"{n} of {limit}"
        return f"number {n}"

    def queue_debrief(self, text: str, lap_num: int, now: Optional[float] = None) -> None:
        """Queues a debrief to be released on a straight or after 15s. Overwrites older pending debriefs."""
        if not text:
            return
        if now is None:
            now = time.monotonic()
        self.pending_debrief = PendingDebrief(lap_num=lap_num, created_at=now, text=text)
        self.straight_start_time = None

    def check_debrief_release(self, st: RaceState, now: Optional[float] = None) -> list[Callout]:
        """Releases queued debrief when speed > 200 kph and |steer| < 0.1 for 1s, or after 15s."""
        if self.pending_debrief is None:
            return []
        if now is None:
            now = time.monotonic()

        elapsed = now - self.pending_debrief.created_at
        if elapsed >= 15.0:
            text = self.pending_debrief.text
            self.pending_debrief = None
            self.straight_start_time = None
            return [_c(2, text, ttl=15.0)]

        p_tel = st.my_telemetry
        if p_tel is not None:
            speed = p_tel.speed_kph
            steer = p_tel.steer
            if speed > 200.0 and abs(steer) < 0.1:
                if self.straight_start_time is None:
                    self.straight_start_time = now
                elif now - self.straight_start_time >= 1.0:
                    text = self.pending_debrief.text
                    self.pending_debrief = None
                    self.straight_start_time = None
                    return [_c(2, text, ttl=15.0)]
            else:
                self.straight_start_time = None
        return []

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

    def process(self, sig, st: RaceState, now: float | None = None) -> list[Callout]:
        kind = sig[0]
        if now is None:
            now = time.monotonic()
        if kind == "new_session":
            self.__init__(self.debrief_config)
        elif kind == "lap":
            # Drop older pending debrief if a newer lap completes
            self.pending_debrief = None
            self.straight_start_time = None
            return self._on_lap(sig[1], st)
        elif kind == "damage":
            return self._on_damage(st)
        elif kind == "event":
            return self._on_event(sig[1], st)
        elif kind == "lap_invalidated":
            if now - self._last_invalidation_time <= 1.0:
                return []
            return self._on_lap_invalidated(now)
        elif kind == "track_limits_warning":
            if now - self._last_warning_time <= 1.0:
                return []
            return self._on_track_limits_warning(sig[1], now)
        elif kind == "penalty":
            return self._on_penalty(sig[1], st, now)
        elif kind == "defend_warning":
            return self._on_defend_warning(sig[1])
        elif kind == "flashback":
            if self.pending_debrief and self.pending_debrief.lap_num > sig[1]:
                self.pending_debrief = None
                self.straight_start_time = None
        return []

    def _on_defend_warning(self, info: dict[str, Any]) -> list[Callout]:
        corner = info.get("corner", "corner")
        if info.get("inside_look"):
            return [_c(1, f"Inside look into {corner}.", ttl=1.5)]
        return [_c(1, f"Defend into {corner}.", ttl=1.5)]

    def _on_lap_invalidated(self, now: float) -> list[Callout]:
        self._last_invalidation_time = now
        # If warning occurred within 1 s, speak combined message
        if now - self._last_warning_time <= 1.0:
            self._last_invalidation_combined = True
            msg = f"Track limits, lap invalidated. Warning {self._format_warning(self._last_warning_num)}."
            return [_c(0, msg, ttl=5.0)]
        self._last_invalidation_combined = False
        return [_c(1, "Lap invalidated.", ttl=5.0)]

    def _on_track_limits_warning(self, n: int, now: float) -> list[Callout]:
        self._last_warning_time = now
        self._last_warning_num = n
        # If invalidation occurred within 1 s, speak combined message
        if now - self._last_invalidation_time <= 1.0:
            self._last_invalidation_combined = True
            msg = f"Track limits, lap invalidated. Warning {self._format_warning(n)}."
            return [_c(0, msg, ttl=5.0)]
        return [_c(1, f"Track limits warning, {self._format_warning(n)}.", ttl=5.0)]

    def _on_penalty(self, ev: packets.Event, state: RaceState, now: float) -> list[Callout]:
        ptype = ev.penalty_type
        sec = ev.penalty_time if ev.penalty_time is not None else 255
        raw_b = getattr(ev, "raw_bytes", b"")

        pname = packets.PENALTY_TYPES.get(ptype, f"Unknown({ptype})")
        iname = packets.INFRINGEMENT_TYPES.get(ev.infringement_type, f"Unknown({ev.infringement_type})")
        log_detectors.info(
            "PENA event: type=%s (%s), infringement=%s (%s), car=%s, other=%s, time=%s, lap=%s, raw=%s",
            ptype, pname, ev.infringement_type, iname, ev.vehicle_idx, ev.other_vehicle_idx,
            sec, ev.lap_num, raw_b.hex() if raw_b else None
        )

        is_tt = (
            getattr(state, "session_type", None) == 13
            or getattr(state, "is_time_trial", False)
            or (getattr(state, "session", None) and getattr(state.session, "session_type", None) == 13)
            or getattr(state, "time_trial", None) is not None
        )
        # Time Trial: never speak penalty calls; only "Lap invalidated" (once per lap)
        if is_tt:
            if ptype in (10, 11, 12, 13, 14, 15):
                if now - self._last_invalidation_time <= 1.0:
                    return []
                cur_lap = state.me.lap_num if state.me else 0
                if getattr(self, "_last_tt_invalidation_lap", None) == cur_lap:
                    return []
                self._last_tt_invalidation_lap = cur_lap
                return self._on_lap_invalidated(now)
            return []

        # If warning (ptype == 5): handled by track-limits warning logic (no "penalty" wording)
        if ptype == 5:
            if now - self._last_warning_time <= 1.0:
                return []
            warning_num = (state.me.corner_cutting_warnings if state.me else 1) or 1
            return self._on_track_limits_warning(warning_num, now)

        # If lap invalidated (ptype in 10..15): handled by lap-invalidated logic, merged with warning call, never as a penalty
        if ptype in (10, 11, 12, 13, 14, 15):
            if now - self._last_invalidation_time <= 1.0:
                return []
            return self._on_lap_invalidated(now)

        # Real penalties:
        # Check time anomaly: time == 255 is N/A. Any time value > 60 s is an anomaly
        if sec == 255:
            pass  # N/A
        elif sec > 60:
            log_detectors.warning("Anomaly penalty time %d > 60: raw=%s; not speaking", sec, raw_b.hex() if raw_b else None)
            return []

        if ptype == 0:
            return [_c(0, "Drive-through penalty.", ttl=15.0)]
        elif ptype == 1:
            if 1 <= sec <= 60:
                return [_c(0, f"{sec}-second stop-and-go penalty.", ttl=15.0)]
            return [_c(0, "Stop-go penalty.", ttl=15.0)]
        elif ptype == 4:
            if 1 <= sec <= 60:
                return [_c(0, f"{sec}-second time penalty.", ttl=15.0)]
            else:
                return []

        # Anything else: log it, don't speak it unless in explicit allow-list
        log_detectors.info("PENA unhandled/not spoken penalty type %s (%s)", ptype, pname)
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
