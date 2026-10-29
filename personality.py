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
from f1telemetry.state import RaceState

log = logging.getLogger("engineer.personality")

BIG_MOMENTS = {"win", "podium", "points", "no_points", "retirement", "beat_teammate", "lost_to_teammate"}
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

        if self._is_on_cooldown(event_name, now):
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
                    other_idx = v2 if v1 == p else v1
                    other_name = state.name(other_idx).split()[-1] if (other_idx is not None and state.name(other_idx)) else "him"
                    return self.trigger("collision", state, {"name": other_name}, now=now)

            elif ev.code == "FLBK":
                return self.trigger("flashback", state, {}, now=now)

            elif ev.code == "FTLP" and ev.vehicle_idx == p:
                laptime_str = ""
                if ev.lap_time:
                    laptime_str = f"{int(ev.lap_time // 60)}:{ev.lap_time % 60:04.1f}" if ev.lap_time >= 60 else f"{ev.lap_time:.1f}"
                return self.trigger("fastest_lap", state, {"laptime": laptime_str}, now=now)

            elif ev.code == "RTMT" and ev.vehicle_idx == p:
                return self.trigger("retirement", state, {}, now=now)

            elif ev.code == "RCWN" and ev.vehicle_idx == p:
                return self.trigger("win", state, {"pos": 1}, now=now)

            elif ev.code == "CHQF":
                # Chequered flag; final classification packet will follow
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
                if diff >= 2:
                    return self.trigger("good_start", state, {"gained": diff}, now=now)
                elif diff <= -2:
                    return self.trigger("bad_start", state, {"lost": abs(diff)}, now=now)

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
            return self.trigger("penalty", state, {}, now=now)

        elif kind == "purple_sector":
            sector_num = sig[1]
            return self.trigger("purple_sector", state, {"sector": sector_num}, now=now)

        # 4. Final Classification (win, podium, points, no_points)
        elif kind == "final_classification":
            cars = sig[1]
            if cars and p < len(cars):
                fc = cars[p]
                pos = fc.position
                ctx = {"pos": pos}
                if pos == 1:
                    return self.trigger("win", state, ctx, now=now)
                elif 2 <= pos <= 3:
                    return self.trigger("podium", state, ctx, now=now)
                elif 4 <= pos <= 10 or fc.points > 0:
                    return self.trigger("points", state, ctx, now=now)
                else:
                    return self.trigger("no_points", state, ctx, now=now)

        return None
