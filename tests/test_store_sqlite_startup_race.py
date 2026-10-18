"""Regression for the Claude Desktop double-spawn (review S7): two server processes open a FRESH database at
the same moment. Both try to switch it from rollback mode to WAL; the switch needs an exclusive lock, so the
loser must wait on the busy timeout instead of dying with "database is locked"."""
import sqlite3
import threading
import time

from payguard.store.sqlite import SqliteRepository


def test_fresh_db_open_waits_for_exclusive_holder(tmp_path):
    path = tmp_path / "race.db"
    holder = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)  # the other process: file exists, still rollback mode
    holder.execute("CREATE TABLE IF NOT EXISTS warmup (x)")
    assert holder.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    holder.execute("BEGIN EXCLUSIVE")  # it is mid-way through its own WAL switch / schema creation

    def release():
        time.sleep(0.5)
        holder.execute("COMMIT")
        holder.close()

    threading.Thread(target=release, daemon=True).start()
    t0 = time.monotonic()
    repo = SqliteRepository(path)  # must not raise "database is locked"
    assert 0.3 < time.monotonic() - t0 < 5.0
    assert repo.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert repo.conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_already_wal_db_open_waits_for_writer(tmp_path):
    path = tmp_path / "race2.db"
    SqliteRepository(path)  # first process finished: schema exists, WAL on
    holder = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
    holder.execute("BEGIN IMMEDIATE")  # first process now writing (recovery pass)

    def release():
        time.sleep(0.3)
        holder.execute("COMMIT")
        holder.close()

    threading.Thread(target=release, daemon=True).start()
    repo = SqliteRepository(path)
    assert repo.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
