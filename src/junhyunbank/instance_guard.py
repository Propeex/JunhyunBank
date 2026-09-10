from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QLockFile


class InstanceGuard:
    """Process-level guard preventing two live traders on the same profile."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = QLockFile(str(path))
        # This is a long-lived application lock. With zero stale timeout Qt uses
        # PID/process-name checks rather than expiring a healthy long session.
        self._lock.setStaleLockTime(0)

    def acquire(self) -> bool:
        return bool(self._lock.tryLock(0))

    def release(self) -> None:
        if self._lock.isLocked():
            self._lock.unlock()

    @property
    def locked(self) -> bool:
        return bool(self._lock.isLocked())
