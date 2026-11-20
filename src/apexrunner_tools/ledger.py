"""Shared state for local risk limits: the rolling 24 h volume ledger and the stop latch.

Both live in one SQLite file (``RiskLimits.state_path``). Every shield, in any
process or for any venue, that points at the same file shares one 24 h volume
total and one stop. Each operation opens its own connection and takes SQLite's
write lock (``BEGIN IMMEDIATE``) for the whole read-check-write, so two agents
cannot both read "$600 used" and both add $500 against a $1,000 cap.

Amounts are stored as integer micro-dollars so boundary comparisons are exact.

Anything wrong with the file (not a database, wrong schema version, locked past
the timeout, unreadable) raises ``LedgerError``. RiskShield treats that as a
blocked trade: it never guesses at state it cannot read.

Deleting the file resets both the volume total and the stop. That is the
"client-side, not tamper-proof" property, stated plainly.
"""

from __future__ import annotations

import math
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

SCHEMA_VERSION = "1"
WINDOW_SECONDS = 24 * 60 * 60
_MICRO = 1_000_000
_PRUNE_AFTER = 7 * 24 * 60 * 60

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS volume (
    id      INTEGER PRIMARY KEY,
    ts      REAL    NOT NULL,
    micro   INTEGER NOT NULL CHECK (micro > 0),
    state   TEXT    NOT NULL CHECK (state IN ('reserved', 'committed', 'unknown')),
    coin    TEXT,
    side    TEXT
);
CREATE INDEX IF NOT EXISTS volume_ts ON volume (ts);
CREATE TABLE IF NOT EXISTS stop_latch (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    baseline_usd    REAL NOT NULL CHECK (baseline_usd > 0),
    armed_at        REAL NOT NULL,
    tripped_at      REAL,
    tripped_equity  REAL,
    tripped_loss    REAL
);
"""


class LedgerError(Exception):
    """The risk state file could not be read or written. Trades must be blocked."""


def to_micro(usd: float) -> int:
    return round(usd * _MICRO)


def from_micro(micro: int) -> float:
    return micro / _MICRO


class _Store:
    def __init__(self, path: str | Path, *, clock: Callable[[], float] = time.time, timeout: float = 10.0):
        self.path = Path(path).expanduser()
        self.clock = clock
        self.timeout = timeout
        self._ready = False

    @contextmanager
    def _txn(self) -> Iterator[sqlite3.Connection]:
        """One connection, one IMMEDIATE transaction. Any SQLite or OS failure -> LedgerError."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.path, timeout=self.timeout, isolation_level=None)
        except (OSError, sqlite3.Error) as exc:
            raise LedgerError(f"cannot open risk state {self.path}: {exc}") from exc
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._init(conn)
            yield conn
            conn.execute("COMMIT")
        except sqlite3.Error as exc:
            _rollback(conn)
            raise LedgerError(f"risk state {self.path} unusable: {exc}") from exc
        except BaseException:
            _rollback(conn)
            raise
        finally:
            conn.close()

    def _init(self, conn: sqlite3.Connection) -> None:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not tables:
            if self._ready:
                # It existed earlier in this process. Recreating it would silently zero the
                # 24 h volume and disarm the stop, so refuse until the process restarts.
                raise LedgerError(f"risk state {self.path} was deleted or emptied while running; refusing to reset it")
            for stmt in filter(str.strip, _SCHEMA.split(";")):
                conn.execute(stmt)
            conn.execute("INSERT INTO meta VALUES ('schema_version', ?)", (SCHEMA_VERSION,))
        else:
            row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone() \
                if "meta" in tables else None
            if row is None or row[0] != SCHEMA_VERSION or not {"volume", "stop_latch"} <= tables:
                raise LedgerError(
                    f"risk state {self.path} is not an apexrunner-tools ledger (schema "
                    f"{row[0] if row else 'missing'}, expected {SCHEMA_VERSION}); refusing to use it")
        self._ready = True


def _rollback(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass


@dataclass(frozen=True)
class Reservation:
    id: int
    usd: float
    #: 24 h volume before this trade.
    used_before_usd: float


class VolumeExceeded(Exception):
    def __init__(self, used_usd: float, usd: float, limit_usd: float):
        super().__init__(f"${used_usd:,.2f} already traded in the last 24 h; ${usd:,.2f} more "
                         f"would exceed the ${limit_usd:,.2f} 24 h limit")
        self.used_usd, self.usd, self.limit_usd = used_usd, usd, limit_usd


class VolumeLedger(_Store):
    """Rolling 24 h traded volume. Reserve before the order, then commit or release.

    Reserved, committed and unknown rows all count toward the total: a trade is
    counted from the moment it is allowed, not from when it fills.
    """

    #: Test hook, called inside the locked transaction between the read and the write.
    _race_hook: Callable[[], None] | None = None

    def used_usd(self) -> float:
        with self._txn() as conn:
            return from_micro(self._used(conn, self.clock()))

    def reserve(self, usd: float, limit_usd: float, *, coin: object = None, side: object = None) -> Reservation:
        """Atomically: fail with VolumeExceeded, or record ``usd`` against the window."""
        if not (isinstance(usd, (int, float)) and not isinstance(usd, bool) and math.isfinite(usd) and usd > 0):
            raise ValueError(f"trade size must be a positive number, got {usd!r}")
        micro, limit = to_micro(usd), to_micro(limit_usd)
        with self._txn() as conn:
            now = self.clock()
            conn.execute("DELETE FROM volume WHERE ts <= ?", (now - _PRUNE_AFTER,))
            used = self._used(conn, now)
            if self._race_hook is not None:
                self._race_hook()
            if used + micro > limit:
                raise VolumeExceeded(from_micro(used), usd, limit_usd)
            cur = conn.execute("INSERT INTO volume (ts, micro, state, coin, side) VALUES (?, ?, 'reserved', ?, ?)",
                               (now, micro, _text(coin), _text(side)))
            return Reservation(id=int(cur.lastrowid), usd=usd, used_before_usd=from_micro(used))

    def record(self, usd: float, *, coin: object = None, side: object = None) -> None:
        """Count a trade that already happened outside ``reserve`` (never refused)."""
        if not (isinstance(usd, (int, float)) and not isinstance(usd, bool) and math.isfinite(usd) and usd > 0):
            raise ValueError(f"trade size must be a positive number, got {usd!r}")
        with self._txn() as conn:
            conn.execute("INSERT INTO volume (ts, micro, state, coin, side) VALUES (?, ?, 'committed', ?, ?)",
                         (self.clock(), to_micro(usd), _text(coin), _text(side)))

    def commit(self, res: Reservation) -> None:
        """The order was placed."""
        self._settle(res, "committed")

    def mark_unknown(self, res: Reservation) -> None:
        """The order function raised: it may or may not have reached the venue. Keep counting it."""
        self._settle(res, "unknown")

    def release(self, res: Reservation) -> None:
        """The trade was blocked after reserving and never reached the venue."""
        with self._txn() as conn:
            conn.execute("DELETE FROM volume WHERE id = ? AND state = 'reserved'", (res.id,))

    def _settle(self, res: Reservation, state: str) -> None:
        with self._txn() as conn:
            conn.execute("UPDATE volume SET state = ? WHERE id = ?", (state, res.id))

    @staticmethod
    def _used(conn: sqlite3.Connection, now: float) -> int:
        row = conn.execute("SELECT COALESCE(SUM(micro), 0) FROM volume WHERE ts > ?", (now - WINDOW_SECONDS,)).fetchone()
        return int(row[0])


@dataclass(frozen=True)
class StopState:
    baseline_usd: float
    armed_at: float
    tripped: bool
    #: Loss from baseline in percent at the last evaluation (positive = down).
    loss_pct: float
    tripped_at: float | None = None


class StopLatch(_Store):
    """Portfolio auto-stop. Arms with a baseline equity; trips when equity is down more
    than ``stop_pct`` from it; stays tripped (across restarts) until ``reset``."""

    def evaluate(self, equity_usd: float, stop_pct: float) -> StopState:
        _check_equity(equity_usd)
        with self._txn() as conn:
            now = self.clock()
            row = conn.execute("SELECT baseline_usd, armed_at, tripped_at, tripped_loss FROM stop_latch WHERE id=1").fetchone()
            if row is None:
                if equity_usd <= 0:
                    raise ValueError("cannot arm the portfolio stop with zero equity")
                conn.execute("INSERT INTO stop_latch (id, baseline_usd, armed_at) VALUES (1, ?, ?)", (equity_usd, now))
                return StopState(baseline_usd=equity_usd, armed_at=now, tripped=False, loss_pct=0.0)
            baseline, armed_at, tripped_at, tripped_loss = row
            if tripped_at is not None:
                return StopState(baseline, armed_at, True, float(tripped_loss), tripped_at)
            loss = (baseline - equity_usd) / baseline * 100
            if loss > stop_pct + 1e-9:  # "exceeds"; tolerance so float noise at exactly -N% does not trip
                conn.execute("UPDATE stop_latch SET tripped_at=?, tripped_equity=?, tripped_loss=? WHERE id=1",
                             (now, equity_usd, loss))
                return StopState(baseline, armed_at, True, loss, now)
            return StopState(baseline, armed_at, False, loss)

    def reset(self, equity_usd: float) -> StopState:
        """Re-arm with ``equity_usd`` as the new baseline and clear any trip."""
        _check_equity(equity_usd)
        if equity_usd <= 0:
            raise ValueError("cannot arm the portfolio stop with zero equity")
        with self._txn() as conn:
            now = self.clock()
            conn.execute("INSERT OR REPLACE INTO stop_latch (id, baseline_usd, armed_at) VALUES (1, ?, ?)",
                         (equity_usd, now))
            return StopState(baseline_usd=equity_usd, armed_at=now, tripped=False, loss_pct=0.0)

    def status(self) -> StopState | None:
        """The stored state, or None if not armed yet. Does not evaluate."""
        with self._txn() as conn:
            row = conn.execute("SELECT baseline_usd, armed_at, tripped_at, tripped_loss FROM stop_latch WHERE id=1").fetchone()
        if row is None:
            return None
        baseline, armed_at, tripped_at, tripped_loss = row
        return StopState(baseline, armed_at, tripped_at is not None, float(tripped_loss or 0.0), tripped_at)


def _check_equity(equity_usd: object) -> None:
    if isinstance(equity_usd, bool) or not isinstance(equity_usd, (int, float)) \
            or not math.isfinite(equity_usd) or equity_usd < 0:
        raise ValueError(f"equity must be a finite number >= 0, got {equity_usd!r}")


def _text(v: object) -> str | None:
    return None if v is None else str(v)[:64]


__all__ = ["LedgerError", "Reservation", "StopLatch", "StopState", "VolumeExceeded", "VolumeLedger"]
