"""Lightweight in-process counters, logged periodically to stdout.

Railway keeps stdout, so a single heartbeat line every minute is enough to tell
whether MatchX is healthy during a hackathon: how much work it is doing, how slow
the database is, and whether it is shedding load or failing to reach Telegram.

Deliberately not a metrics system: no server, no scrape endpoint, no dependency.
Counters are process-local and reset on restart, which is fine — they answer
"is it healthy right now", not "what happened last Tuesday".
"""

from __future__ import annotations

import threading
import time
from collections import Counter


class Stats:
    """Thread-safe counters. Written from executor threads, read by the event loop."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._query_count = 0
        self._query_seconds = 0.0
        self._query_slowest = 0.0
        self._pool_timeouts = 0
        self._db_errors = 0
        self._actions: Counter[str] = Counter()
        self._telegram: Counter[str] = Counter()
        self._since = time.monotonic()

    # ---- database -------------------------------------------------------
    def record_query(self, seconds: float) -> None:
        with self._lock:
            self._query_count += 1
            self._query_seconds += seconds
            if seconds > self._query_slowest:
                self._query_slowest = seconds

    def record_pool_timeout(self) -> None:
        with self._lock:
            self._pool_timeouts += 1

    def record_error(self) -> None:
        with self._lock:
            self._db_errors += 1

    # ---- product / Telegram ---------------------------------------------
    def record_action(self, name: str) -> None:
        with self._lock:
            self._actions[name] += 1

    def record_telegram(self, outcome: str) -> None:
        """outcome: 'sent', 'retry_after', 'blocked', 'failed'."""
        with self._lock:
            self._telegram[outcome] += 1

    # ---- reporting -------------------------------------------------------
    def snapshot_and_reset(self) -> dict[str, object]:
        with self._lock:
            window = max(time.monotonic() - self._since, 1e-9)
            count = self._query_count
            snap: dict[str, object] = {
                "window_s": round(window, 1),
                "queries": count,
                "queries_per_s": round(count / window, 1),
                "db_avg_ms": round(self._query_seconds / count * 1000, 1) if count else 0.0,
                "db_slowest_ms": round(self._query_slowest * 1000, 1),
                "pool_timeouts": self._pool_timeouts,
                "db_errors": self._db_errors,
                "actions": dict(self._actions),
                "telegram": dict(self._telegram),
            }
            self._query_count = 0
            self._query_seconds = 0.0
            self._query_slowest = 0.0
            self._pool_timeouts = 0
            self._db_errors = 0
            self._actions.clear()
            self._telegram.clear()
            self._since = time.monotonic()
            return snap

    def format_heartbeat(self, pool_stats: dict[str, object] | None = None) -> str:
        snap = self.snapshot_and_reset()
        parts = [
            f"queries={snap['queries']} ({snap['queries_per_s']}/s)",
            f"db_avg={snap['db_avg_ms']}ms",
            f"db_max={snap['db_slowest_ms']}ms",
        ]
        if snap["pool_timeouts"]:
            parts.append(f"POOL_TIMEOUTS={snap['pool_timeouts']}")
        if snap["db_errors"]:
            parts.append(f"db_errors={snap['db_errors']}")
        if snap["actions"]:
            parts.append("actions=" + ",".join(f"{k}:{v}" for k, v in sorted(snap["actions"].items())))  # type: ignore[union-attr]
        if snap["telegram"]:
            parts.append("telegram=" + ",".join(f"{k}:{v}" for k, v in sorted(snap["telegram"].items())))  # type: ignore[union-attr]
        if pool_stats:
            busy = pool_stats.get("pool_size", 0)
            waiting = pool_stats.get("requests_waiting", 0)
            parts.append(f"pool={busy} waiting={waiting}")
        return " | ".join(parts)


STATS = Stats()
