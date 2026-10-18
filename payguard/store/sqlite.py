"""SQLite repository (build-night default). WAL mode + busy_timeout 5000 ms (review S7)."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from payguard.models import (
    BUDGET_STATES,
    IN_FLIGHT,
    Evidence,
    Finding,
    FulfillmentStatus,
    PaymentStatus,
    Purchase,
    assert_transition,
    new_id,
    now,
)
from payguard.store.base import DuplicateKeyError

SCHEMA = """
CREATE TABLE IF NOT EXISTS purchases (
  purchase_id TEXT PRIMARY KEY,
  principal_id TEXT NOT NULL,
  task_id TEXT,
  purchase_intent_id TEXT NOT NULL,
  request_fingerprint TEXT NOT NULL,
  attempt INTEGER NOT NULL,
  supersedes_purchase_id TEXT,
  approved_by TEXT,
  service_id TEXT NOT NULL,
  args TEXT NOT NULL,
  quoted_amount_minor INTEGER,
  authorized_amount_minor INTEGER,
  asset TEXT,
  network TEXT NOT NULL,
  pay_to TEXT,
  payment_status TEXT NOT NULL,
  fulfillment_status TEXT NOT NULL,
  settlement_tx TEXT,
  valid_before INTEGER,
  artifact_hash TEXT,
  reject_reason TEXT,
  history TEXT NOT NULL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  UNIQUE (principal_id, purchase_intent_id, request_fingerprint, attempt)
);
CREATE INDEX IF NOT EXISTS idx_purchases_fp ON purchases (principal_id, request_fingerprint, created_at);
CREATE TABLE IF NOT EXISTS evidence (
  evidence_id TEXT PRIMARY KEY,
  purchase_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  payload TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evidence_purchase ON evidence (purchase_id, created_at);
CREATE TABLE IF NOT EXISTS findings (
  finding_id TEXT PRIMARY KEY,
  purchase_id TEXT NOT NULL,
  code TEXT NOT NULL,
  severity TEXT NOT NULL,
  summary TEXT NOT NULL,
  evidence_ids TEXT NOT NULL,
  safe_next_actions TEXT NOT NULL,
  created_at REAL NOT NULL,
  resolved_at REAL
);
CREATE INDEX IF NOT EXISTS idx_findings_purchase ON findings (purchase_id);
CREATE TABLE IF NOT EXISTS wallet_audit (
  principal_id TEXT NOT NULL,
  network TEXT NOT NULL,
  state TEXT NOT NULL,
  updated_at REAL NOT NULL,
  PRIMARY KEY (principal_id, network)
);
"""

PURCHASE_COLS = (
    "purchase_id", "principal_id", "task_id", "purchase_intent_id", "request_fingerprint", "attempt",
    "supersedes_purchase_id", "approved_by", "service_id", "args", "quoted_amount_minor",
    "authorized_amount_minor", "asset", "network", "pay_to", "payment_status", "fulfillment_status",
    "settlement_tx", "valid_before", "artifact_hash", "reject_reason", "history", "created_at", "updated_at",
)
JSON_COLS = {"args", "history"}
PROTECTED_COLS = {
    "purchase_id", "payment_status", "history", "principal_id", "request_fingerprint",
    "purchase_intent_id", "attempt", "service_id", "created_at",
}
FULFILLMENT_PROTECTED = PROTECTED_COLS | {"fulfillment_status", "authorized_amount_minor"}


def _enc(col: str, v: Any) -> Any:
    if col in JSON_COLS:
        return json.dumps(v)
    if isinstance(v, (dict, list)):
        raise TypeError(f"column {col!r} does not accept {type(v).__name__}")
    if isinstance(v, (PaymentStatus, FulfillmentStatus)):
        return str(v)
    return v


def _row(r: sqlite3.Row) -> Purchase:
    d = {k: r[k] for k in PURCHASE_COLS}
    d["args"] = json.loads(d["args"])
    d["history"] = json.loads(d["history"])
    d["payment_status"] = PaymentStatus(d["payment_status"])
    d["fulfillment_status"] = FulfillmentStatus(d["fulfillment_status"])
    return Purchase(**d)


def _finding(r: sqlite3.Row) -> Finding:
    return Finding(
        finding_id=r["finding_id"], purchase_id=r["purchase_id"], code=r["code"], severity=r["severity"],
        summary=r["summary"], evidence_ids=json.loads(r["evidence_ids"]),
        safe_next_actions=json.loads(r["safe_next_actions"]), created_at=r["created_at"], resolved_at=r["resolved_at"],
    )


class _Res:
    """Rows and rowcount captured while the connection lock was held."""

    def __init__(self, rows: list[Any], rowcount: int) -> None:
        self._rows = rows
        self.rowcount = rowcount

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[Any]:
        return self._rows


class SqliteRepository:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        # isolation_level=None -> autocommit; every UPDATE is its own atomic transaction.
        # timeout=5 and busy_timeout BEFORE the WAL pragma: Claude Desktop spawns the server twice at once
        # (chat app + Code bridge), and the second process must wait for the first one's schema/recovery
        # write instead of dying with "database is locked" (review S7).
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=5.0)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=5000")
        self._set_wal()
        self.conn.executescript(SCHEMA)
        # One shared connection: serialize execute + fetch/rowcount so threads cannot interleave.
        self._lock = threading.RLock()

    def _set_wal(self) -> None:
        """Switch to WAL, tolerating a concurrent opener: the journal-mode change needs a brief exclusive
        lock, so retry past the busy timeout, and accept 'wal' already set by the other process."""
        last: Exception | None = None
        for _ in range(3):
            try:
                mode = self.conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
                if str(mode).lower() == "wal":
                    return
                raise sqlite3.OperationalError(f"journal_mode is {mode!r}, expected wal")
            except sqlite3.OperationalError as e:  # "database is locked" while the other process writes
                last = e
                time.sleep(0.2)
        current = self.conn.execute("PRAGMA journal_mode").fetchone()[0]
        if str(current).lower() != "wal":
            raise last or sqlite3.OperationalError("could not enable WAL")

    def _q(self, sql: str, params: Any = ()) -> _Res:
        with self._lock:
            cur = self.conn.execute(sql, params)
            rows = cur.fetchall() if cur.description else []
            return _Res(rows, cur.rowcount)

    # ---- purchases -------------------------------------------------------
    def create_purchase(self, row: Purchase) -> Purchase:
        d = row.to_dict()
        cols = ", ".join(PURCHASE_COLS)
        marks = ", ".join("?" for _ in PURCHASE_COLS)
        try:
            self._q(f"INSERT INTO purchases ({cols}) VALUES ({marks})",
                              [_enc(c, d[c]) for c in PURCHASE_COLS])
        except sqlite3.IntegrityError as e:
            if "UNIQUE constraint failed" in str(e) or "PRIMARY KEY" in str(e):
                raise DuplicateKeyError(str(e)) from e
            raise
        return self.get_purchase(row.purchase_id)  # type: ignore[return-value]

    def get_purchase(self, purchase_id: str) -> Purchase | None:
        r = self._q("SELECT * FROM purchases WHERE purchase_id = ?", (purchase_id,)).fetchone()
        return _row(r) if r else None

    def find_by_key(self, principal_id: str, purchase_intent_id: str, request_fingerprint: str) -> list[Purchase]:
        rows = self._q(
            "SELECT * FROM purchases WHERE principal_id = ? AND purchase_intent_id = ? AND request_fingerprint = ? ORDER BY attempt",
            (principal_id, purchase_intent_id, request_fingerprint),
        ).fetchall()
        return [_row(r) for r in rows]

    def find_recent_by_fingerprint(self, principal_id: str, request_fingerprint: str, since_ts: float) -> list[Purchase]:
        rows = self._q(
            "SELECT * FROM purchases WHERE principal_id = ? AND request_fingerprint = ? AND created_at >= ? ORDER BY created_at DESC",
            (principal_id, request_fingerprint, since_ts),
        ).fetchall()
        return [_row(r) for r in rows]

    def list_purchases(self, principal_id: str, limit: int = 50) -> list[Purchase]:
        rows = self._q(
            "SELECT * FROM purchases WHERE principal_id = ? ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (principal_id, int(limit)),
        ).fetchall()
        return [_row(r) for r in rows]

    def update_status(self, purchase_id: str, expected: PaymentStatus, new: PaymentStatus,
                      history_entry: dict[str, Any] | None = None, **fields: Any) -> bool:
        expected, new = PaymentStatus(expected), PaymentStatus(new)
        assert_transition(expected, new)
        bad = [k for k in fields if k not in PURCHASE_COLS or k in PROTECTED_COLS]
        if bad:
            raise ValueError(f"cannot set columns {bad} through update_status")
        sets = ["payment_status = ?", "updated_at = ?"]
        params: list[Any] = [str(new), now()]
        if history_entry is not None:
            sets.append("history = json_insert(history, '$[#]', json(?))")
            params.append(json.dumps(history_entry))
        for k, v in fields.items():
            sets.append(f"{k} = ?")
            params.append(_enc(k, v))
        params += [purchase_id, str(expected)]
        cur = self._q(
            f"UPDATE purchases SET {', '.join(sets)} WHERE purchase_id = ? AND payment_status = ?", params
        )
        return cur.rowcount == 1

    def set_fulfillment(self, purchase_id: str, status: FulfillmentStatus,
                        history_entry: dict[str, Any] | None = None, **fields: Any) -> None:
        bad = [k for k in fields if k not in PURCHASE_COLS or k in FULFILLMENT_PROTECTED]
        if bad:
            raise ValueError(f"cannot set columns {bad} through set_fulfillment")
        sets = ["fulfillment_status = ?", "updated_at = ?"]
        params: list[Any] = [str(FulfillmentStatus(status)), now()]
        if history_entry is not None:
            sets.append("history = json_insert(history, '$[#]', json(?))")
            params.append(json.dumps(history_entry))
        for k, v in fields.items():
            sets.append(f"{k} = ?")
            params.append(_enc(k, v))
        params.append(purchase_id)
        self._q(f"UPDATE purchases SET {', '.join(sets)} WHERE purchase_id = ?", params)

    def append_history(self, purchase_id: str, entry: dict[str, Any]) -> None:
        self._q(
            "UPDATE purchases SET history = json_insert(history, '$[#]', json(?)), updated_at = ? WHERE purchase_id = ?",
            (json.dumps(entry), now(), purchase_id),
        )

    def list_stuck(self, older_than_s: float) -> list[Purchase]:
        marks = ", ".join("?" for _ in IN_FLIGHT)
        rows = self._q(
            f"SELECT * FROM purchases WHERE payment_status IN ({marks}) AND updated_at < ? ORDER BY updated_at",
            [str(s) for s in IN_FLIGHT] + [now() - older_than_s],
        ).fetchall()
        return [_row(r) for r in rows]

    def spent_minor(self, principal_id: str) -> int:
        marks = ", ".join("?" for _ in BUDGET_STATES)
        r = self._q(
            f"SELECT COALESCE(SUM(authorized_amount_minor), 0) FROM purchases WHERE principal_id = ? AND payment_status IN ({marks})",
            [principal_id] + [str(s) for s in BUDGET_STATES],
        ).fetchone()
        return int(r[0])

    # ---- evidence --------------------------------------------------------
    def add_evidence(self, purchase_id: str, kind: str, payload: dict[str, Any]) -> str:
        eid = new_id()
        self._q(
            "INSERT INTO evidence (evidence_id, purchase_id, kind, payload, created_at) VALUES (?, ?, ?, ?, ?)",
            (eid, purchase_id, str(kind), json.dumps(payload, default=str), now()),
        )
        return eid

    def list_evidence(self, purchase_id: str) -> list[Evidence]:
        rows = self._q(
            "SELECT * FROM evidence WHERE purchase_id = ? ORDER BY created_at, rowid", (purchase_id,)
        ).fetchall()
        return [Evidence(r["evidence_id"], r["purchase_id"], r["kind"], json.loads(r["payload"]), r["created_at"]) for r in rows]

    # ---- findings --------------------------------------------------------
    def add_finding(self, purchase_id: str, code: str, severity: str, summary: str,
                    evidence_ids: list[str], safe_next_actions: list[str]) -> str:
        fid = new_id()
        self._q(
            "INSERT INTO findings (finding_id, purchase_id, code, severity, summary, evidence_ids, safe_next_actions, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (fid, purchase_id, str(code), severity, summary, json.dumps(list(evidence_ids)), json.dumps(list(safe_next_actions)), now()),
        )
        return fid

    def findings_for_purchase(self, purchase_id: str) -> list[Finding]:
        rows = self._q("SELECT * FROM findings WHERE purchase_id = ? ORDER BY created_at, rowid", (purchase_id,)).fetchall()
        return [_finding(r) for r in rows]

    def has_finding(self, purchase_id: str, code: str) -> bool:
        r = self._q("SELECT 1 FROM findings WHERE purchase_id = ? AND code = ? LIMIT 1", (purchase_id, str(code))).fetchone()
        return r is not None

    def has_unresolved_service_finding(self, principal_id: str, service_id: str, code: str) -> bool:
        r = self._q(
            "SELECT 1 FROM findings f JOIN purchases p ON p.purchase_id = f.purchase_id "
            "WHERE p.principal_id = ? AND p.service_id = ? AND f.code = ? AND f.resolved_at IS NULL LIMIT 1",
            (principal_id, service_id, str(code)),
        ).fetchone()
        return r is not None

    def resolve_findings(self, purchase_id: str, code: str) -> int:
        cur = self._q(
            "UPDATE findings SET resolved_at = ? WHERE purchase_id = ? AND code = ? AND resolved_at IS NULL",
            (now(), purchase_id, str(code)),
        )
        return cur.rowcount

    def resolve_service_findings(self, principal_id: str, service_id: str, code: str) -> int:
        cur = self._q(
            "UPDATE findings SET resolved_at = ? WHERE resolved_at IS NULL AND code = ? AND purchase_id IN "
            "(SELECT purchase_id FROM purchases WHERE principal_id = ? AND service_id = ?)",
            (now(), str(code), principal_id, service_id),
        )
        return cur.rowcount

    def list_findings(self, principal_id: str, unresolved_only: bool = True) -> list[dict[str, Any]]:
        where = "AND f.resolved_at IS NULL" if unresolved_only else ""
        rows = self._q(
            "SELECT f.*, p.service_id, p.payment_status, p.fulfillment_status, p.authorized_amount_minor, p.purchase_intent_id "
            f"FROM findings f JOIN purchases p ON p.purchase_id = f.purchase_id WHERE p.principal_id = ? {where} "
            "ORDER BY f.created_at, f.rowid",
            (principal_id,),
        ).fetchall()
        out = []
        for r in rows:
            d = _finding(r).to_dict()
            d.update(service_id=r["service_id"], payment_status=r["payment_status"], fulfillment_status=r["fulfillment_status"],
                     authorized_amount_minor=r["authorized_amount_minor"], purchase_intent_id=r["purchase_intent_id"])
            out.append(d)
        return out

    # ---- wallet audit ----------------------------------------------------
    def get_wallet_state(self, principal_id: str, network: str) -> dict[str, Any] | None:
        r = self._q("SELECT state FROM wallet_audit WHERE principal_id = ? AND network = ?",
                    (principal_id, network)).fetchone()
        return json.loads(r["state"]) if r else None

    def save_wallet_state(self, principal_id: str, network: str, state: dict[str, Any],
                          create_only: bool = False) -> dict[str, Any]:
        """Upsert the state; with create_only an existing state wins (two processes taking a baseline at once)."""
        verb = "INSERT OR IGNORE" if create_only else "INSERT OR REPLACE"
        self._q(f"{verb} INTO wallet_audit (principal_id, network, state, updated_at) VALUES (?, ?, ?, ?)",
                (principal_id, network, json.dumps(state), now()))
        return self.get_wallet_state(principal_id, network) or state

    # ---- admin -----------------------------------------------------------
    def reset(self, principal_id: str) -> dict[str, int]:
        sub = "(SELECT purchase_id FROM purchases WHERE principal_id = ?)"
        with self._lock:
            self._q("BEGIN IMMEDIATE")
            try:
                ev = self._q(f"DELETE FROM evidence WHERE purchase_id IN {sub}", (principal_id,)).rowcount
                fi = self._q(f"DELETE FROM findings WHERE purchase_id IN {sub}", (principal_id,)).rowcount
                pu = self._q("DELETE FROM purchases WHERE principal_id = ?", (principal_id,)).rowcount
                self._q("DELETE FROM wallet_audit WHERE principal_id = ?", (principal_id,))  # the wallet baseline
                self._q("COMMIT")
            except Exception:
                self._q("ROLLBACK")
                raise
        return {"purchases": pu, "evidence": ev, "findings": fi}
