"""Test executor with durable jobs (A09-1 demonstration; the real CAWbase queue and workers are B01).

- `submit` only writes a durable `pending` job: it never calls the cloud nor waits for the cold start.
- The controller sees the job as demand and starts the host; workers claim only while the controller phase is
  `ready` and below `max_in_flight` (the server slots), so excess work waits durably here, not in server memory.
- Before sending, the worker counts the prompt tokens with the model's own template and rejects explicitly, without
  retry, any input whose prompt plus output budget exceeds the effective context per slot.
- Transient failures are retried with backoff up to `max_attempts`; expired worker leases return to `pending`.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any

from .clock import Clock
from .decide import Demand, dispatch_allowed
from .llama import ContextExceededError, InferenceError, LlamaClient, OutputInvalidError, TransientInferenceError
from .store import Store


@dataclass(frozen=True)
class ExecutorPolicy:
    max_in_flight: int = 1
    max_attempts: int = 5
    job_deadline_s: float = 86_400.0
    inference_timeout_s: float = 180.0
    lease_margin_s: float = 120.0
    retry_backoff_s: float = 30.0
    retry_backoff_max_s: float = 900.0

    @property
    def lease_ttl_s(self) -> float:
        return self.inference_timeout_s + self.lease_margin_s


@dataclass(frozen=True)
class ClaimedJob:
    job_id: str
    lease_token: str
    attempts: int
    payload: dict[str, Any]


class JobQueue:
    def __init__(self, store: Store, clock: Clock, policy: ExecutorPolicy | None = None) -> None:
        self.store = store
        self.clock = clock
        self.policy = policy or ExecutorPolicy()

    # --- demand (read by the controller inside its serialized transaction) ------------------------------------

    @staticmethod
    def demand(conn: sqlite3.Connection, now: float) -> Demand:
        pending = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE deadline_at > ? AND (status = 'pending' OR "
            "(status = 'running' AND lease_expires_at <= ?))",
            (now, now),
        ).fetchone()[0]
        active = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE status = 'running' AND lease_expires_at > ?", (now,)
        ).fetchone()[0]
        last = conn.execute("SELECT MAX(finished_at) FROM jobs").fetchone()[0]
        return Demand(pending=int(pending), active=int(active), last_activity_at=last)

    def demand_now(self) -> Demand:
        conn = self.store.connect()
        try:
            return self.demand(conn, self.clock.now())
        finally:
            conn.close()

    # --- submission --------------------------------------------------------------------------------------------

    def submit(self, payload: dict[str, Any], *, job_id: str | None = None) -> str:
        _validate_payload(payload)
        now = self.clock.now()
        jid = job_id or f"job_{uuid.uuid4().hex}"
        with self.store.tx() as conn:
            conn.execute(
                "INSERT INTO jobs (job_id, status, payload, created_at, available_at, deadline_at, max_attempts) "
                "VALUES (?, 'pending', ?, ?, ?, ?, ?)",
                (jid, json.dumps(payload), now, now, now + self.policy.job_deadline_s, self.policy.max_attempts),
            )
        return jid

    def get(self, job_id: str) -> dict[str, Any] | None:
        conn = self.store.connect()
        try:
            row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        finally:
            conn.close()
        return dict(row) if row is not None else None

    def counts(self) -> dict[str, int]:
        conn = self.store.connect()
        try:
            rows = conn.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status").fetchall()
        finally:
            conn.close()
        return {r["status"]: int(r["n"]) for r in rows}

    # --- recovery ----------------------------------------------------------------------------------------------

    def _recover(self, conn: sqlite3.Connection, now: float) -> None:
        conn.execute(
            "UPDATE jobs SET status = 'failed', finished_at = ?, error_code = 'deadline_exceeded', lease_token = NULL "
            "WHERE status IN ('pending', 'running') AND deadline_at <= ?",
            (now, now),
        )
        conn.execute(
            "UPDATE jobs SET status = 'failed', finished_at = ?, error_code = 'retries_exhausted:lease_lost', "
            "lease_token = NULL WHERE status = 'running' AND lease_expires_at <= ? AND attempts >= max_attempts",
            (now, now),
        )
        conn.execute(
            "UPDATE jobs SET status = 'pending', available_at = ?, lease_token = NULL, worker = NULL, "
            "error_code = 'lease_lost' WHERE status = 'running' AND lease_expires_at <= ?",
            (now, now),
        )

    def recover(self) -> None:
        with self.store.tx() as conn:
            self._recover(conn, self.clock.now())

    # --- claim (gated by the controller phase, serialized with the stop decision) -----------------------------

    def claim(self, worker: str) -> ClaimedJob | None:
        now = self.clock.now()
        with self.store.tx() as conn:
            self._recover(conn, now)
            if not dispatch_allowed(Store.read(conn).state):  # ready and no failed probe since the last verification
                return None
            running = conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE status = 'running' AND lease_expires_at > ?", (now,)
            ).fetchone()[0]
            if running >= self.policy.max_in_flight:
                return None
            row = conn.execute(
                "SELECT job_id, attempts, payload FROM jobs WHERE status = 'pending' AND available_at <= ? "
                "AND deadline_at > ? ORDER BY available_at, created_at LIMIT 1",
                (now, now),
            ).fetchone()
            if row is None:
                return None
            token = secrets.token_hex(16)
            conn.execute(
                "UPDATE jobs SET status = 'running', attempts = attempts + 1, lease_token = ?, lease_expires_at = ?, "
                "worker = ?, started_at = COALESCE(started_at, ?), error_code = NULL WHERE job_id = ?",
                (token, now + self.policy.lease_ttl_s, worker, now, row["job_id"]),
            )
            return ClaimedJob(row["job_id"], token, int(row["attempts"]) + 1, json.loads(row["payload"]))

    # --- owner transitions (fenced by lease token) -------------------------------------------------------------

    def complete(self, job: ClaimedJob, result: dict[str, Any]) -> bool:
        now = self.clock.now()
        with self.store.tx() as conn:
            cur = conn.execute(
                "UPDATE jobs SET status = 'succeeded', finished_at = ?, result = ?, lease_token = NULL "
                "WHERE job_id = ? AND status = 'running' AND lease_token = ? AND lease_expires_at > ?",
                (now, json.dumps(result), job.job_id, job.lease_token, now),
            )
            return cur.rowcount == 1

    def fail(self, job: ClaimedJob, code: str, *, retryable: bool, detail: dict[str, Any] | None = None) -> str:
        now = self.clock.now()
        with self.store.tx() as conn:
            row = conn.execute(
                "SELECT attempts, max_attempts, deadline_at FROM jobs WHERE job_id = ? AND status = 'running' "
                "AND lease_token = ? AND lease_expires_at > ?",
                (job.job_id, job.lease_token, now),
            ).fetchone()
            if row is None:
                return "lease_lost"
            delay = min(self.policy.retry_backoff_s * 2 ** (row["attempts"] - 1), self.policy.retry_backoff_max_s)
            if retryable and row["attempts"] < row["max_attempts"] and now + delay < row["deadline_at"]:
                conn.execute(
                    "UPDATE jobs SET status = 'pending', available_at = ?, lease_token = NULL, worker = NULL, "
                    "error_code = ?, error_detail = ? WHERE job_id = ?",
                    (now + delay, code, json.dumps(detail or {}), job.job_id),
                )
                return "retry_wait"
            final = code if not retryable else f"retries_exhausted:{code}"
            conn.execute(
                "UPDATE jobs SET status = 'failed', finished_at = ?, lease_token = NULL, error_code = ?, "
                "error_detail = ? WHERE job_id = ?",
                (now, final, json.dumps(detail or {}), job.job_id),
            )
            return "failed"


def _validate_payload(payload: dict[str, Any]) -> None:
    if not isinstance(payload.get("messages"), list) or not payload["messages"]:
        raise ValueError("payload.messages must be a non-empty list")
    if not isinstance(payload.get("schema"), dict):
        raise ValueError("payload.schema must be a JSON Schema object")
    mt = payload.get("max_tokens")
    if not isinstance(mt, int) or isinstance(mt, bool) or mt < 1:
        raise ValueError("payload.max_tokens must be a positive integer")


class Worker:
    """Executes claimed jobs against the inference service. One call of `run_once` = at most one job."""

    def __init__(self, queue: JobQueue, client: LlamaClient, *, name: str, model: str | None = None) -> None:
        self.queue = queue
        self.client = client
        self.name = name
        self.model = model

    def run_once(self) -> str | None:
        job = self.queue.claim(self.name)
        if job is None:
            return None
        p = job.payload
        try:
            n_ctx = self.client.slot_context()
            n_prompt = self.client.count_prompt_tokens(p["messages"])
            if n_prompt + p["max_tokens"] > n_ctx:
                detail = {"n_prompt_tokens": n_prompt, "max_tokens": p["max_tokens"], "n_ctx_slot": n_ctx}
                return self.queue.fail(job, "input_exceeds_context", retryable=False, detail=detail)
            res = self.client.chat_structured(
                p["messages"],
                p["schema"],
                max_tokens=p["max_tokens"],
                model=self.model,
                timeout_s=self.queue.policy.inference_timeout_s,
            )
        except ContextExceededError as exc:
            return self.queue.fail(job, exc.code, retryable=False, detail=exc.detail)
        except TransientInferenceError as exc:
            return self.queue.fail(job, exc.code, retryable=True)
        except OutputInvalidError as exc:
            return self.queue.fail(job, f"output_invalid:{exc.code}", retryable=False)
        except InferenceError as exc:  # pragma: no cover - closed hierarchy
            return self.queue.fail(job, exc.code, retryable=False)
        ok = self.queue.complete(job, {"output": res["output"], "usage": res["usage"], "timings": res["timings"]})
        return "succeeded" if ok else "lease_lost"
