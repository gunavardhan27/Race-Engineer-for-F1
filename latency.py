"""Latency tracking for driver transmissions.

Measures stage-by-stage latencies:
  ptt_up -> stt_done -> route_done -> llm_first_token -> llm_done -> tts_first_audio -> playback_done
Logs one line per transmission in milliseconds and maintains rolling averages.
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("engineer.latency")

STAGES = (
    "ptt_up",
    "stt_done",
    "route_done",
    "llm_first_token",
    "llm_done",
    "tts_first_audio",
    "playback_done",
)


@dataclass
class Transmission:
    id: int
    timestamps: dict[str, float] = field(default_factory=dict)
    durations_ms: dict[str, float] = field(default_factory=dict)
    completed: bool = False


class LatencyTracker:
    def __init__(self, window_size: int = 20, logger: Optional[logging.Logger] = None):
        self.window_size = window_size
        self.log = logger or log
        self._counter = 0
        self._current: Optional[Transmission] = None
        self._history: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=self.window_size))
        self._completed_transmissions: list[Transmission] = []

    def start_transmission(self, timestamp: Optional[float] = None) -> int:
        """Starts a new transmission and returns its unique ID."""
        self._counter += 1
        t = Transmission(id=self._counter)
        if timestamp is not None:
            t.timestamps["ptt_up"] = timestamp
        else:
            t.timestamps["ptt_up"] = time.monotonic()
        self._current = t
        return self._counter

    def record(self, stage: str, transmission_id: Optional[int] = None, timestamp: Optional[float] = None) -> None:
        """Records a timestamp for a given stage. Finalizes when playback_done is recorded."""
        ts = timestamp if timestamp is not None else time.monotonic()

        # If a specific ID is given or no transmission exists, handle appropriately
        if self._current is None:
            self.start_transmission(timestamp=ts if stage == "ptt_up" else None)

        target = self._current
        if target is None:
            return

        target.timestamps[stage] = ts

        if stage == "playback_done":
            self._finalize(target)

    def cancel_active(self) -> None:
        """Cancels the in-flight transmission without adding to rolling averages (e.g. if interrupted)."""
        self._current = None

    def _finalize(self, t: Transmission) -> None:
        ts = t.timestamps
        durations = {}

        # STT duration: ptt_up -> stt_done
        if "ptt_up" in ts and "stt_done" in ts:
            durations["stt"] = max(0.0, (ts["stt_done"] - ts["ptt_up"]) * 1000.0)

        # Route duration: stt_done -> route_done
        if "stt_done" in ts and "route_done" in ts:
            durations["route"] = max(0.0, (ts["route_done"] - ts["stt_done"]) * 1000.0)

        # LLM time-to-first-token: route_done -> llm_first_token
        if "route_done" in ts and "llm_first_token" in ts:
            durations["llm_1st"] = max(0.0, (ts["llm_first_token"] - ts["route_done"]) * 1000.0)

        # LLM total duration: route_done -> llm_done
        if "route_done" in ts and "llm_done" in ts:
            durations["llm_done"] = max(0.0, (ts["llm_done"] - ts["route_done"]) * 1000.0)

        # TTS time-to-first-audio:
        # relative to llm_first_token (if AI query) or route_done (if deterministic query)
        tts_start_ref = ts.get("llm_first_token", ts.get("route_done"))
        if tts_start_ref is not None and "tts_first_audio" in ts:
            durations["tts_1st"] = max(0.0, (ts["tts_first_audio"] - tts_start_ref) * 1000.0)

        # Playback duration: tts_first_audio -> playback_done
        if "tts_first_audio" in ts and "playback_done" in ts:
            durations["playback"] = max(0.0, (ts["playback_done"] - ts["tts_first_audio"]) * 1000.0)

        # Total duration: from earliest timestamp to playback_done
        if ts and "playback_done" in ts:
            earliest = min(ts.values())
            durations["total"] = max(0.0, (ts["playback_done"] - earliest) * 1000.0)

        t.durations_ms = durations
        t.completed = True
        self._completed_transmissions.append(t)

        # Update rolling averages
        for k, v in durations.items():
            self._history[k].append(v)

        # Format one-line log
        parts = [f"{k}={v:.1f}ms" for k, v in durations.items() if k != "total"]
        total_str = f"total={durations.get('total', 0.0):.1f}ms"
        avg_total = self.rolling_average("total")
        avg_str = f"rolling_avg_total={avg_total:.1f}ms" if avg_total is not None else ""

        line = f"[LATENCY] #{t.id} | " + ", ".join(parts + [total_str])
        if avg_str:
            line += f" | {avg_str}"

        self.log.info(line)
        self._current = None

    def rolling_average(self, stage: str) -> Optional[float]:
        """Returns the rolling average duration in milliseconds for a stage, or None if no samples."""
        history = self._history.get(stage)
        if not history:
            return None
        return sum(history) / len(history)

    def rolling_averages(self) -> dict[str, float]:
        """Returns a snapshot of all current rolling averages."""
        return {k: sum(vals) / len(vals) for k, vals in self._history.items() if vals}

    def reset(self) -> None:
        """Resets tracker state and history."""
        self._counter = 0
        self._current = None
        self._history.clear()
        self._completed_transmissions.clear()
