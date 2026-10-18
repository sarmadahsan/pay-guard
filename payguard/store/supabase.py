"""Supabase (PostgREST) repository: same protocol and semantics as `payguard.store.sqlite`, the reference.

Schema lives in `supabase/schema.sql` and is applied manually; this module never runs DDL and does no
writes at construction, so two server processes can start at once (Claude Desktop double spawn).

All calls are synchronous supabase-py calls (blocking is accepted here, as with SQLite).

History concurrency. `history` is jsonb and PostgREST has no atomic "append to array" update, so any
write that appends a history entry is a read-modify-write. To avoid lost updates we use optimistic
concurrency on `updated_at`: every write sets a fresh `updated_at`, and a history write only succeeds if
`updated_at` still equals the value it read (plus `payment_status = expected` for update_status). If
another writer got in between, zero rows come back and we re-read and retry, a bounded number of times.
Writes that do not touch history (update_status/set_fulfillment without an entry) are single updates
that need no read; they still bump `updated_at`, which forces any concurrent history writer to retry.
"""
from __future__ import annotations

import json
import logging
import math
import os
import random
import time
from typing import Any, Callable

from postgrest.exceptions import APIError
from supabase import Client, create_client

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

# Column sets are imported from the reference backend so the two can never drift apart. Importing
# payguard.store.sqlite has no side effects (sqlite3 is stdlib; no connection is opened at import).
from payguard.store.sqlite import FULFILLMENT_PROTECTED, PROTECTED_COLS, PURCHASE_COLS

log = logging.getLogger(__name__)

PURCHASE_KEY = "principal_id,purchase_intent_id,request_fingerprint,attempt"
JSON_COLS = {"args", "history"}
INT_COLS = ("attempt", "quoted_amount_minor", "authorized_amount_minor", "valid_before")
PURCHASE_FIELDS = tuple(Purchase.__dataclass_fields__)
UNIQUE_VIOLATION = "23505"  # Postgres SQLSTATE
HISTORY_WRITE_ATTEMPTS = 5
RETRY_JITTER_S = (0.01, 0.05)  # per attempt; desynchronises two processes retrying together
PAGE_SIZE = 1000  # Supabase's default max-rows; _select_all also copes with a lower server cap
ID_CHUNK = 100  # ids per `in.(...)` filter, keeps request URLs short
FINDING_JOIN = (
    "*, purchases!inner(principal_id, service_id, payment_status, fulfillment_status, "
    "authorized_amount_minor, purchase_intent_id)"
)


def _enc(col: str, v: Any) -> Any:
    """Encode a purchase column value for PostgREST (jsonb columns take Python objects as-is)."""
    if col in JSON_COLS:
        return v
    if isinstance(v, (dict, list)):
        raise TypeError(f"column {col!r} does not accept {type(v).__name__}")
    if isinstance(v, (PaymentStatus, FulfillmentStatus)):
        return str(v)
    return v


def _check_cols(fields: dict[str, Any], protected: set[str], where: str) -> None:
    bad = [k for k in fields if k not in PURCHASE_COLS or k in protected]
    if bad:
        raise ValueError(f"cannot set columns {bad} through {where}")


def _row(r: dict[str, Any]) -> Purchase:
    """Build a Purchase from a DB row, ignoring extra columns (seq) and restoring enums and ints."""
    d = {k: r.get(k) for k in PURCHASE_FIELDS}
    for k in INT_COLS:
        if d[k] is not None:
            d[k] = int(d[k])
    d["payment_status"] = PaymentStatus(d["payment_status"])
    d["fulfillment_status"] = FulfillmentStatus(d["fulfillment_status"])
    d["args"] = d["args"] if d["args"] is not None else {}
    d["history"] = list(d["history"] or [])
    return Purchase(**d)


def _finding(r: dict[str, Any]) -> Finding:
    return Finding(
        finding_id=r["finding_id"], purchase_id=r["purchase_id"], code=r["code"], severity=r["severity"],
        summary=r["summary"], evidence_ids=list(r["evidence_ids"] or []),
        safe_next_actions=list(r["safe_next_actions"] or []), created_at=r["created_at"],
        resolved_at=r.get("resolved_at"),
    )


def _fresh_ts(previous: Any) -> float:
    """now(), guaranteed different from the updated_at we read, so the optimistic check cannot pass twice."""
    t = now()
    if previous is not None and t <= float(previous):
        t = math.nextafter(float(previous), math.inf)
    return t


def _chunks(ids: list[str]) -> list[list[str]]:
    return [ids[i:i + ID_CHUNK] for i in range(0, len(ids), ID_CHUNK)]


class SupabaseRepository:
    def __init__(self, client: Client) -> None:
        self.client = client

    @classmethod
    def from_env(cls) -> "SupabaseRepository":
        """Build from SUPABASE_URL / SUPABASE_SERVICE_KEY. No network calls, no DDL, no writes."""
        values = {}
        for name in ("SUPABASE_URL", "SUPABASE_SERVICE_KEY"):
            value = os.environ.get(name, "").strip()
            if not value:
                raise RuntimeError(f"PAYGUARD_STORE=supabase needs {name} to be set")
            values[name] = value
        return cls(create_client(values["SUPABASE_URL"], values["SUPABASE_SERVICE_KEY"]))

    def _t(self, name: str) -> Any:
        return self.client.table(name)

    def _select_all(self, build: Callable[[], Any]) -> list[dict[str, Any]]:
        """Run a (deterministically ordered) select page by page. PostgREST silently caps responses at
        max-rows, so sums and scans must paginate; stop only on an empty page so a lower server cap
        than PAGE_SIZE cannot truncate the result."""
        out: list[dict[str, Any]] = []
        while True:
            page = build().range(len(out), len(out) + PAGE_SIZE - 1).execute().data or []
            if not page:
                return out
            out.extend(page)

    # ---- purchases -------------------------------------------------------
    def create_purchase(self, row: Purchase) -> Purchase:
        d = row.to_dict()
        payload = {c: _enc(c, d[c]) for c in PURCHASE_COLS}
        try:
            res = self._t("purchases").upsert(payload, on_conflict=PURCHASE_KEY, ignore_duplicates=True).execute()
        except APIError as e:
            if e.code == UNIQUE_VIOLATION:  # e.g. purchase_id primary-key collision
                raise DuplicateKeyError(str(e)) from e
            raise
        if not res.data:  # ON CONFLICT DO NOTHING inserted nothing: the idempotency key exists
            raise DuplicateKeyError(f"purchase key ({PURCHASE_KEY}) already exists")
        return _row(res.data[0])

    def _get_raw(self, purchase_id: str, cols: str = "*") -> dict[str, Any] | None:
        data = self._t("purchases").select(cols).eq("purchase_id", purchase_id).limit(1).execute().data
        return data[0] if data else None

    def get_purchase(self, purchase_id: str) -> Purchase | None:
        r = self._get_raw(purchase_id)
        return _row(r) if r else None

    def find_by_key(self, principal_id: str, purchase_intent_id: str, request_fingerprint: str) -> list[Purchase]:
        data = (self._t("purchases").select("*").eq("principal_id", principal_id)
                .eq("purchase_intent_id", purchase_intent_id).eq("request_fingerprint", request_fingerprint)
                .order("attempt").execute().data)
        return [_row(r) for r in data or []]

    def find_recent_by_fingerprint(self, principal_id: str, request_fingerprint: str, since_ts: float) -> list[Purchase]:
        data = (self._t("purchases").select("*").eq("principal_id", principal_id)
                .eq("request_fingerprint", request_fingerprint).gte("created_at", since_ts)
                .order("created_at", desc=True).order("seq", desc=True).execute().data)
        return [_row(r) for r in data or []]

    def list_purchases(self, principal_id: str, limit: int = 50) -> list[Purchase]:
        data = (self._t("purchases").select("*").eq("principal_id", principal_id)
                .order("created_at", desc=True).order("seq", desc=True).limit(int(limit)).execute().data)
        return [_row(r) for r in data or []]

    def _write_with_history(self, purchase_id: str, patch: dict[str, Any], entry: dict[str, Any],
                            expected: PaymentStatus | None = None) -> bool:
        """Append `entry` to history and apply `patch` with optimistic concurrency on updated_at.

        Returns False when the row is missing or (with `expected`) its payment_status is not `expected`:
        that is a genuine compare-and-set loss. Retries when only updated_at moved under us.
        """
        cols = "history, updated_at, payment_status"
        for attempt in range(1, HISTORY_WRITE_ATTEMPTS + 1):
            cur = self._get_raw(purchase_id, cols)
            if cur is None or (expected is not None and cur["payment_status"] != str(expected)):
                return False
            body = {**patch, "history": list(cur["history"] or []) + [entry],
                    "updated_at": _fresh_ts(cur["updated_at"])}
            q = (self._t("purchases").update(body).eq("purchase_id", purchase_id)
                 .eq("updated_at", cur["updated_at"]))
            if expected is not None:
                q = q.eq("payment_status", str(expected))
            if len(q.execute().data or []) == 1:
                return True
            log.info("history write on %s raced a concurrent writer (attempt %d); retrying", purchase_id, attempt)
            if attempt < HISTORY_WRITE_ATTEMPTS:
                time.sleep(random.uniform(*RETRY_JITTER_S) * attempt)
        raise RuntimeError(
            f"could not write history for purchase {purchase_id}: row kept changing "
            f"({HISTORY_WRITE_ATTEMPTS} attempts)"
        )

    def update_status(self, purchase_id: str, expected: PaymentStatus, new: PaymentStatus,
                      history_entry: dict[str, Any] | None = None, **fields: Any) -> bool:
        expected, new = PaymentStatus(expected), PaymentStatus(new)
        assert_transition(expected, new)
        _check_cols(fields, PROTECTED_COLS, "update_status")
        patch = {k: _enc(k, v) for k, v in fields.items()}
        patch["payment_status"] = str(new)
        if history_entry is not None:
            return self._write_with_history(purchase_id, patch, history_entry, expected=expected)
        patch["updated_at"] = now()
        data = (self._t("purchases").update(patch).eq("purchase_id", purchase_id)
                .eq("payment_status", str(expected)).execute().data)
        return len(data or []) == 1

    def set_fulfillment(self, purchase_id: str, status: FulfillmentStatus,
                        history_entry: dict[str, Any] | None = None, **fields: Any) -> None:
        _check_cols(fields, FULFILLMENT_PROTECTED, "set_fulfillment")
        patch = {k: _enc(k, v) for k, v in fields.items()}
        patch["fulfillment_status"] = str(FulfillmentStatus(status))
        if history_entry is not None:
            self._write_with_history(purchase_id, patch, history_entry)
            return
        patch["updated_at"] = now()
        self._t("purchases").update(patch).eq("purchase_id", purchase_id).execute()

    def append_history(self, purchase_id: str, entry: dict[str, Any]) -> None:
        self._write_with_history(purchase_id, {}, entry)

    def list_stuck(self, older_than_s: float) -> list[Purchase]:
        cutoff = now() - older_than_s
        rows = self._select_all(lambda: self._t("purchases").select("*")
                                .in_("payment_status", sorted(str(s) for s in IN_FLIGHT))
                                .lt("updated_at", cutoff).order("updated_at").order("seq"))
        return [_row(r) for r in rows]

    def spent_minor(self, principal_id: str) -> int:
        rows = self._select_all(lambda: self._t("purchases").select("authorized_amount_minor, seq")
                                .eq("principal_id", principal_id)
                                .in_("payment_status", sorted(str(s) for s in BUDGET_STATES)).order("seq"))
        return sum(int(r["authorized_amount_minor"]) for r in rows if r.get("authorized_amount_minor") is not None)

    # ---- evidence --------------------------------------------------------
    def add_evidence(self, purchase_id: str, kind: str, payload: dict[str, Any]) -> str:
        eid = new_id()
        # Same coercion as SQLite's json.dumps(default=str): non-JSON values become strings instead of
        # crashing supabase-py's request serialization.
        body = json.loads(json.dumps(payload, default=str))
        self._t("evidence").insert({"evidence_id": eid, "purchase_id": purchase_id, "kind": str(kind),
                                    "payload": body, "created_at": now()}).execute()
        return eid

    def list_evidence(self, purchase_id: str) -> list[Evidence]:
        data = (self._t("evidence").select("*").eq("purchase_id", purchase_id)
                .order("created_at").order("seq").execute().data)
        return [Evidence(r["evidence_id"], r["purchase_id"], r["kind"], r["payload"], r["created_at"])
                for r in data or []]

    # ---- findings --------------------------------------------------------
    def add_finding(self, purchase_id: str, code: str, severity: str, summary: str,
                    evidence_ids: list[str], safe_next_actions: list[str]) -> str:
        fid = new_id()
        self._t("findings").insert({
            "finding_id": fid, "purchase_id": purchase_id, "code": str(code), "severity": severity,
            "summary": summary, "evidence_ids": list(evidence_ids), "safe_next_actions": list(safe_next_actions),
            "created_at": now(),
        }).execute()
        return fid

    def findings_for_purchase(self, purchase_id: str) -> list[Finding]:
        data = (self._t("findings").select("*").eq("purchase_id", purchase_id)
                .order("created_at").order("seq").execute().data)
        return [_finding(r) for r in data or []]

    def has_finding(self, purchase_id: str, code: str) -> bool:
        data = (self._t("findings").select("finding_id").eq("purchase_id", purchase_id)
                .eq("code", str(code)).limit(1).execute().data)
        return bool(data)

    def has_unresolved_service_finding(self, principal_id: str, service_id: str, code: str) -> bool:
        # !inner makes the filters on the embedded purchase restrict the findings rows (a SQL inner join).
        data = (self._t("findings").select("finding_id, purchases!inner(principal_id, service_id)")
                .eq("code", str(code)).is_("resolved_at", "null")
                .eq("purchases.principal_id", principal_id).eq("purchases.service_id", service_id)
                .limit(1).execute().data)
        return bool(data)

    def resolve_findings(self, purchase_id: str, code: str) -> int:
        data = (self._t("findings").update({"resolved_at": now()}).eq("purchase_id", purchase_id)
                .eq("code", str(code)).is_("resolved_at", "null").execute().data)
        return len(data or [])

    def _purchase_ids(self, principal_id: str, service_id: str | None = None) -> list[str]:
        def build() -> Any:
            q = self._t("purchases").select("purchase_id, seq").eq("principal_id", principal_id)
            if service_id is not None:
                q = q.eq("service_id", service_id)
            return q.order("seq")

        return [r["purchase_id"] for r in self._select_all(build)]

    def resolve_service_findings(self, principal_id: str, service_id: str, code: str) -> int:
        # PostgREST cannot filter an UPDATE on an embedded resource, so resolve by purchase-id list.
        ts = now()
        n = 0
        for ids in _chunks(self._purchase_ids(principal_id, service_id)):
            data = (self._t("findings").update({"resolved_at": ts}).in_("purchase_id", ids)
                    .eq("code", str(code)).is_("resolved_at", "null").execute().data)
            n += len(data or [])
        return n

    def list_findings(self, principal_id: str, unresolved_only: bool = True) -> list[dict[str, Any]]:
        def build() -> Any:
            q = self._t("findings").select(FINDING_JOIN).eq("purchases.principal_id", principal_id)
            if unresolved_only:
                q = q.is_("resolved_at", "null")
            return q.order("created_at").order("seq")

        out = []
        for r in self._select_all(build):
            p = r["purchases"]
            d = _finding(r).to_dict()
            amount = p.get("authorized_amount_minor")
            d.update(service_id=p["service_id"], payment_status=p["payment_status"],
                     fulfillment_status=p["fulfillment_status"],
                     authorized_amount_minor=int(amount) if amount is not None else None,
                     purchase_intent_id=p["purchase_intent_id"])
            out.append(d)
        return out

    # ---- admin -----------------------------------------------------------
    def reset(self, principal_id: str) -> dict[str, int]:
        # Not one transaction: PostgREST runs each request in its own transaction. Children are deleted
        # first (evidence, then findings, then purchases) so a failure part-way leaves no orphans, and a
        # re-run finishes the job. Purchases created concurrently with a reset may survive it.
        ev = fi = 0
        for ids in _chunks(self._purchase_ids(principal_id)):
            ev += len(self._t("evidence").delete().in_("purchase_id", ids).execute().data or [])
            fi += len(self._t("findings").delete().in_("purchase_id", ids).execute().data or [])
        pu = len(self._t("purchases").delete().eq("principal_id", principal_id).execute().data or [])
        return {"purchases": pu, "evidence": ev, "findings": fi}
