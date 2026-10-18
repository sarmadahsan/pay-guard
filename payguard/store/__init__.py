"""Store selection by PAYGUARD_STORE (PRD F13): sqlite (default) | supabase (M6)."""
from __future__ import annotations

import os
from pathlib import Path

from payguard.store.base import DuplicateKeyError, Repository

__all__ = ["DuplicateKeyError", "Repository", "open_repository"]


def open_repository(root: Path | None = None) -> Repository:
    kind = os.environ.get("PAYGUARD_STORE", "sqlite").lower()
    if kind == "sqlite":
        from payguard.store.sqlite import SqliteRepository

        path = os.environ.get("PAYGUARD_SQLITE_PATH", "payguard.db")
        if root is not None and not os.path.isabs(path):
            path = str(root / path)
        return SqliteRepository(path)
    if kind == "supabase":
        from payguard.store.supabase import SupabaseRepository  # M6

        return SupabaseRepository.from_env()
    raise ValueError(f"unknown PAYGUARD_STORE={kind!r} (expected sqlite|supabase)")
