"""Usage guard system for cloud AI services (Groq LLM, Groq STT, edge-tts).

Enforces:
1. Free-tier rate limits (RPM, RPD, TPM, TPD, audio seconds, characters).
2. Live response headers as source of truth (x-ratelimit-*).
3. 80% safety margin threshold (stops at 80% of any limit).
4. allow_paid: false ceiling clamping if account headers show paid tier limits.
5. In-memory rolling minute window + SQLite daily persistence.
6. 429 pause handling with retry-after.
7. TTS audio disk caching for 0-cost repeated text.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone, timedelta
import json
import logging
from pathlib import Path
import re
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from config import ModelRateLimits, EdgeTTSRateLimits, UsageConfig

log = logging.getLogger("engineer.usage")


def get_seconds_until_utc_midnight() -> float:
    """Calculates remaining seconds until the next 00:00:00 UTC (Groq daily quota reset)."""
    now_utc = datetime.now(timezone.utc)
    tomorrow_utc = (now_utc + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(0.0, (tomorrow_utc - now_utc).total_seconds())


def format_countdown(seconds: float) -> str:
    """Formats seconds into human-readable hours and minutes (e.g. '4h 12m')."""
    secs = int(max(0.0, seconds))
    h = secs // 3600
    m = (secs % 3600) // 60
    return f"{h}h {m}m"


def parse_duration_s(val: Any) -> float:
    """Parses duration strings like '2m59.56s', '6s', '1.5ms', '300ms', or numbers to float seconds."""
    if val is None:
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip()
    if not s:
        return 0.0
    try:
        return float(s)
    except ValueError:
        pass

    total = 0.0
    # Match milliseconds first
    ms_match = re.search(r"(\d+(?:\.\d+)?)\s*ms", s)
    if ms_match:
        total += float(ms_match.group(1)) / 1000.0
        s = re.sub(r"(\d+(?:\.\d+)?)\s*ms", "", s)

    h_match = re.search(r"(\d+(?:\.\d+)?)\s*h", s)
    if h_match:
        total += float(h_match.group(1)) * 3600.0
    m_match = re.search(r"(\d+(?:\.\d+)?)\s*m(?!s)", s)
    if m_match:
        total += float(m_match.group(1)) * 60.0
    s_match = re.search(r"(\d+(?:\.\d+)?)\s*s", s)
    if s_match:
        total += float(s_match.group(1))
    return total


class UsageGuard:
    """Central usage and rate limit guard for all cloud AI providers."""

    def __init__(self, config: Optional[UsageConfig] = None, db_path: str = "storage.db"):
        self.config = config or UsageConfig()
        self.db_path = str(db_path)
        self.max_fraction = float(self.config.max_fraction)
        self.allow_paid = bool(self.config.allow_paid)

        # In-memory rolling minute trackers: service -> list of (monotonic_ts, count)
        self._minute_reqs: Dict[str, List[Tuple[float, int]]] = {}
        self._minute_tokens: Dict[str, List[Tuple[float, int]]] = {}
        self._minute_audio: Dict[str, List[Tuple[float, float]]] = {}
        self._minute_chars: Dict[str, List[Tuple[float, int]]] = {}

        # Live header tracking: service -> dict
        self._header_state: Dict[str, Dict[str, Any]] = {}

        # Paused services: service -> monotonic_timestamp
        self._paused_until: Dict[str, float] = {}
        self._pause_reasons: Dict[str, str] = {}

        # Daily threshold warnings: date_service_key -> set of alerted thresholds (e.g. {50, 80})
        self._alerted_thresholds: Dict[str, set[int]] = {}

        # Paid tier warning logged: set of services
        self._warned_paid_limits: set[str] = set()

        # Thread lock for in-memory rolling tracking and SQLite access
        self._lock = threading.Lock()

        # Last LLM provider used
        self.last_provider_used: str = "none"

        # Caching for high-frequency dashboard polling
        self._cached_status: Optional[Dict[str, Any]] = None
        self._last_status_mono: float = 0.0

        self._init_db()

    def _init_db(self) -> None:
        """Initializes usage_daily SQLite table."""
        try:
            p = Path(self.db_path).parent
            if str(p) != "" and str(p) != ".":
                p.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS usage_daily (
                        date TEXT NOT NULL,
                        service TEXT NOT NULL,
                        requests INTEGER NOT NULL DEFAULT 0,
                        tokens INTEGER NOT NULL DEFAULT 0,
                        audio_seconds REAL NOT NULL DEFAULT 0.0,
                        characters INTEGER NOT NULL DEFAULT 0,
                        PRIMARY KEY (date, service)
                    );
                """)
                conn.commit()
        except Exception as e:
            log.warning("UsageGuard: Failed to initialize SQLite table in %s: %s", self.db_path, e)

    @staticmethod
    def _today_str() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    @staticmethod
    def estimate_tokens(prompt: str, max_tokens: int = 60) -> int:
        """Conservative token estimation before sending prompt: chars / 4 + max_tokens."""
        chars = len(prompt) if prompt else 0
        return int(chars / 4) + int(max_tokens)

    def get_service_limits(self, service: str, model: str = "") -> Dict[str, Any]:
        """Resolves configured limits for a given service and model."""
        s = service.lower()
        if s == "edge_tts" or "edge" in s:
            limits = self.config.edge_tts
            return {
                "rpm": limits.rpm,
                "rpd": limits.rpd,
                "tpm": 0,
                "tpd": 0,
                "ash": 0,
                "asd": 0,
                "chars_per_min": limits.chars_per_min,
                "chars_per_day": limits.chars_per_day,
                "model": "edge-tts",
                "service_type": "tts",
            }

        # Check if direct match in groq_models
        m_name = model
        if not m_name:
            if s in self.config.groq_models:
                m_name = s
            elif ":" in service:
                m_name = service.split(":", 1)[1]
            elif "stt" in s or "whisper" in s:
                m_name = "whisper-large-v3"
            else:
                m_name = "llama-3.1-8b-instant"

        m_limits = self.config.groq_models.get(m_name)
        if not m_limits:
            if "whisper" in m_name:
                m_limits = ModelRateLimits(rpm=20, rpd=2000, ash=7200, asd=28800)
            elif "70b" in m_name:
                m_limits = ModelRateLimits(rpm=30, rpd=1000, tpm=12000, tpd=100000)
            else:
                m_limits = ModelRateLimits(rpm=30, rpd=14400, tpm=6000, tpd=500000)

        is_stt = "whisper" in m_name or "stt" in s
        return {
            "rpm": m_limits.rpm,
            "rpd": m_limits.rpd,
            "tpm": m_limits.tpm if not is_stt else 0,
            "tpd": m_limits.tpd if not is_stt else 0,
            "ash": m_limits.ash if is_stt else 0,
            "asd": m_limits.asd if is_stt else 0,
            "chars_per_min": 0,
            "chars_per_day": 0,
            "model": m_name,
            "service_type": "stt" if is_stt else "llm",
        }

    def _clean_minute_window(self, now: float) -> None:
        """Evicts entries older than 60 seconds from the rolling window."""
        cutoff = now - 60.0
        for container in (self._minute_reqs, self._minute_tokens, self._minute_audio, self._minute_chars):
            for s_key in list(container.keys()):
                container[s_key] = [item for item in container[s_key] if item[0] >= cutoff]

    def _get_minute_metric(self, container: Dict[str, List[Tuple[float, Any]]], service: str) -> float:
        items = container.get(service, [])
        return sum(item[1] for item in items)

    def _get_daily_usage(self, service: str, date_str: Optional[str] = None) -> Dict[str, Any]:
        """Queries SQLite for daily totals."""
        d = date_str or self._today_str()
        try:
            with sqlite3.connect(self.db_path) as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT requests, tokens, audio_seconds, characters FROM usage_daily WHERE date = ? AND service = ?",
                    (d, service),
                )
                row = cur.fetchone()
                if row:
                    return {
                        "requests": int(row[0]),
                        "tokens": int(row[1]),
                        "audio_seconds": float(row[2]),
                        "characters": int(row[3]),
                    }
        except Exception as e:
            log.warning("UsageGuard: Failed to query daily usage: %s", e)
        return {"requests": 0, "tokens": 0, "audio_seconds": 0.0, "characters": 0}

    def _save_daily_usage(
        self,
        service: str,
        add_requests: int = 0,
        add_tokens: int = 0,
        add_audio_seconds: float = 0.0,
        add_chars: int = 0,
    ) -> Dict[str, Any]:
        """Upserts daily usage in SQLite and returns updated totals."""
        d = self._today_str()
        res = {"requests": 0, "tokens": 0, "audio_seconds": 0.0, "characters": 0}
        try:
            with sqlite3.connect(self.db_path) as conn:
                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO usage_daily (date, service, requests, tokens, audio_seconds, characters)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(date, service) DO UPDATE SET
                        requests = requests + excluded.requests,
                        tokens = tokens + excluded.tokens,
                        audio_seconds = audio_seconds + excluded.audio_seconds,
                        characters = characters + excluded.characters;
                """, (d, service, add_requests, add_tokens, add_audio_seconds, add_chars))
                conn.commit()

                cur.execute(
                    "SELECT requests, tokens, audio_seconds, characters FROM usage_daily WHERE date = ? AND service = ?",
                    (d, service),
                )
                row = cur.fetchone()
                if row:
                    res = {
                        "requests": int(row[0]),
                        "tokens": int(row[1]),
                        "audio_seconds": float(row[2]),
                        "characters": int(row[3]),
                    }
        except Exception as e:
            log.warning("UsageGuard: Failed to persist daily usage: %s", e)
        return res

    def pause_service(self, service: str, duration_s: float, reason: str = "") -> None:
        """Pauses a service for duration_s (e.g. after HTTP 429)."""
        with self._lock:
            now = time.monotonic()
            until = now + max(1.0, float(duration_s))
            self._paused_until[service] = until
            self._pause_reasons[service] = reason
            log.warning("UsageGuard: Pausing service '%s' for %.1fs until %.1f (%s)", service, duration_s, until, reason)

    def is_paused(self, service: str) -> Tuple[bool, float, str]:
        """Returns (is_paused, remaining_seconds, reason)."""
        with self._lock:
            now = time.monotonic()
            until = self._paused_until.get(service, 0.0)
            if now < until:
                return True, until - now, self._pause_reasons.get(service, "Service paused")
            return False, 0.0, ""

    def check_allowed(
        self,
        service: str,
        est_tokens: int = 0,
        est_audio_seconds: float = 0.0,
        est_chars: int = 0,
        model: str = "",
    ) -> Tuple[bool, str]:
        """Checks if a request is permitted within the safety margin (max_fraction).

        Returns (allowed: bool, reason: str).
        """
        now = time.monotonic()
        with self._lock:
            # 1. Check paused status
            until = self._paused_until.get(service, 0.0)
            if now < until:
                rem = until - now
                reason = self._pause_reasons.get(service, "HTTP 429 rate limit")
                return False, f"Service '{service}' is paused for {rem:.1f}s ({reason})"

            # 2. Evict expired rolling minute data
            self._clean_minute_window(now)

            limits = self.get_service_limits(service, model)
            rpm = limits.get("rpm", 30)
            rpd = limits.get("rpd", 1000)
            tpm = limits.get("tpm", 6000)
            tpd = limits.get("tpd", 100000)
            ash = limits.get("ash", 7200)
            asd = limits.get("asd", 28800)
            chars_min = limits.get("chars_per_min", 10000)
            chars_day = limits.get("chars_per_day", 200000)

            # 3. Check live response header constraints if available
            hdr = self._header_state.get(service, {})
            if hdr:
                # Clamped remaining requests from headers
                hdr_rem_req = hdr.get("remaining_requests")
                hdr_limit_req = hdr.get("limit_requests")
                if hdr_rem_req is not None and hdr_limit_req is not None:
                    # Enforce free-tier limit ceiling if paid headers detected
                    eff_limit = min(hdr_limit_req, rpm) if not self.allow_paid else hdr_limit_req
                    used_req = max(0, hdr_limit_req - hdr_rem_req)
                    eff_rem = max(0, eff_limit - used_req)
                    # Deny if below safety margin
                    min_reserve = int(eff_limit * (1.0 - self.max_fraction))
                    if eff_rem <= min_reserve:
                        return False, f"Groq header remaining requests ({eff_rem}) at or below reserve threshold ({min_reserve})"

                # Clamped remaining tokens from headers
                hdr_rem_tok = hdr.get("remaining_tokens")
                hdr_limit_tok = hdr.get("limit_tokens")
                if hdr_rem_tok is not None and hdr_limit_tok is not None:
                    eff_limit_tok = min(hdr_limit_tok, tpm) if not self.allow_paid else hdr_limit_tok
                    used_tok = max(0, hdr_limit_tok - hdr_rem_tok)
                    eff_rem_tok = max(0, eff_limit_tok - used_tok)
                    tok_reserve = int(eff_limit_tok * (1.0 - self.max_fraction))
                    if eff_rem_tok - est_tokens <= tok_reserve:
                        return False, f"Groq header remaining tokens ({eff_rem_tok} - est {est_tokens}) below reserve threshold ({tok_reserve})"

            # 4. Check rolling minute requests
            cur_min_req = self._get_minute_metric(self._minute_reqs, service)
            allowed_min_req = int(rpm * self.max_fraction)
            if cur_min_req + 1 > allowed_min_req:
                return False, f"Minute request limit reached: {cur_min_req + 1} > {allowed_min_req} ({self.max_fraction*100:.0f}% of {rpm} RPM)"

            # 5. Check daily requests from SQLite
            daily_stats = self._get_daily_usage(service)
            cur_day_req = daily_stats["requests"]
            allowed_day_req = int(rpd * self.max_fraction)
            if cur_day_req + 1 > allowed_day_req:
                return False, f"Daily request limit reached: {cur_day_req + 1} > {allowed_day_req} ({self.max_fraction*100:.0f}% of {rpd} RPD)"

            # 6. Check tokens if LLM request
            if est_tokens > 0:
                cur_min_tok = self._get_minute_metric(self._minute_tokens, service)
                allowed_min_tok = int(tpm * self.max_fraction)
                if cur_min_tok + est_tokens > allowed_min_tok:
                    return False, f"Minute token limit reached: {cur_min_tok + est_tokens} > {allowed_min_tok} ({self.max_fraction*100:.0f}% of {tpm} TPM)"

                cur_day_tok = daily_stats["tokens"]
                allowed_day_tok = int(tpd * self.max_fraction)
                if cur_day_tok + est_tokens > allowed_day_tok:
                    return False, f"Daily token limit reached: {cur_day_tok + est_tokens} > {allowed_day_tok} ({self.max_fraction*100:.0f}% of {tpd} TPD)"

            # 7. Check audio seconds if STT request
            if est_audio_seconds > 0.0:
                cur_day_aud = daily_stats["audio_seconds"]
                allowed_day_aud = float(asd * self.max_fraction)
                if cur_day_aud + est_audio_seconds > allowed_day_aud:
                    return False, f"Daily audio limit reached: {cur_day_aud + est_audio_seconds:.1f}s > {allowed_day_aud:.1f}s ({self.max_fraction*100:.0f}% of {asd}s ASD)"

            # 8. Check characters if TTS request
            if est_chars > 0:
                cur_min_chars = self._get_minute_metric(self._minute_chars, service)
                allowed_min_chars = int(chars_min * self.max_fraction)
                if cur_min_chars + est_chars > allowed_min_chars:
                    return False, f"Minute char limit reached: {cur_min_chars + est_chars} > {allowed_min_chars} ({self.max_fraction*100:.0f}% of {chars_min} chars/min)"

                cur_day_chars = daily_stats["characters"]
                allowed_day_chars = int(chars_day * self.max_fraction)
                if cur_day_chars + est_chars > allowed_day_chars:
                    return False, f"Daily char limit reached: {cur_day_chars + est_chars} > {allowed_day_chars} ({self.max_fraction*100:.0f}% of {chars_day} chars/day)"

            return True, "Allowed"

    def record_usage(
        self,
        service: str,
        requests: int = 1,
        tokens: int = 0,
        audio_seconds: float = 0.0,
        characters: int = 0,
        headers: Optional[Dict[str, Any]] = None,
        status_code: int = 200,
        model: str = "",
    ) -> None:
        """Records actual usage and response headers. Handles 429 pausing and threshold alerts."""
        now = time.monotonic()
        with self._lock:
            self._cached_status = None
            # 1. Handle HTTP 429
            if status_code == 429:
                retry_after_val = None
                if headers:
                    for k, v in headers.items():
                        if k.lower() == "retry-after":
                            retry_after_val = v
                            break
                dur = parse_duration_s(retry_after_val) if retry_after_val else 60.0
                if dur <= 0.0:
                    dur = 60.0
                self._paused_until[service] = now + dur
                self._pause_reasons[service] = f"HTTP 429 rate limit (retry-after {dur:.1f}s)"
                log.warning("UsageGuard: HTTP 429 received for '%s'. Pausing service for %.1fs.", service, dur)
                return

            # 2. Update rolling minute memory structures
            self._clean_minute_window(now)
            if requests > 0:
                self._minute_reqs.setdefault(service, []).append((now, requests))
            if tokens > 0:
                self._minute_tokens.setdefault(service, []).append((now, tokens))
            if audio_seconds > 0.0:
                self._minute_audio.setdefault(service, []).append((now, audio_seconds))
            if characters > 0:
                self._minute_chars.setdefault(service, []).append((now, characters))

            # 3. Parse headers
            if headers:
                h_norm = {k.lower(): str(v) for k, v in headers.items()}
                lim_req = h_norm.get("x-ratelimit-limit-requests")
                rem_req = h_norm.get("x-ratelimit-remaining-requests")
                reset_req = h_norm.get("x-ratelimit-reset-requests")
                lim_tok = h_norm.get("x-ratelimit-limit-tokens")
                rem_tok = h_norm.get("x-ratelimit-remaining-tokens")
                reset_tok = h_norm.get("x-ratelimit-reset-tokens")

                limits = self.get_service_limits(service, model)
                rpm = limits.get("rpm", 30)
                tpm = limits.get("tpm", 6000)

                parsed_hdr: Dict[str, Any] = {"updated_at": now}
                if lim_req is not None:
                    try:
                        parsed_hdr["limit_requests"] = int(lim_req)
                    except ValueError:
                        pass
                if rem_req is not None:
                    try:
                        parsed_hdr["remaining_requests"] = int(rem_req)
                    except ValueError:
                        pass
                if reset_req is not None:
                    parsed_hdr["reset_requests_s"] = parse_duration_s(reset_req)

                if lim_tok is not None:
                    try:
                        parsed_hdr["limit_tokens"] = int(lim_tok)
                    except ValueError:
                        pass
                if rem_tok is not None:
                    try:
                        parsed_hdr["remaining_tokens"] = int(rem_tok)
                    except ValueError:
                        pass
                if reset_tok is not None:
                    parsed_hdr["reset_tokens_s"] = parse_duration_s(reset_tok)

                # Never pay: check if headers exceed configured free-tier values
                if not self.allow_paid:
                    hdr_tpm = parsed_hdr.get("limit_tokens")
                    hdr_rpm = parsed_hdr.get("limit_requests")
                    if (hdr_tpm and hdr_tpm > tpm) or (hdr_rpm and hdr_rpm > rpm):
                        if service not in self._warned_paid_limits:
                            log.warning(
                                "UsageGuard: Groq header limit (RPM=%s, TPM=%s) exceeds configured free-tier limits (RPM=%s, TPM=%s); enforcing free-tier ceiling because allow_paid is false",
                                hdr_rpm, hdr_tpm, rpm, tpm
                            )
                            self._warned_paid_limits.add(service)

                self._header_state[service] = parsed_hdr

            # 4. SQLite persistence
            updated_daily = self._save_daily_usage(
                service,
                add_requests=requests,
                add_tokens=tokens,
                add_audio_seconds=audio_seconds,
                add_chars=characters,
            )

            # 5. Check 50% and 80% daily alerts
            limits = self.get_service_limits(service, model)
            rpd = limits.get("rpd", 1000)
            tpd = limits.get("tpd", 100000)
            asd = limits.get("asd", 28800)
            chars_day = limits.get("chars_per_day", 200000)

            # Compute highest percentage of daily limit reached
            pcts = []
            if rpd > 0:
                pcts.append((updated_daily["requests"] / float(rpd)) * 100.0)
            if tokens > 0 and tpd > 0:
                pcts.append((updated_daily["tokens"] / float(tpd)) * 100.0)
            if audio_seconds > 0.0 and asd > 0:
                pcts.append((updated_daily["audio_seconds"] / float(asd)) * 100.0)
            if characters > 0 and chars_day > 0:
                pcts.append((updated_daily["characters"] / float(chars_day)) * 100.0)

            if pcts:
                max_pct = max(pcts)
                alert_key = f"{self._today_str()}:{service}"
                alerted = self._alerted_thresholds.setdefault(alert_key, set())
                if max_pct >= 50.0 and 50 not in alerted:
                    log.warning("UsageGuard: Service '%s' reached 50%% of a daily limit (current: %.1f%%)", service, max_pct)
                    alerted.add(50)
                if max_pct >= 80.0 and 80 not in alerted:
                    log.warning("UsageGuard: Service '%s' reached 80%% of a daily limit (current: %.1f%%)", service, max_pct)
                    alerted.add(80)

    def get_status(self, force: bool = False) -> Dict[str, Any]:
        """Collects usage statistics, limits, percentages, and states for dashboard and CLI."""
        now = time.monotonic()
        sec_to_reset = get_seconds_until_utc_midnight()
        countdown_str = format_countdown(sec_to_reset)

        with self._lock:
            if not force and self._cached_status and (now - self._last_status_mono < 1.0):
                cached = dict(self._cached_status)
                cached["seconds_to_reset"] = round(sec_to_reset, 1)
                cached["countdown_str"] = countdown_str
                cached["last_provider"] = self.last_provider_used
                return cached

            self._clean_minute_window(now)
            services_tracked = {"groq", "groq_stt", "edge_tts"}
            for k in list(self._minute_reqs.keys()) + list(self._header_state.keys()):
                services_tracked.add(k)

            today = self._today_str()
            result = {
                "date": today,
                "allow_paid": self.allow_paid,
                "max_fraction": self.max_fraction,
                "cutoff_pct": round(self.max_fraction * 100.0, 1),
                "seconds_to_reset": round(sec_to_reset, 1),
                "countdown_str": countdown_str,
                "last_provider": self.last_provider_used,
                "overall_quota_pct": 0.0,
                "services": {},
            }

            max_overall_daily_pct = 0.0

            for s_name in sorted(services_tracked):
                limits = self.get_service_limits(s_name)
                rpm = limits.get("rpm", 30)
                rpd = limits.get("rpd", 1000)
                tpm = limits.get("tpm", 6000)
                tpd = limits.get("tpd", 100000)
                ash = limits.get("ash", 7200)
                asd = limits.get("asd", 28800)
                chars_min = limits.get("chars_per_min", 10000)
                chars_day = limits.get("chars_per_day", 200000)

                min_req = int(self._get_minute_metric(self._minute_reqs, s_name))
                min_tok = int(self._get_minute_metric(self._minute_tokens, s_name))
                min_aud = float(self._get_minute_metric(self._minute_audio, s_name))
                min_chr = int(self._get_minute_metric(self._minute_chars, s_name))

                daily = self._get_daily_usage(s_name, today)

                # Determine state: ok / near limit / paused / using fallback
                is_p, rem_pause, reason = False, 0.0, ""
                until = self._paused_until.get(s_name, 0.0)
                if now < until:
                    is_p = True
                    rem_pause = until - now
                    reason = self._pause_reasons.get(s_name, "Paused")

                # Compute minute & daily percentage
                pct_req_min = (min_req / float(rpm)) * 100.0 if rpm > 0 else 0.0
                pct_req_day = (daily["requests"] / float(rpd)) * 100.0 if rpd > 0 else 0.0
                pct_tok_min = (min_tok / float(tpm)) * 100.0 if tpm > 0 else 0.0
                pct_tok_day = (daily["tokens"] / float(tpd)) * 100.0 if tpd > 0 else 0.0
                pct_aud_day = (daily["audio_seconds"] / float(asd)) * 100.0 if asd > 0 else 0.0
                pct_chr_min = (min_chr / float(chars_min)) * 100.0 if chars_min > 0 else 0.0
                pct_chr_day = (daily["characters"] / float(chars_day)) * 100.0 if chars_day > 0 else 0.0

                daily_pct = max(pct_req_day, pct_tok_day, pct_aud_day, pct_chr_day)
                if daily_pct > max_overall_daily_pct:
                    max_overall_daily_pct = daily_pct

                max_pct = max(pct_req_min, daily_pct, pct_chr_min)

                if is_p:
                    state = "paused"
                elif max_pct >= (self.max_fraction * 100.0):
                    state = "using fallback"
                elif max_pct >= 60.0:
                    state = "near limit"
                else:
                    state = "ok"

                result["services"][s_name] = {
                    "state": state,
                    "paused_remaining_s": round(rem_pause, 1),
                    "pause_reason": reason,
                    "minute": {
                        "requests": min_req,
                        "requests_limit": rpm,
                        "requests_pct": round(pct_req_min, 1),
                        "tokens": min_tok,
                        "tokens_limit": tpm,
                        "tokens_pct": round(pct_tok_min, 1),
                        "characters": min_chr,
                        "characters_limit": chars_min,
                    },
                    "daily": {
                        "requests": daily["requests"],
                        "requests_limit": rpd,
                        "requests_pct": round(pct_req_day, 1),
                        "tokens": daily["tokens"],
                        "tokens_limit": tpd,
                        "tokens_pct": round(pct_tok_day, 1),
                        "audio_seconds": round(daily["audio_seconds"], 1),
                        "audio_limit": asd,
                        "audio_pct": round(pct_aud_day, 1),
                        "characters": daily["characters"],
                        "characters_limit": chars_day,
                        "characters_pct": round(pct_chr_day, 1),
                    },
                    "header": self._header_state.get(s_name, {}),
                }

            result["overall_quota_pct"] = round(max_overall_daily_pct, 1)
            self._cached_status = result
            self._last_status_mono = now
            return result

    def get_daily_history(self, days: int = 7) -> List[Dict[str, Any]]:
        """Queries SQLite for daily totals over the past N days."""
        days = max(1, min(days, 90))
        result = []
        try:
            with sqlite3.connect(self.db_path) as conn:
                cur = conn.cursor()
                cur.execute("""
                    SELECT date, service, requests, tokens, audio_seconds, characters
                    FROM usage_daily
                    ORDER BY date DESC, service ASC
                """)
                rows = cur.fetchall()
                seen_dates: List[str] = []
                for row in rows:
                    d_str = row[0]
                    if d_str not in seen_dates:
                        seen_dates.append(d_str)
                    if len(seen_dates) > days:
                        continue

                    limits = self.get_service_limits(row[1])
                    rpd = limits.get("rpd", 1000)
                    tpd = limits.get("tpd", 100000)
                    asd = limits.get("asd", 28800)
                    chars_day = limits.get("chars_per_day", 200000)

                    pct_req = (row[2] / float(rpd)) * 100.0 if rpd > 0 else 0.0
                    pct_tok = (row[3] / float(tpd)) * 100.0 if tpd > 0 else 0.0
                    pct_aud = (row[4] / float(asd)) * 100.0 if asd > 0 else 0.0
                    pct_chr = (row[5] / float(chars_day)) * 100.0 if chars_day > 0 else 0.0
                    max_pct = max(pct_req, pct_tok, pct_aud, pct_chr)

                    result.append({
                        "date": row[0],
                        "service": row[1],
                        "requests": int(row[2]),
                        "tokens": int(row[3]),
                        "audio_seconds": round(float(row[4]), 1),
                        "characters": int(row[5]),
                        "limits": {
                            "rpd": rpd,
                            "tpd": tpd,
                            "asd": asd,
                            "chars_day": chars_day,
                        },
                        "max_pct": round(max_pct, 1),
                    })
        except Exception as e:
            log.warning("UsageGuard: Failed to query daily history: %s", e)
        return result

    def run_preflight_check(self, print_banner: bool = True) -> Dict[str, Any]:
        """Runs pre-flight check on startup, verifying remaining quota and logging summary."""
        today = self._today_str()
        sec_reset = get_seconds_until_utc_midnight()
        countdown_str = format_countdown(sec_reset)

        status = self.get_status()
        services = status["services"]

        overall_pct = 0.0
        service_summaries = []

        for name, data in services.items():
            d = data["daily"]
            r_pct = d.get("requests_pct", 0.0)
            t_pct = d.get("tokens_pct", 0.0)
            a_pct = d.get("audio_pct", 0.0)
            c_pct = d.get("characters_pct", 0.0)
            s_pct = max(r_pct, t_pct, a_pct, c_pct)
            if s_pct > overall_pct:
                overall_pct = s_pct

            service_summaries.append({
                "service": name,
                "pct": s_pct,
                "state": data["state"],
                "requests": d.get("requests", 0),
                "requests_limit": d.get("requests_limit", 0),
            })

        cutoff_pct = self.max_fraction * 100.0
        if overall_pct >= cutoff_pct:
            health = "CUTOFF"
            msg = f"Cloud AI usage is at or above safety cutoff ({overall_pct:.1f}% >= {cutoff_pct:.0f}%). Using local fallback."
            log.warning("UsageGuard Pre-Flight: %s", msg)
        elif overall_pct >= 60.0:
            health = "NEAR_LIMIT"
            msg = f"Cloud AI usage is near limit ({overall_pct:.1f}%). Safety cutoff at {cutoff_pct:.0f}%."
            log.warning("UsageGuard Pre-Flight: %s", msg)
        else:
            health = "OK"
            msg = f"All cloud services within free-tier limits ({overall_pct:.1f}% used). Quota resets in {countdown_str}."
            log.info("UsageGuard Pre-Flight: %s", msg)

        if print_banner:
            print("\n" + "─" * 70)
            print(f" 🛡️  AI USAGE PRE-FLIGHT CHECK ({today})")
            print(f"    Mode: {'PAID ALLOWED' if self.allow_paid else 'FREE TIER ENFORCED'} | Safety Cutoff: {cutoff_pct:.0f}%")
            print(f"    Daily Quota Reset: in {countdown_str} (00:00 UTC)")
            for s in service_summaries:
                print(f"    • {s['service'].upper()}: {s['pct']:.1f}% used ({s['requests']}/{s['requests_limit']} reqs) [{s['state'].upper()}]")
            print(f"    Status: {health} — {msg}")
            print("─" * 70 + "\n", flush=True)

        return {
            "health": health,
            "overall_pct": round(overall_pct, 1),
            "cutoff_pct": round(cutoff_pct, 1),
            "message": msg,
            "seconds_to_reset": sec_reset,
            "countdown_str": countdown_str,
            "services": service_summaries,
        }

    def print_usage_summary(self) -> None:
        """Formatted terminal output for `python main.py --usage`."""
        status = self.get_status()
        print("=" * 72)
        print(f" RACE ENGINEER AI USAGE & RATE LIMIT GUARD ({status['date']})")
        print(f" Mode: {'PAID ALLOWED' if status['allow_paid'] else 'FREE TIER ENFORCED (allow_paid=false)'}")
        print(f" Safety Margin: Cutoff at {status['max_fraction'] * 100:.0f}% of limits")
        print(f" Daily Reset: in {status.get('countdown_str', '--')} (00:00 UTC)")
        print(f" Last Provider Used: {status['last_provider']}")
        print("=" * 72)

        for s_name, data in status["services"].items():
            st = data["state"].upper()
            st_color = "\033[92m" if st == "OK" else ("\033[93m" if st == "NEAR LIMIT" else "\033[91m")
            reset_color = "\033[0m"

            print(f"\n[Service: {s_name}] Status: {st_color}{st}{reset_color}")
            if data["paused_remaining_s"] > 0:
                print(f"  Paused for: {data['paused_remaining_s']}s ({data['pause_reason']})")

            m = data["minute"]
            d = data["daily"]

            # Requests
            print(f"  Requests (RPM/RPD):")
            print(f"    Minute: {m['requests']} / {m['requests_limit']} ({m['requests_pct']}%)")
            print(f"    Today:  {d['requests']} / {d['requests_limit']} ({d['requests_pct']}%)")

            # Tokens if applicable
            if m["tokens_limit"] > 0 or d["tokens"] > 0:
                print(f"  Tokens (TPM/TPD):")
                print(f"    Minute: {m['tokens']} / {m['tokens_limit']} ({m['tokens_pct']}%)")
                print(f"    Today:  {d['tokens']} / {d['tokens_limit']} ({d['tokens_pct']}%)")

            # Audio seconds if applicable
            if d["audio_seconds"] > 0 or d["audio_limit"] > 0:
                print(f"  Audio Seconds (ASD):")
                print(f"    Today:  {d['audio_seconds']}s / {d['audio_limit']}s ({d['audio_pct']}%)")

            # Characters if applicable
            if m["characters"] > 0 or d["characters"] > 0:
                print(f"  Characters (Minute/Today):")
                print(f"    Minute: {m['characters']} / {m['characters_limit']}")
                print(f"    Today:  {d['characters']} / {d['characters_limit']} ({d['characters_pct']}%)")

            # Live headers if available
            hdr = data.get("header")
            if hdr:
                print(f"  Live Headers:")
                if "remaining_requests" in hdr:
                    print(f"    Remaining Requests: {hdr.get('remaining_requests')} (reset in {hdr.get('reset_requests_s', 0):.1f}s)")
                if "remaining_tokens" in hdr:
                    print(f"    Remaining Tokens:   {hdr.get('remaining_tokens')} (reset in {hdr.get('reset_tokens_s', 0):.1f}s)")

        print("\n" + "=" * 72)

