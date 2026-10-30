"""TTS audio cache manager for Race Engineer.

Features:
- Only caches static phrases (spotter calls, menu labels, fixed radio lines, persona lines without placeholders).
- Never caches dynamic phrases containing numbers, times, gaps, or positions.
- Compresses audio to int16 format, cutting disk storage by 50%.
- Automatic orphan cleanup on voice change or lines editing.
- LRU eviction respecting max disk size cap (default 100 MB).
- Cross-platform cache directory (Windows %LOCALAPPDATA%\\RaceEngineer\\cache\\audio, macOS ~/Library/Caches/RaceEngineer/audio).
"""
from __future__ import annotations

import hashlib
import io
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Iterable, Optional, Tuple

import numpy as np

log = logging.getLogger("engineer.audio_cache")

# Built-in fixed radio phrases that are always static
KNOWN_STATIC_PHRASES: set[str] = {
    "radio check.",
    "radio check, radio check.",
    "loud and clear.",
    "drive-through penalty.",
    "stop-go penalty.",
    "stop-and-go penalty.",
    "safety car, safety car.",
    "virtual safety car.",
    "virtual safety car, virtual safety car.",
    "vsc ending.",
    "yellow flag.",
    "green flag.",
    "pit window open.",
    "box this lap.",
    "box, box, box.",
    "quiet mode on.",
    "quiet mode off.",
    "banter off.",
    "banter on.",
    "car left.",
    "car right.",
    "clear.",
    "clear left.",
    "clear right.",
    "three wide.",
}


def get_default_cache_dir() -> Path:
    """Returns platform-standard audio cache directory."""
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            base = Path(local_app_data) / "RaceEngineer" / "cache" / "audio"
        else:
            base = Path.home() / "AppData" / "Local" / "RaceEngineer" / "cache" / "audio"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches" / "RaceEngineer" / "audio"
    else:
        xdg = os.environ.get("XDG_CACHE_HOME")
        if xdg:
            base = Path(xdg) / "race_engineer" / "cache" / "audio"
        else:
            base = Path.home() / ".cache" / "race_engineer" / "audio"
    base.mkdir(parents=True, exist_ok=True)
    return base


def normalize_cache_text(text: str) -> str:
    """Normalizes phrase string for cache key generation."""
    return re.sub(r"\s+", " ", text.strip().lower())


def is_static_cacheable(text: str, registered_static: Optional[set[str]] = None) -> bool:
    """Determines whether a phrase is static and safe to cache.

    Rules:
    - Never cache text containing numbers/digits (gaps, lap times, positions, percentages).
    - Never cache text containing template placeholders ({name}, {gap}, etc.).
    - Static phrases include known spotter calls, menu commands, and registered static persona lines.
    """
    clean = text.strip()
    if not clean:
        return False

    # Check for template braces
    if "{" in clean or "}" in clean:
        return False

    # Check for digits/numbers (times, gaps, positions, percentages)
    if re.search(r"\d", clean):
        return False

    norm = normalize_cache_text(clean)
    if norm in KNOWN_STATIC_PHRASES:
        return True

    if registered_static and norm in registered_static:
        return True

    # If the phrase contains no digits and no braces, it is static (e.g. fixed radio commands)
    return True


class AudioCacheManager:
    """Manages on-disk caching, LRU eviction, and orphan cleanup of synthesized speech clips."""

    def __init__(
        self,
        cache_dir: Optional[Path | str] = None,
        voice: str = "en-GB-RyanNeural",
        max_mb: float = 100.0,
    ):
        self.cache_dir = Path(cache_dir) if cache_dir else get_default_cache_dir()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.voice = voice
        self.max_bytes = int(max(1.0, max_mb) * 1024 * 1024)
        self._registered_static_phrases: set[str] = set()

    def register_static_phrases(self, phrases: Iterable[str]) -> None:
        """Registers a set of known static phrases (e.g. from persona YAML files)."""
        for p in phrases:
            if not p or "{" in p or "}" in p or re.search(r"\d", p):
                continue
            self._registered_static_phrases.add(normalize_cache_text(p))

    def _cache_keys(self, text: str) -> list[str]:
        keys = []
        raw = f"{self.voice}:{text.strip()}".encode("utf-8")
        keys.append(hashlib.sha256(raw).hexdigest())
        norm = f"{self.voice}:{normalize_cache_text(text)}".encode("utf-8")
        h_norm = hashlib.sha256(norm).hexdigest()
        if h_norm not in keys:
            keys.append(h_norm)
        return keys

    def _cache_key(self, text: str) -> str:
        return self._cache_keys(text)[0]

    def get_cache_file(self, text: str) -> Path:
        for k in self._cache_keys(text):
            p = self.cache_dir / f"{k}.npz"
            if p.is_file():
                return p
        return self.cache_dir / f"{self._cache_key(text)}.npz"

    def is_cacheable(self, text: str) -> bool:
        return is_static_cacheable(text, self._registered_static_phrases)

    def load(self, text: str) -> Optional[Tuple[np.ndarray, int]]:
        """Loads cached audio if available and updates LRU timestamp."""
        if not self.is_cacheable(text):
            return None

        cache_file = self.get_cache_file(text)
        if not cache_file.is_file():
            return None

        try:
            # Update access time for LRU tracking
            os.utime(cache_file, None)
            with np.load(cache_file) as npz:
                raw_audio = npz["audio"]
                sr = int(npz["sample_rate"])

                # Decompress int16 -> float32 if saved in int16
                if raw_audio.dtype == np.int16:
                    audio = (raw_audio.astype(np.float32) / 32767.0).copy()
                else:
                    audio = raw_audio.astype(np.float32).copy()

                return audio, sr
        except Exception as e:
            log.warning("Failed loading audio cache file %s: %s", cache_file, e)
            try:
                cache_file.unlink()
            except Exception:
                pass
            return None

    def save(self, text: str, audio: np.ndarray, sample_rate: int) -> bool:
        """Compresses audio to int16 and saves to disk cache if phrase is static."""
        if not self.is_cacheable(text):
            return False

        if audio is None or len(audio) == 0:
            return False

        cache_file = self.get_cache_file(text)
        try:
            # Convert float32 [-1.0, 1.0] to int16 to halve disk usage
            audio_f32 = audio.astype(np.float32) if audio.dtype != np.float32 else audio
            audio_i16 = (np.clip(audio_f32, -1.0, 1.0) * 32767.0).astype(np.int16)

            np.savez_compressed(cache_file, audio=audio_i16, sample_rate=int(sample_rate))
            self.enforce_size_cap()
            return True
        except Exception as e:
            log.warning("Failed saving audio cache file %s: %s", cache_file, e)
            return False

    def enforce_size_cap(self) -> int:
        """Evicts least-recently-used files when total cache size exceeds max_bytes.

        Returns:
            Number of evicted files.
        """
        try:
            files = list(self.cache_dir.glob("*.npz"))
            if not files:
                return 0

            # List of (file, size, access_time)
            file_stats = []
            total_size = 0
            for f in files:
                try:
                    st = f.stat()
                    # Use atime if available and reliable, fallback to mtime
                    atime = getattr(st, "st_atime", st.st_mtime)
                    file_stats.append((f, st.st_size, atime))
                    total_size += st.st_size
                except OSError:
                    pass

            if total_size <= self.max_bytes:
                return 0

            # Sort by access time ascending (oldest first)
            file_stats.sort(key=lambda x: x[2])
            target_size = int(self.max_bytes * 0.85)  # Evict down to 85% of budget
            evicted = 0

            for f, sz, _ in file_stats:
                if total_size <= target_size:
                    break
                try:
                    f.unlink()
                    total_size -= sz
                    evicted += 1
                except OSError:
                    pass

            if evicted > 0:
                log.info("LRU audio cache eviction: removed %d files, new size: %.2f MB",
                         evicted, total_size / (1024 * 1024))
            return evicted
        except Exception as e:
            log.warning("Error enforcing audio cache size cap: %s", e)
            return 0

    def cleanup_orphans(self, all_static_phrases: Optional[Iterable[str]] = None) -> int:
        """Scans cache directory and removes files not matching current voice + static phrases.

        Returns:
            Number of orphaned files removed.
        """
        phrases = set(self._registered_static_phrases)
        phrases.update(KNOWN_STATIC_PHRASES)
        if all_static_phrases:
            for p in all_static_phrases:
                if p and "{" not in p and "}" not in p and not re.search(r"\d", p):
                    phrases.add(p)
                    phrases.add(normalize_cache_text(p))

        # Compute valid key set
        valid_keys = set()
        for norm in phrases:
            for k in self._cache_keys(norm):
                valid_keys.add(k)

        removed = 0
        try:
            for f in self.cache_dir.glob("*.npz"):
                key = f.stem
                if key not in valid_keys:
                    try:
                        f.unlink()
                        removed += 1
                    except OSError:
                        pass
            if removed > 0:
                log.info("Cleaned up %d orphaned audio cache files for voice '%s'", removed, self.voice)
        except Exception as e:
            log.warning("Error during audio cache orphan cleanup: %s", e)
        return removed

    def get_stats(self) -> dict[str, Any]:
        """Returns statistics for display in Settings UI."""
        files = list(self.cache_dir.glob("*.npz"))
        total_bytes = sum(f.stat().st_size for f in files if f.is_file())
        return {
            "file_count": len(files),
            "size_bytes": total_bytes,
            "size_mb": round(total_bytes / (1024 * 1024), 2),
            "max_mb": round(self.max_bytes / (1024 * 1024), 1),
            "cache_dir": str(self.cache_dir),
            "voice": self.voice,
        }

    def clear(self) -> tuple[int, int]:
        """Clears all cached audio files.

        Returns:
            (deleted_files_count, deleted_bytes)
        """
        deleted_count = 0
        deleted_bytes = 0
        for f in self.cache_dir.glob("*.npz"):
            try:
                sz = f.stat().st_size
                f.unlink()
                deleted_count += 1
                deleted_bytes += sz
            except OSError:
                pass
        log.info("Cleared audio cache: %d files, %.2f MB freed", deleted_count, deleted_bytes / (1024 * 1024))
        return deleted_count, deleted_bytes
