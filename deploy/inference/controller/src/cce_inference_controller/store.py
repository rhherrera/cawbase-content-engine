"""Durable controller state (SQLite, WAL, `BEGIN IMMEDIATE` write transactions).

One row holds the desired state, its generation and the leader lease. Every write transaction is serialized by
SQLite, so a decision that reads the demand and changes the phase (for example the idle stop) and a worker claim
that requires `phase = ready` are linearizable: no work can be claimed on a host the controller decided to stop.

The test executor keeps its durable jobs in the same database file (see `executor.py`). A production queue (B01)
must give the same guarantee: the stop decision and the claim must be serialized against each other.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .decide import ControlState, Event

SCHEMA = """
CREATE TABLE IF NOT EXISTS ctl_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    data TEXT NOT NULL,
    generation INTEGER NOT NULL,
    lease_owner TEXT,
    lease_expires_at REAL
);
CREATE TABLE IF NOT EXISTS ctl_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    generation INTEGER NOT NULL,
    kind TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'succeeded', 'failed')),
    payload TEXT NOT NULL,
    created_at REAL NOT NULL,
    available_at REAL NOT NULL,
    deadline_at REAL NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL,
    lease_token TEXT,
    lease_expires_at REAL,
    worker TEXT,
    started_at REAL,
    finished_at REAL,
    result TEXT,
    error_code TEXT,
    error_detail TEXT
);
CREATE INDEX IF NOT EXISTS jobs_status_available ON jobs (status, available_at);
"""


@dataclass(frozen=True)
class StateRow:
    state: ControlState
    generation: int
    lease_owner: str | None
    lease_expires_at: float | None


class Store:
    def __init__(self, path: str | Path, *, busy_timeout_s: float = 30.0) -> None:
        self.path = str(path)
        self._busy_timeout_s = busy_timeout_s

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=self._busy_timeout_s, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def initialize(self, now: float) -> None:
        with self.tx() as conn:
            for stmt in [s for s in SCHEMA.split(";") if s.strip()]:
                conn.execute(stmt)
            row = conn.execute("SELECT 1 FROM ctl_state WHERE id = 1").fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO ctl_state (id, data, generation) VALUES (1, ?, 0)",
                    (ControlState(phase_since=now).to_json(),),
                )

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """A serialized write transaction. Commits on success, rolls back on any exception."""
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            conn.close()

    # --- state -------------------------------------------------------------------------------------------------

    @staticmethod
    def read(conn: sqlite3.Connection) -> StateRow:
        row = conn.execute(
            "SELECT data, generation, lease_owner, lease_expires_at FROM ctl_state WHERE id = 1"
        ).fetchone()
        if row is None:
            raise RuntimeError("controller store is not initialized")
        return StateRow(
            ControlState.from_json(row["data"]), row["generation"], row["lease_owner"], row["lease_expires_at"]
        )

    def snapshot(self) -> StateRow:
        conn = self.connect()
        try:
            return self.read(conn)
        finally:
            conn.close()

    @staticmethod
    def write(conn: sqlite3.Connection, state: ControlState, expected_generation: int) -> int:
        cur = conn.execute(
            "UPDATE ctl_state SET data = ?, generation = generation + 1 WHERE id = 1 AND generation = ?",
            (state.to_json(), expected_generation),
        )
        if cur.rowcount != 1:
            raise RuntimeError("generation changed inside a serialized transaction")
        return expected_generation + 1

    @staticmethod
    def append_events(conn: sqlite3.Connection, now: float, generation: int, events: tuple[Event, ...]) -> None:
        for kind, data in events:
            conn.execute(
                "INSERT INTO ctl_events (ts, generation, kind, data) VALUES (?, ?, ?, ?)",
                (now, generation, kind, json.dumps(data, sort_keys=True)),
            )

    def events(self, limit: int = 50) -> list[dict[str, Any]]:
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT seq, ts, generation, kind, data FROM ctl_events ORDER BY seq DESC LIMIT ?", (limit,)
            ).fetchall()
        finally:
            conn.close()
        return [
            {"seq": r["seq"], "ts": r["ts"], "generation": r["generation"], "kind": r["kind"], **json.loads(r["data"])}
            for r in reversed(rows)
        ]

    # --- leader lease ------------------------------------------------------------------------------------------

    @staticmethod
    def take_lease(conn: sqlite3.Connection, owner: str, now: float, ttl_s: float) -> bool:
        """Acquire or renew the leader lease. A holder keeps it while nobody else took it (fencing by owner); a
        different owner can take it only after it expired."""
        row = conn.execute("SELECT lease_owner, lease_expires_at FROM ctl_state WHERE id = 1").fetchone()
        holder, expires = row["lease_owner"], row["lease_expires_at"]
        if holder not in (None, owner) and expires is not None and expires > now:
            return False
        conn.execute("UPDATE ctl_state SET lease_owner = ?, lease_expires_at = ? WHERE id = 1", (owner, now + ttl_s))
        return True

    def release_lease(self, owner: str) -> None:
        with self.tx() as conn:
            conn.execute(
                "UPDATE ctl_state SET lease_owner = NULL, lease_expires_at = NULL WHERE id = 1 AND lease_owner = ?",
                (owner,),
            )
