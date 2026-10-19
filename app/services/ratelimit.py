"""Minimal in-memory sliding-window rate limiter (stdlib only).

Scope notes, on purpose:
- Per-instance state: resets on restart, not shared between workers or
  replicas. Sufficient for a single-process deployment; a multi-worker
  deployment needs a shared store (Redis) — Phase 3 material.
- Keyed by client IP. Behind a reverse proxy every caller may share the
  proxy's IP, making the limit stricter than configured — safe direction.
"""
import threading
import time


class SlidingWindowLimiter:
    def __init__(self, limit: int, window_seconds: float = 60.0, clock=time.monotonic):
        self.limit = limit
        self.window = window_seconds
        self.clock = clock
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = self.clock()
        with self._lock:
            recent = [t for t in self._hits.get(key, []) if now - t < self.window]
            if len(recent) >= self.limit:
                self._hits[key] = recent
                return False
            recent.append(now)
            self._hits[key] = recent
            return True
