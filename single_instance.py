"""Single-instance lock manager for race_engineer.

Ensures only one instance runs at a time across CLI, desktop app, and batch scripts.
Uses OS file locking (msvcrt on Windows, fcntl on Unix/macOS) so locks automatically
release even if an instance crashes or is killed ungracefully.
"""
from __future__ import annotations

import atexit
import logging
import os
import sys
from pathlib import Path
from typing import Optional

log = logging.getLogger("engineer.instance")


def get_default_app_dir() -> Path:
    """Returns the platform-standard application data / cache directory."""
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            base = Path(local_app_data) / "RaceEngineer"
        else:
            base = Path.home() / "AppData" / "Local" / "RaceEngineer"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches" / "RaceEngineer"
    else:
        xdg = os.environ.get("XDG_CACHE_HOME")
        if xdg:
            base = Path(xdg) / "race_engineer"
        else:
            base = Path.home() / ".cache" / "race_engineer"
    base.mkdir(parents=True, exist_ok=True)
    return base


class AcquireResult(tuple):
    """Tuple of (acquired: bool, pid: int | None) that also evaluates directly as a boolean."""
    def __new__(cls, acquired: bool, pid: Optional[int] = None):
        return super().__new__(cls, (acquired, pid))

    @property
    def acquired(self) -> bool:
        return self[0]

    @property
    def pid(self) -> Optional[int]:
        return self[1]

    def __bool__(self) -> bool:
        return bool(self[0])

    def __eq__(self, other):
        if isinstance(other, bool):
            return self[0] == other
        return super().__eq__(other)


class SingleInstanceLock:
    """Acquires a process-lifetime lock on race_engineer.lock."""

    def __init__(self, lock_file: Optional[Path] = None):
        self.lock_path = lock_file or (get_default_app_dir() / "race_engineer.lock")
        self._fd: Optional[int] = None
        self._locked = False

    def get_owner_pid(self) -> Optional[int]:
        """Returns the PID of the process currently holding the lock, if recorded."""
        return self._read_recorded_pid()

    def acquire(self) -> AcquireResult:
        """Attempts to acquire exclusive lock.

        Returns:
            AcquireResult(True, None) if lock acquired.
            AcquireResult(False, pid) if another active instance already holds the lock.
        """
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            # Open for reading and writing (or create)
            flags = os.O_RDWR | os.O_CREAT
            if hasattr(os, "O_CLOEXEC"):
                flags |= os.O_CLOEXEC
            self._fd = os.open(str(self.lock_path), flags, 0o644)
        except Exception as e:
            log.warning("Could not open single-instance lock file %s: %s", self.lock_path, e)
            return AcquireResult(True, None)

        locked = False
        if sys.platform == "win32":
            import msvcrt
            try:
                # Lock byte 0
                msvcrt.locking(self._fd, msvcrt.LK_NBLCK, 1)
                locked = True
            except OSError:
                locked = False
        else:
            import fcntl
            try:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except (OSError, BlockingIOError):
                locked = False

        if not locked:
            # Another process holds the lock. Read the recorded PID.
            other_pid = self._read_recorded_pid()
            try:
                os.close(self._fd)
            except Exception:
                pass
            self._fd = None
            return AcquireResult(False, other_pid)

        # Lock successfully acquired. Write our current PID.
        try:
            os.ftruncate(self._fd, 0)
            os.lseek(self._fd, 0, os.SEEK_SET)
            os.write(self._fd, f"{os.getpid()}\n".encode("utf-8"))
            os.fsync(self._fd)
        except Exception as e:
            log.debug("Could not write PID to lock file: %s", e)

        self._locked = True
        atexit.register(self.release)
        return AcquireResult(True, None)

    def _read_recorded_pid(self) -> Optional[int]:
        try:
            content = self.lock_path.read_text(encoding="utf-8").strip()
            if content and content.isdigit():
                return int(content)
        except Exception:
            pass
        return None

    def release(self) -> None:
        """Releases the lock and cleans up file descriptor."""
        if not self._locked or self._fd is None:
            return

        try:
            if sys.platform == "win32":
                import msvcrt
                try:
                    os.lseek(self._fd, 0, os.SEEK_SET)
                    msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
                except Exception:
                    pass
            else:
                import fcntl
                try:
                    fcntl.flock(self._fd, fcntl.LOCK_UN)
                except Exception:
                    pass

            os.close(self._fd)
        except Exception:
            pass
        finally:
            self._fd = None
            self._locked = False
            try:
                if self.lock_path.exists():
                    self.lock_path.unlink()
            except Exception:
                pass
