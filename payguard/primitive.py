"""Primitive (https://primitive.dev) mail client for agent-filed disputes, plus an offline dry-run backend.

Verified against https://www.primitive.dev/openapi.json on 2026-10-17:
  base  https://api.primitive.dev/v1  (PRIMITIVE_API_BASE_URL overrides), `Authorization: Bearer <key>`
  POST  /send-mail            {from, to (one address string), subject, body_text, attachments:[{filename, content_type,
                               content_base64}], wait}  ->  {success, data: {id, status, from, ...}}
  GET   /sent-emails/{id}/reply  ->  {success, data: {sent_email_id, reply: ReplyEmail | null, waited, timed_out}}
        ReplyEmail: {id, thread_id, from_email, to_email, subject, body_text, body_html, received_at, sender_verified, ...}
        (the spec has no /replies collection: Primitive returns the best single reply to a send)

Env: PRIMITIVE_API_KEY (unset -> dry run), PRIMITIVE_FROM (the agent's own Primitive address). Nothing here moves money.
"""
from __future__ import annotations

import base64
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from payguard.models import iso_now

DEFAULT_BASE_URL = "https://api.primitive.dev/v1"
ATTACHMENT_NAME = "payguard-evidence.json"
_SAFE_ID = re.compile(r"[A-Za-z0-9_-]+")


@dataclass(frozen=True)
class SentMail:
    id: str
    backend: str  # "primitive" | "dry_run"
    from_addr: str
    sent_at: str


@dataclass(frozen=True)
class Reply:
    reply_id: str
    from_addr: str
    received_at: str | None
    body_text: str
    sender_verified: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"reply_id": self.reply_id, "from": self.from_addr, "received_at": self.received_at,
                "body_text": self.body_text, "sender_verified": self.sender_verified}


def _attachment(bundle: dict[str, Any]) -> dict[str, str]:
    raw = json.dumps(bundle, indent=1, default=str).encode()
    return {"filename": ATTACHMENT_NAME, "content_type": "application/json",
            "content_base64": base64.b64encode(raw).decode()}


class PrimitiveBackend:
    """Real sends through Primitive. `transport` lets tests use httpx.MockTransport; the suite never touches the network."""

    name = "primitive"

    def __init__(self, api_key: str, from_addr: str, base_url: str | None = None,
                 transport: httpx.AsyncBaseTransport | None = None, timeout_s: float = 30.0) -> None:
        if not api_key:
            raise ValueError("PRIMITIVE_API_KEY is empty")
        if not from_addr:
            raise ValueError("PRIMITIVE_FROM is unset (the agent's Primitive address)")
        self.from_addr = from_addr
        self.base_url = (base_url or os.environ.get("PRIMITIVE_API_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
        self._transport = transport
        self._timeout = timeout_s

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.base_url, headers=self._headers, transport=self._transport, timeout=self._timeout)

    async def send_mail(self, *, to: str, subject: str, body_text: str, bundle: dict[str, Any], purchase_id: str) -> SentMail:
        payload = {"from": self.from_addr, "to": to, "subject": subject, "body_text": body_text,
                   "attachments": [_attachment(bundle)], "wait": False}
        async with self._client() as client:
            resp = await client.post("/send-mail", json=payload, headers={"Idempotency-Key": f"payguard-dispute-{purchase_id}"})
        if resp.status_code >= 400:
            raise RuntimeError(f"Primitive send-mail failed: HTTP {resp.status_code} {resp.text[:300]}")
        data = resp.json().get("data") or {}
        sent_id = data.get("id")
        if not sent_id:
            raise RuntimeError(f"Primitive send-mail returned no id: {resp.text[:300]}")
        return SentMail(id=str(sent_id), backend=self.name, from_addr=str(data.get("from") or self.from_addr), sent_at=iso_now())

    async def get_replies(self, sent_id: str, purchase_id: str) -> list[Reply]:
        async with self._client() as client:
            resp = await client.get(f"/sent-emails/{sent_id}/reply")
        if resp.status_code == 404:
            return []
        if resp.status_code >= 400:
            raise RuntimeError(f"Primitive reply lookup failed: HTTP {resp.status_code} {resp.text[:300]}")
        reply = (resp.json().get("data") or {}).get("reply")
        if not reply:
            return []
        return [Reply(reply_id=str(reply.get("id")), from_addr=str(reply.get("from_email") or ""),
                      received_at=reply.get("received_at"), body_text=str(reply.get("body_text") or ""),
                      sender_verified=reply.get("sender_verified"))]


class DryRunBackend:
    """No key: write the rendered email to <artifact_dir>/disputes/<purchase_id>.txt and hand back a local id.

    A file <purchase_id>.reply.txt dropped next to it is read back as the seller's reply, so the whole demo beat
    can be rehearsed offline.
    """

    name = "dry_run"

    def __init__(self, artifact_dir: Path | str, from_addr: str | None = None) -> None:
        self.dir = Path(artifact_dir) / "disputes"
        self.from_addr = from_addr or "payguard-agent@dry-run.local"

    def path_for(self, purchase_id: str) -> Path:
        if not _SAFE_ID.fullmatch(purchase_id or ""):
            raise ValueError(f"invalid purchase_id {purchase_id!r}")  # keeps the file inside the disputes dir
        return self.dir / f"{purchase_id}.txt"

    async def send_mail(self, *, to: str, subject: str, body_text: str, bundle: dict[str, Any], purchase_id: str) -> SentMail:
        path = self.path_for(purchase_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        sent_at = iso_now()
        text = (f"From: {self.from_addr}\nTo: {to}\nSubject: {subject}\nDate: {sent_at}\n"
                f"X-PayGuard-Backend: dry_run (PRIMITIVE_API_KEY unset; nothing was sent)\n\n{body_text}\n\n"
                f"---- attachment: {ATTACHMENT_NAME} ----\n{json.dumps(bundle, indent=1, default=str)}\n")
        path.write_text(text, encoding="utf-8")
        return SentMail(id=f"local-{purchase_id}", backend=self.name, from_addr=self.from_addr, sent_at=sent_at)

    async def get_replies(self, sent_id: str, purchase_id: str) -> list[Reply]:
        reply = self.path_for(purchase_id).with_suffix(".reply.txt")
        if not reply.exists():
            return []
        return [Reply(reply_id=f"local-reply-{purchase_id}", from_addr="seller@dry-run.local",
                      received_at=iso_now(), body_text=reply.read_text(encoding="utf-8"), sender_verified=None)]


def build_backend(artifact_dir: Path | str, transport: httpx.AsyncBaseTransport | None = None) -> PrimitiveBackend | DryRunBackend:
    """PrimitiveBackend when PRIMITIVE_API_KEY is set (PRIMITIVE_FROM then required), else the offline dry run."""
    key = (os.environ.get("PRIMITIVE_API_KEY") or "").strip()
    sender = (os.environ.get("PRIMITIVE_FROM") or "").strip()
    if not key:
        return DryRunBackend(artifact_dir, sender or None)
    return PrimitiveBackend(key, sender, transport=transport)
