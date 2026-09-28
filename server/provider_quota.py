"""Persistent, conservative request budget for metered providers.

Reservations use a rolling 24-hour window. This never exceeds a 100/day
allowance regardless of the upstream service's calendar-day reset timezone.
The write lock is acquired before reading the count, including across Store
instances pointed at the same SQLite database.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


WINDOW = timedelta(hours=24)
MICROSECONDS = 1_000_000


@dataclass(frozen=True)
class Reservation:
    allowed: bool
    remaining: int
    next_available_at: str = ""


class DailyQuota:
    def __init__(self, store, provider_id: str, limit: int = 100):
        if store is None or not hasattr(store, "atomic"):
            raise ValueError("Persistent Store required for provider quota")
        if not provider_id or not 1 <= int(limit) <= 100:
            raise ValueError("Invalid provider quota")
        self.store = store
        self.provider_id = provider_id
        self.limit = int(limit)
        with store.atomic():
            store._db.execute("""CREATE TABLE IF NOT EXISTS provider_quota_reservations (
                id INTEGER PRIMARY KEY, provider_id TEXT NOT NULL, reserved_us INTEGER NOT NULL)""")
            store._db.execute("""CREATE INDEX IF NOT EXISTS provider_quota_window
                ON provider_quota_reservations(provider_id, reserved_us)""")
            store._db.execute("""CREATE TABLE IF NOT EXISTS provider_quota_pauses (
                provider_id TEXT PRIMARY KEY, until_us INTEGER NOT NULL)""")

    def reserve(self, *, now: datetime | None = None) -> Reservation:
        if self.store._transaction_depth:
            # An outer transaction could later roll back after network I/O.
            raise RuntimeError("Quota reservation requires its own committed transaction")
        moment = _utc(now)
        stamp = _micros(moment)
        cutoff = _micros(moment - WINDOW)
        with self.store.atomic():
            # The first statement writes, so SQLite serializes competing Store
            # connections before either one reads the count.
            self.store._db.execute(
                "DELETE FROM provider_quota_reservations WHERE provider_id=? AND reserved_us<=?",
                (self.provider_id, cutoff),
            )
            pause = self.store._db.execute(
                "SELECT until_us FROM provider_quota_pauses WHERE provider_id=?",
                (self.provider_id,),
            ).fetchone()
            rows = self.store._db.execute(
                "SELECT COUNT(*), MIN(reserved_us) FROM provider_quota_reservations WHERE provider_id=?",
                (self.provider_id,),
            ).fetchone()
            count, oldest = rows
            if pause and pause[0] > stamp:
                return Reservation(False, max(0, self.limit - count), _iso(pause[0]))
            if count >= self.limit:
                return Reservation(False, 0, _iso(oldest + _micros(WINDOW)))
            self.store._db.execute(
                "INSERT INTO provider_quota_reservations(provider_id,reserved_us) VALUES(?,?)",
                (self.provider_id, stamp),
            )
            return Reservation(True, self.limit - count - 1)

    def status(self, *, now: datetime | None = None) -> dict:
        """Read-only state suitable for a data-source settings response."""
        moment = _utc(now)
        stamp = _micros(moment)
        cutoff = _micros(moment - WINDOW)
        with self.store.lock:
            count, oldest = self.store._db.execute(
                """SELECT COUNT(*), MIN(reserved_us) FROM provider_quota_reservations
                   WHERE provider_id=? AND reserved_us>?""",
                (self.provider_id, cutoff),
            ).fetchone()
            pause = self.store._db.execute(
                "SELECT until_us FROM provider_quota_pauses WHERE provider_id=?",
                (self.provider_id,),
            ).fetchone()
        next_us = pause[0] if pause and pause[0] > stamp else 0
        if count >= self.limit and oldest is not None:
            next_us = max(next_us, oldest + _micros(WINDOW))
        return {"limit": self.limit, "used": count, "remaining": max(0, self.limit - count),
                "window_hours": 24, "next_available_at": _iso(next_us) if next_us else ""}

    def pause_for_rate_limit(self, *, now: datetime | None = None,
                             duration: timedelta = timedelta(minutes=1)) -> str:
        """Pause for the reported window; unknown 429 uses a short cooldown.

        The rolling local budget still caps calls at 100 per 24 hours even if
        upstream limits are shared with another client or key installation.
        """
        if duration <= timedelta(0) or duration > WINDOW:
            raise ValueError("Invalid rate-limit cooldown")
        until = _micros(_utc(now) + duration)
        with self.store.atomic():
            self.store._db.execute("""INSERT INTO provider_quota_pauses(provider_id,until_us)
                VALUES(?,?) ON CONFLICT(provider_id) DO UPDATE SET
                until_us=MAX(provider_quota_pauses.until_us,excluded.until_us)""",
                (self.provider_id, until),
            )
            actual = self.store._db.execute(
                "SELECT until_us FROM provider_quota_pauses WHERE provider_id=?",
                (self.provider_id,),
            ).fetchone()[0]
        return _iso(actual)


def _utc(value: datetime | None) -> datetime:
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None:
        raise ValueError("Quota time must include a timezone")
    return value.astimezone(timezone.utc)


def _micros(value: datetime | timedelta) -> int:
    if isinstance(value, timedelta):
        return round(value.total_seconds() * MICROSECONDS)
    return round(value.timestamp() * MICROSECONDS)


def _iso(stamp: int) -> str:
    return datetime.fromtimestamp(stamp / MICROSECONDS, timezone.utc).isoformat()
