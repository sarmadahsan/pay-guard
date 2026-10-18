"""Delivered artifacts (design §6): inline JSON, or a local path for PDFs/bytes. Never base64."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

from payguard.models import EvidenceKind


@dataclass
class Artifact:
    kind: str  # "json" | "pdf" | "bytes"
    content_type: str
    sha256: str
    size: int
    json: Any | None = None
    path: str | None = None
    page_count: int | None = None
    declared_length: int | None = None  # the seller's Content-Length, when it sent one (identity encoding only)
    received_length: int | None = None  # bytes actually read off the wire; differs from declared_length on a cut body

    def evidence(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_evidence(cls, payload: dict[str, Any]) -> "Artifact":
        return cls(**{f.name: payload.get(f.name) for f in fields(cls)})  # older rows lack the length fields -> None

    def summary(self) -> dict[str, Any]:
        """What the MCP response shows: inline JSON or a path, plus hash, page count and both byte lengths."""
        d = {"kind": self.kind, "content_type": self.content_type, "sha256": self.sha256, "size": self.size,
             "declared_length": self.declared_length, "received_length": self.received_length}
        if self.kind == "json":
            d["json"] = self.json
        else:
            d["path"] = self.path
            d["page_count"] = self.page_count
        return d


def pdf_page_count(path: str) -> int | None:
    """Page count via pypdf; None when the file is not a readable PDF (the validator then fails with actual=None)."""
    try:
        from pypdf import PdfReader

        return len(PdfReader(path).pages)
    except Exception:  # noqa: BLE001
        return None


def store_artifact(body: bytes, content_type: str, purchase_id: str, root: Path,
                   declared_length: int | None = None) -> Artifact:
    sha = hashlib.sha256(body).hexdigest()
    ct = (content_type or "").split(";")[0].strip().lower()
    lengths = {"declared_length": declared_length, "received_length": len(body)}
    if ct == "application/json" or ct.endswith("+json"):
        try:
            return Artifact("json", ct, sha, len(body), json=json.loads(body), **lengths)
        except (ValueError, RecursionError):
            pass  # a cut or malformed JSON body falls through to a .bin file so not_truncated can inspect the raw bytes
    if not re.fullmatch(r"[A-Za-z0-9_-]+", purchase_id or ""):
        raise ValueError(f"invalid purchase_id {purchase_id!r}")  # keeps the file inside root
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    ext = "pdf" if ct == "application/pdf" else "bin"
    path = root / f"{purchase_id}.{ext}"
    if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() != sha:
        # A different body for the same purchase: keep the earlier file so its recorded sha256 stays true.
        path = root / f"{purchase_id}-{sha[:16]}.{ext}"
    path.write_bytes(body)
    art = Artifact("pdf" if ext == "pdf" else "bytes", ct, sha, len(body), path=str(path), **lengths)
    if art.kind == "pdf":
        art.page_count = pdf_page_count(art.path)
    return art


def load_latest(repo: Any, purchase_id: str) -> Artifact | None:
    rows = [e for e in repo.list_evidence(purchase_id) if e.kind == EvidenceKind.ARTIFACT]
    return Artifact.from_evidence(rows[-1].payload) if rows else None
