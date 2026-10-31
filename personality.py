"""Personality and reaction system for F1 race engineer.

Loads lines from lines/<persona>.yaml, manages shuffle bags with profanity filtering,
tracks blunders and escalation, spin heuristics, and triggers deterministic reactions.
"""
from __future__ import annotations

import collections
import hashlib
import logging
import math
import os
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import yaml

from config import PersonaConfig
from f1telemetry import packets
from f1telemetry.deterministic import Callout, _c
from f1telemetry.state import RaceState, F1_RACE_POINTS

log = logging.getLogger("engineer.personality")

BIG_MOMENTS = {
    "win", "podium", "points", "no_points",
    "practice_finish", "quali_pole", "quali_finish",
    "retirement", "beat_teammate", "lost_to_teammate",
}
BLUNDER_EVENTS = {"spin", "collision", "flashback", "penalty", "lost_positions"}

BASE_COOLDOWNS = {
    "overtake_made": 20.0,
    "overtaken": 20.0,
    "defended": 40.0,
    "good_start": 999.0,
    "bad_start": 999.0,
    "fastest_lap": 45.0,
    "personal_best": 30.0,
    "win": 0.0,
    "podium": 0.0,
    "points": 0.0,
    "no_points": 0.0,
    "practice_finish": 0.0,
    "quali_pole": 0.0,
    "quali_finish": 0.0,
    "spin": 30.0,
    "collision": 20.0,
    "flashback": 20.0,
    "track_limits": 30.0,
    "penalty": 20.0,
    "lost_positions": 30.0,
    "escalation": 30.0,
    "clean_stint": 120.0,
    "retirement": 0.0,
    "radio_check": 10.0,
    # Pace and expectation cooldowns
    "slower_lap": 30.0,
    "slower_lap_explained": 30.0,
    "quali_gap_large": 30.0,
    "quali_danger_zone": 20.0,
    "quali_safe": 30.0,
    "purple_sector": 15.0,
    "below_expectation": 60.0,
    "above_expectation": 60.0,
    "beat_teammate": 0.0,
    "lost_to_teammate": 0.0,
    "tt_personal_best": 20.0,
    "tt_slower": 20.0,
    "ai_too_easy": 999.0,
    "ai_too_hard": 999.0,
    # Finish and overtakes cooldowns
    "win_photo_finish": 0.0,
    "win_close": 0.0,
    "win_comfortable": 0.0,
    "win_dominant": 0.0,
    "podium_held": 0.0,
    "podium_missed_higher": 0.0,
    "missed_podium": 0.0,
    "passed_stricken_car": 10.0,
    "overtake_clean": 10.0,
    "overtake_brave": 10.0,
    "overtake_divebomb": 10.0,
    "divebomb_contact": 10.0,
    "overtake_switchback": 5.0,
    "defend_warning": 20.0,
    "inside_look": 20.0,
    "told_you": 0.0,
}

FREQ_MULTIPLIERS = {
    "low": 1.5,
    "normal": 1.0,
    "high": 0.6,
}


@dataclass
class PersonaLine:
    text: str
    level: str = "none"   # "none", "mild", "strong"

    @property
    def has_placeholders(self) -> bool:
        return bool(re.search(r"\{[a-zA-Z_]+\}", self.text))


def load_persona_lines(persona_name: str, lines_dir: str = "lines") -> Dict[str, List[PersonaLine]]:
    """Loads and normalizes persona lines from YAML file."""
    if persona_name == "professional":
        return {}

    path = Path(lines_dir) / f"{persona_name}.yaml"
    if not path.is_file():
        log.warning("Persona lines file '%s' not found; personality disabled", path)
        return {}

    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except Exception as e:
        log.error("Failed to parse persona file '%s': %s", path, e)
        return {}

    out: Dict[str, List[PersonaLine]] = {}
    for event_name, entries in raw.items():
        if not isinstance(entries, list):
            continue
        line_objs = []
        for item in entries:
            if isinstance(item, str):
                line_objs.append(PersonaLine(text=item.strip(), level="none"))
            elif isinstance(item, dict) and "text" in item:
                level = str(item.get("level", "none")).lower()
                line_objs.append(PersonaLine(text=str(item["text"]).strip(), level=level))
        if line_objs:
            out[event_name] = line_objs
    return out


class PersonalityEngine:
    def __init__(
        self,
        config: Optional[PersonaConfig] = None,
        lines_dir: str = "lines",
        cache_dir: str = "cache/audio",
    ):
        self.config = config or PersonaConfig()
        self.lines_dir = lines_dir
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        self.lines: Dict[str, List[PersonaLine]] = {}
        self.bags: Dict[str, collections.deque[PersonaLine]] = {}
        self.last_fired_per_event: Dict[str, float] = {}
        self.last_reaction_time: float = -999.0

        # Defense tracking: consecutive laps car behind is < 1.0s
        self._defense_behind_idx: Optional[int] = None
        self._defense_consecutive_laps: int = 0

        # Blunder history: list of (lap_num, blunder_event_name)
        self.blunders: List[Tuple[int, str]] = []
        self._last_clean_stint_lap: int = 0

        # Spin heuristic tracker
        self._spin_entry_time: Optional[float] = None

        # Finish detection tracking
        self._finish_spoken_session_uid: Optional[int | str] = None
        self._finish_line_pos: Optional[int] = None
        self._finish_time: float = -999.0

        # Divebomb tracking for told_you reaction
        self._last_divebomb_time: float = -999.0
        self._last_divebomb_driver: str = "he"

        # Pre-rendered audio cache: md5(text+voice) -> audio ndarray, sample_rate
        self.prerendered_audio: Dict[str, Tuple[Any, int]] = {}

        self.reload_persona(self.config.name)

    def reload_persona(self, persona_name: str) -> None:
        """Loads persona lines and resets shuffle bags."""
        self.config.name = persona_name
        self.lines = load_persona_lines(persona_name, self.lines_dir)
        self.bags.clear()
        self.last_fired_per_event.clear()
        self.last_reaction_time = -999.0
        self._finish_spoken_session_uid = None
        self._finish_line_pos = None
        self._finish_time = -999.0
        self._last_divebomb_time = -999.0

    def _filter_lines_by_profanity(self, raw_lines: List[PersonaLine]) -> List[PersonaLine]:
        prof = self.config.profanity.lower()
        allowed_levels = {"none"}
        if prof in ("mild", "strong"):
            allowed_levels.add("mild")
        if prof == "strong":
            allowed_levels.add("strong")

        return [line for line in raw_lines if line.level in allowed_levels]

    def _get_next_line(self, event_name: str, context: Dict[str, Any]) -> Optional[str]:
        """Picks a non-repeating line from the shuffle-bag that can satisfy all placeholders."""
        all_lines = self.lines.get(event_name, [])
        valid_pool = self._filter_lines_by_profanity(all_lines)
        if not valid_pool:
            return None

        bag = self.bags.get(event_name)
        if not bag:
            shuffled = list(valid_pool)
            random.shuffle(shuffled)
            bag = collections.deque(shuffled)
            self.bags[event_name] = bag

        # Try lines in the bag until one matches context placeholders
        attempts = len(bag)
        for _ in range(attempts):
            candidate = bag.popleft()
            text = candidate.text
            placeholders = re.findall(r"\{([a-zA-Z_]+)\}", text)
            missing = [p for p in placeholders if p not in context or context[p] is None]

            if not missing:
                # Format text with context
                try:
                    formatted = text.format(**context)
                    return formatted
                except Exception:
                    continue
            else:
                # Cannot satisfy placeholders; put at back of bag and try next
                bag.append(candidate)

        return None

    def _is_on_cooldown(self, event_name: str, now: float) -> bool:
        if event_name in BIG_MOMENTS:
            return False

        # Check global gap (only if a reaction has fired previously)
        if self.last_reaction_time is not None and self.last_reaction_time > 0:
            if now - self.last_reaction_time < self.config.min_gap_s:
                return True

        # Check per-event cooldown (only if event has fired previously)
        if event_name not in self.last_fired_per_event:
            return False

        base_cd = BASE_COOLDOWNS.get(event_name, 20.0)
        mult = FREQ_MULTIPLIERS.get(self.config.reaction_frequency, 1.0)
        eff_cd = base_cd * mult

        last = self.last_fired_per_event[event_name]
        return (now - last) < eff_cd

    def record_blunder(self, lap_num: int, blunder_type: str) -> None:
        """Records a driver blunder for escalation and clean stint tracking."""
        self.blunders.append((lap_num, blunder_type))

    def get_recent_blunders(self, current_lap: int, window_laps: int = 5) -> List[Tuple[int, str]]:
        """Returns blunders within the last window_laps."""
        return [b for b in self.blunders if 0 <= (current_lap - b[0]) <= window_laps]

    # ---- Trigger Processors ----

    def check_spin(self, state: RaceState, now: Optional[float] = None) -> bool:
        """Spin heuristic from Motion packet data.

        Angle between forward direction and velocity > spin_angle_deg (60°) at speed > 50 kph,
        followed by speed < 30 kph within spin_window_s (3.0s).
        """
        if state.motion is None or state.player >= len(state.motion):
            return False
        mot = state.motion[state.player]
        if mot is None:
            return False

        if now is None:
            now = time.monotonic()

        p_tel = state.my_telemetry
        speed_kph = float(p_tel.speed_kph) if p_tel else 0.0

        vel_len = math.sqrt(mot.vx**2 + mot.vy**2 + mot.vz**2)
        if speed_kph <= 0.0 and vel_len > 0.0:
            speed_kph = vel_len * 3.6

        fwd_len = math.sqrt(mot.fwd_x**2 + mot.fwd_y**2 + mot.fwd_z**2)

        # Check if we already noted a spin entry
        if self._spin_entry_time is not None:
            elapsed = now - self._spin_entry_time
            if elapsed <= self.config.spin_window_s:
                if speed_kph < self.config.spin_exit_speed_kph:
                    self._spin_entry_time = None
                    return True
            else:
                self._spin_entry_time = None

        # Check spin entry condition
        if speed_kph > self.config.spin_entry_speed_kph and fwd_len > 1e-4 and vel_len > 1e-3:
            dot = mot.fwd_x * mot.vx + mot.fwd_y * mot.vy + mot.fwd_z * mot.vz
            cos_theta = dot / (fwd_len * vel_len)
            cos_theta = max(-1.0, min(1.0, cos_theta))
            angle_deg = math.degrees(math.acos(cos_theta))
            if angle_deg > self.config.spin_angle_deg:
                self._spin_entry_time = now

        return False

    def check_spin_manual(self, angle_deg: float, speed_kph: float, now: float) -> bool:
        """Explicit angle/speed evaluator for testing and synthetic motion validation."""
        if self._spin_entry_time is not None:
            elapsed = now - self._spin_entry_time
            if elapsed <= self.config.spin_window_s:
                if speed_kph < self.config.spin_exit_speed_kph:
                    self._spin_entry_time = None
                    return True
            else:
                self._spin_entry_time = None

        if speed_kph > self.config.spin_entry_speed_kph and angle_deg > self.config.spin_angle_deg:
            self._spin_entry_time = now

        return False

    def trigger(
        self,
        event_name: str,
        state: RaceState,
        extra_context: Optional[Dict[str, Any]] = None,
        now: Optional[float] = None,
        bypass_cooldown: bool = False,
    ) -> Optional[Callout]:
        """Evaluates cooldowns, checks escalation, formats line, and returns Callout or None."""
        if self.config.name == "professional" or not self.lines:
            return None

        if now is None:
            now = time.monotonic()

        # Build context dictionary
        ctx = extra_context.copy() if extra_context else {}
        me = state.me
        if me:
            ctx.setdefault("pos", me.position)

        current_lap = me.lap_num if me else 1

        # Check for escalation: 3+ blunders within the last 5 laps
        if event_name in BLUNDER_EVENTS:
            self.record_blunder(current_lap, event_name)
            recent = self.get_recent_blunders(current_lap, window_laps=5)
            if len(recent) >= 3:
                event_name = "escalation"
                ctx["count"] = len(recent)

        if not bypass_cooldown and self._is_on_cooldown(event_name, now):
            return None

        line = self._get_next_line(event_name, ctx)
        if not line:
            return None

        self.last_reaction_time = now
        self.last_fired_per_event[event_name] = now

        # Priority 1 for big moments, priority 2 for regular reactions; TTL 5.0 s
        prio = 1 if event_name in BIG_MOMENTS else 2
        return Callout(prio, now, line, ttl=5.0)

    def get_reaction_text(
        self,
        event_name: str,
        extra_context: Optional[Dict[str, Any]] = None,
        check_cooldown: bool = False,
        now: Optional[float] = None,
    ) -> Optional[str]:
        """Returns next formatted reaction line respecting profanity filter, optionally checking cooldowns."""
        if self.config.name == "professional" or not self.lines:
            return None
        if now is None:
            now = time.monotonic()
        if check_cooldown and self._is_on_cooldown(event_name, now):
            return None
        ctx = extra_context.copy() if extra_context else {}
        line = self._get_next_line(event_name, ctx)
        if line:
            self.last_reaction_time = now
            self.last_fired_per_event[event_name] = now
        return line

    # ---- Signal Evaluator for Main Loop ----

    def on_signal(
        self,
        sig: Tuple[str, Any],
        state: RaceState,
        spotter_active: bool = False,
        now: Optional[float] = None,
    ) -> Optional[Callout]:
        """Processes telemetry signals into personality reactions.

        Overrides:
        - No reactions while spotter has active overlap or PTT is held.
        - Big moments override cooldowns.
        """
        if spotter_active or state.ptt_down:
            return None

        kind = sig[0]
        if now is None:
            now = time.monotonic()

        # Suppress all other persona lines for 8 s after the finish
        if now < self._finish_time + 8.0 and kind not in ("finished", "final_classification"):
            return None

        p = state.player
        me = state.me
        current_lap = me.lap_num if me else 1

        # 1. Overtake / Overtaken
        if kind == "event":
            ev: packets.Event = sig[1]
            if ev.code == "OVTK":
                ov_idx = ev.overtaking_vehicle_idx
                bg_idx = ev.being_overtaken_vehicle_idx
                if ov_idx == p or bg_idx == p:
                    other_idx = bg_idx if ov_idx == p else ov_idx
                    if state.lap and p < len(state.lap) and other_idx < len(state.lap):
                        p_lap = state.lap[p]
                        other_lap = state.lap[other_idx]
                        if p_lap and other_lap:
                            # Ignore if either car in pit lane
                            if p_lap.pit_status != 0 or other_lap.pit_status != 0:
                                return None
                            # Ignore lapping traffic (compare lap numbers)
                            if p_lap.lap_num != other_lap.lap_num:
                                return None

                            other_name = state.name(other_idx).split()[-1] if state.name(other_idx) else "he"
                            ctx = {"name": other_name, "pos": p_lap.position}
                            event_type = "overtake_made" if ov_idx == p else "overtaken"
                            return self.trigger(event_type, state, ctx, now=now)

            elif ev.code == "COLL":
                # Collision involving player
                v1, v2 = ev.vehicle_1_idx, ev.vehicle_2_idx
                if v1 == p or v2 == p:
                    sev = getattr(ev, "severity", None)
                    if sev == 0:
                        # 2026: low severity touch: do not fire heavy collision roast
                        return None
                    other_idx = v2 if v1 == p else v1
                    other_name = state.name(other_idx).split()[-1] if (other_idx is not None and state.name(other_idx)) else "him"
                    return self.trigger("collision", state, {"name": other_name, "severity": sev}, now=now)

            elif ev.code == "FLBK":
                return self.trigger("flashback", state, {}, now=now)

            elif ev.code == "FTLP" and ev.vehicle_idx == p:
                laptime_str = ""
                if ev.lap_time:
                    laptime_str = f"{int(ev.lap_time // 60)}:{ev.lap_time % 60:04.1f}" if ev.lap_time >= 60 else f"{ev.lap_time:.1f}"
                return self.trigger("fastest_lap", state, {"laptime": laptime_str}, now=now)

            elif ev.code == "RTMT" and ev.vehicle_idx == p:
                return self.trigger("retirement", state, {}, now=now)

            elif ev.code in ("RCWN", "CHQF"):
                # Result is handled deterministically via ('finished', info); do not fire a second message
                pass

        elif kind == "personal_best":
            laptime_str = sig[1] if len(sig) > 1 and isinstance(sig[1], str) else ""
            return self.trigger("personal_best", state, {"laptime": laptime_str}, now=now)

        # 2. Lap-completion triggers: defended, good_start, bad_start, clean_stint, lost_positions
        elif kind == "lap":
            rec = sig[1]
            out = None

            # Defended trigger: car behind within 1.0 s for >= 3 consecutive laps and still behind
            if rec.gap_behind_ms is not None and rec.gap_behind_ms < 1000 and rec.behind_idx is not None:
                if self._defense_behind_idx == rec.behind_idx:
                    self._defense_consecutive_laps += 1
                else:
                    self._defense_behind_idx = rec.behind_idx
                    self._defense_consecutive_laps = 1

                if self._defense_consecutive_laps >= 3:
                    name = state.name(rec.behind_idx).split()[-1] if state.name(rec.behind_idx) else "he"
                    out = self.trigger("defended", state, {"name": name, "count": self._defense_consecutive_laps}, now=now)
            else:
                self._defense_behind_idx = None
                self._defense_consecutive_laps = 0

            if out:
                return out

            # Start trigger at end of lap 1 vs grid position
            if rec.lap == 1 and rec.grid_position > 0:
                diff = rec.grid_position - rec.position
                reaction_str = ""
                if state.session and getattr(state.session, "start_reaction_time", 0.0) > 0.0:
                    reaction_str = f"reaction {state.session.start_reaction_time:.2f}"

                if diff >= 2:
                    out = self.trigger("good_start", state, {"gained": diff, "reaction": reaction_str}, now=now)
                    if out and reaction_str and reaction_str not in out:
                        out = f"{out.rstrip('.')}! {reaction_str.capitalize()}."
                    return out
                elif diff <= -2:
                    out = self.trigger("bad_start", state, {"lost": abs(diff), "reaction": reaction_str}, now=now)
                    if out and reaction_str and reaction_str not in out:
                        out = f"{out.rstrip('.')}! {reaction_str.capitalize()}."
                    return out

            # Lost >= 2 positions in one lap
            if len(state.laps) >= 2:
                prev_rec = state.laps[-2]
                pos_lost = rec.position - prev_rec.position
                if pos_lost >= 2:
                    return self.trigger("lost_positions", state, {"lost": pos_lost}, now=now)

            # Clean stint trigger: 10 consecutive valid laps with no blunder
            if not rec.invalid:
                recent_blunders = [b for b in self.blunders if b[0] > self._last_clean_stint_lap]
                valid_lap_run = rec.lap - self._last_clean_stint_lap
                if valid_lap_run >= 10 and not recent_blunders:
                    self._last_clean_stint_lap = rec.lap
                    return self.trigger("clean_stint", state, {}, now=now)
            else:
                self._last_clean_stint_lap = rec.lap

        # 3. Track limits & penalty
        elif kind == "track_limits_warning":
            n = sig[1]
            return self.trigger("track_limits", state, {"count": n}, now=now)

        elif kind == "penalty":
            ev = sig[1]
            ptype = getattr(ev, "penalty_type", None)
            sec = getattr(ev, "penalty_time", 0) or 0
            is_tt = (
                getattr(state, "session_type", None) == 13
                or getattr(state, "is_time_trial", False)
                or (getattr(state, "session", None) and getattr(state.session, "session_type", None) == 13)
                or getattr(state, "time_trial", None) is not None
            )
            if is_tt:
                return None

            # Only real penalties: 0 (drive-through), 1 (stop-go), 4 (time penalty with 1 <= sec <= 60)
            is_real_penalty = (ptype in (0, 1)) or (ptype == 4 and 1 <= sec <= 60)
            if not is_real_penalty:
                return None

            # If a real penalty arrives within 10s after a divebomb -> told_you
            if now - self._last_divebomb_time <= 10.0:
                ctx = {"name": self._last_divebomb_driver}
                return self.trigger("told_you", state, ctx, now=now, bypass_cooldown=True)

            return self.trigger("penalty", state, {}, now=now)

        elif kind == "purple_sector":
            sector_num = sig[1]
            return self.trigger("purple_sector", state, {"sector": sector_num}, now=now)

        # 4. Finish detection signal
        elif kind == "finished":
            info = sig[1]
            s_uid = info.get("session_uid")
            if s_uid is not None and self._finish_spoken_session_uid == s_uid:
                return None
            self._finish_spoken_session_uid = s_uid
            self._finish_time = now
            pos = info.get("position", 1)
            self._finish_line_pos = pos

            gap_behind_ms = info.get("gap_behind_ms")
            gap_ahead_ms = info.get("gap_ahead_ms")
            gap_behind_s = (gap_behind_ms / 1000.0) if gap_behind_ms is not None else 999.0
            gap_ahead_s = (gap_ahead_ms / 1000.0) if gap_ahead_ms is not None else 999.0
            penalties_s = info.get("penalties_s", 0)

            behind_idx = info.get("behind_idx")
            behind_name = state.name(behind_idx).split()[-1] if (behind_idx is not None and state.name(behind_idx)) else "he"

            f_cfg = getattr(self.config, "finish", None)
            photo_m = getattr(f_cfg, "photo_finish_margin_s", 0.5) if f_cfg else 0.5
            close_m = getattr(f_cfg, "close_margin_s", 3.0) if f_cfg else 3.0
            comf_m = getattr(f_cfg, "comfortable_margin_s", 10.0) if f_cfg else 10.0
            pod_held_m = getattr(f_cfg, "podium_held_behind_margin_s", 1.0) if f_cfg else 1.0
            pod_miss_m = getattr(f_cfg, "podium_missed_higher_ahead_margin_s", 1.0) if f_cfg else 1.0
            miss_pod_m = getattr(f_cfg, "missed_podium_ahead_margin_s", 2.0) if f_cfg else 2.0

            ctx = {
                "pos": pos,
                "margin": f"{gap_behind_s:.1f}",
                "gap": f"{gap_ahead_s:.1f}",
                "name": behind_name,
                "higher": pos - 1 if pos > 1 else 1,
            }

            if getattr(state, "is_practice", False):
                spoken = self.trigger("practice_finish", state, ctx, now=now, bypass_cooldown=True)
                if not spoken:
                    spoken = Callout(1, now, f"Chequered flag. That's P{pos} in practice. Bring it into the box.", ttl=15.0)
                return spoken

            if getattr(state, "is_quali", False):
                if pos == 1:
                    spoken = self.trigger("quali_pole", state, ctx, now=now, bypass_cooldown=True)
                    if not spoken:
                        spoken = Callout(1, now, "Pole position! Brilliant lap! You start on pole for the race!", ttl=15.0)
                else:
                    spoken = self.trigger("quali_finish", state, ctx, now=now, bypass_cooldown=True)
                    if not spoken:
                        spoken = Callout(1, now, f"Chequered flag. That's P{pos} for the grid.", ttl=15.0)
                return spoken

            pts = state.get_points(pos) if hasattr(state, "get_points") else F1_RACE_POINTS.get(pos, 0)
            ctx["points"] = pts

            if pos == 1:
                if gap_behind_s < photo_m:
                    line_key = "win_photo_finish"
                elif gap_behind_s < close_m:
                    line_key = "win_close"
                elif gap_behind_s < comf_m:
                    line_key = "win_comfortable"
                else:
                    line_key = "win_dominant"
            elif 2 <= pos <= 3:
                if gap_behind_s < pod_held_m:
                    line_key = "podium_held"
                elif gap_ahead_s < pod_miss_m:
                    line_key = "podium_missed_higher"
                else:
                    line_key = "podium"
            elif pos == 4:
                if gap_ahead_s <= miss_pod_m:
                    line_key = "missed_podium"
                else:
                    line_key = "points"
            elif 5 <= pos <= 10:
                line_key = "points"
            else:
                line_key = "no_points"

            spoken = self.trigger(line_key, state, ctx, now=now, bypass_cooldown=True)
            if not spoken:
                fallback_key = "win" if pos == 1 else ("podium" if pos <= 3 else ("points" if pos <= 10 else "no_points"))
                spoken = self.trigger(fallback_key, state, ctx, now=now, bypass_cooldown=True)

            if not spoken:
                if pos == 1:
                    spoken = Callout(1, now, "P1! You've won the Grand Prix! 25 points, mega drive!", ttl=15.0)
                elif 2 <= pos <= 3:
                    spoken = Callout(1, now, f"P{pos}! On the podium, mate! That's {pts} points.", ttl=15.0)
                elif pos <= 10:
                    pts_str = f"{pts} points" if pts != 1 else "1 point"
                    spoken = Callout(1, now, f"P{pos} across the line. That's {pts_str} in the bag.", ttl=15.0)
                else:
                    spoken = Callout(1, now, f"P{pos} across the line. Outside the points today, but good fight.", ttl=15.0)

            if penalties_s > 0 and spoken:
                pen_word = {5: "five", 10: "ten", 3: "three"}.get(penalties_s, str(penalties_s))
                spoken.text += f" That's provisional, pending the {pen_word}-second penalty."

            return spoken

        # 5. Overtakes and position changes
        elif kind == "overtake_confirmed":
            rec = sig[1]
            cls_name = rec.get("classification", "clean")
            name = rec.get("other_driver_name", "he")
            corner = rec.get("corner_name", "the corner")
            pos = rec.get("position", state.me.position if state.me else 0)

            ctx = {"name": name, "pos": pos, "corner": corner}
            if cls_name == "passed_stricken_car":
                return self.trigger("passed_stricken_car", state, ctx, now=now)
            elif cls_name == "overtake_switchback":
                return self.trigger("overtake_switchback", state, ctx, now=now)
            elif cls_name == "overtake_mode_pass":
                return self.trigger("overtake_mode_pass", state, ctx, now=now)
            elif cls_name == "around_the_outside":
                return self.trigger("around_the_outside", state, ctx, now=now)
            elif cls_name == "divebomb":
                self._last_divebomb_time = now
                self._last_divebomb_driver = name
                return self.trigger("overtake_divebomb", state, ctx, now=now)
            elif cls_name == "divebomb_contact":
                self._last_divebomb_time = now
                self._last_divebomb_driver = name
                return self.trigger("divebomb_contact", state, ctx, now=now)
            elif cls_name == "brave":
                return self.trigger("overtake_brave", state, ctx, now=now)
            else:
                res = self.trigger("overtake_clean", state, ctx, now=now)
                if not res:
                    res = self.trigger("overtake_made", state, ctx, now=now)
                return res

        elif kind == "overtaken_confirmed":
            rec = sig[1]
            cls_name = rec.get("classification", "fair_pass_against")
            name = rec.get("other_driver_name", "he")
            corner = rec.get("corner_name", "the corner")
            pos = rec.get("position", state.me.position if state.me else 0)
            ctx = {"name": name, "pos": pos, "corner": corner}

            if cls_name == "divebombed_by":
                return self.trigger("divebombed_by", state, ctx, now=now)
            elif cls_name == "forced_off_by":
                return self.trigger("forced_off_by", state, ctx, now=now)
            elif cls_name == "fair_pass_against":
                res = self.trigger("fair_pass_against", state, ctx, now=now)
                if not res:
                    res = self.trigger("overtaken", state, ctx, now=now)
                return res
            return self.trigger("overtaken", state, ctx, now=now)

        elif kind == "defend_warning":
            info = sig[1]
            corner = info.get("corner", "corner")
            name = info.get("driver", "he")
            ctx = {"name": name, "corner": corner}
            if info.get("inside_look"):
                return self.trigger("inside_look", state, ctx, now=now)
            return self.trigger("defend_warning", state, ctx, now=now)

        # 6. Final Classification (correction only if classified pos != finish pos)
        elif kind == "final_classification":
            cars = sig[1]
            if cars and p < len(cars):
                fc = cars[p]
                pos = fc.position
                if self._finish_line_pos is not None and pos != self._finish_line_pos:
                    self._finish_line_pos = pos
                    return _c(2, f"Correction: penalties applied, classified P{pos}.", ttl=10.0)
            return None

        return None
