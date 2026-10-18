"""Delivered artifacts (design §6): inline JSON, or a local path for PDFs/bytes. Never base64."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
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

    def evidence(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_evidence(cls, payload: dict[str, Any]) -> "Artifact":
        return cls(**{k: payload.get(k) for k in ("kind", "content_type", "sha256", "size", "json", "path", "page_count")})

    def summary(self) -> dict[str, Any]:
        """What the MCP response shows: inline JSON or a path, plus hash and page count."""
        d = {"kind": self.kind, "content_type": self.content_type, "sha256": self.sha256, "size": self.size}
        if self.kind == "json":
            d["json"] = self.json
        else:
            d["path"] = self.path
            d["page_count"] = self.page_count
        return d


def pdf_page_count(path: str) -> int | None:
    """Filled in by M4 (Task 4.1). Returns None until then."""
    return None


def store_artifact(body: bytes, content_type: str, purchase_id: str, root: Path) -> Artifact:
    sha = hashlib.sha256(body).hexdigest()
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct == "application/json" or ct.endswith("+json"):
        try:
            return Artifact("json", ct, sha, len(body), json=json.loads(body))
        except (ValueError, RecursionError):
            pass
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    ext = "pdf" if ct == "application/pdf" else "bin"
    path = root / f"{purchase_id}.{ext}"
    if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() != sha:
        # A different body for the same purchase: keep the earlier file so its recorded sha256 stays true.
        path = root / f"{purchase_id}-{sha[:16]}.{ext}"
    path.write_bytes(body)
    art = Artifact("pdf" if ext == "pdf" else "bytes", ct, sha, len(body), path=str(path))
    if art.kind == "pdf":
        art.page_count = pdf_page_count(art.path)
    return art


def load_latest(repo: Any, purchase_id: str) -> Artifact | None:
    rows = [e for e in repo.list_evidence(purchase_id) if e.kind == EvidenceKind.ARTIFACT]
    return Artifact.from_evidence(rows[-1].payload) if rows else None
