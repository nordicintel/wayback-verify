from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from wayback_verify import (
    AccessCheck,
    Capture,
    FileSnapshot,
    Outcome,
    Submission,
    SubmissionStatus,
    VerificationResult,
)
from wayback_verify.cache import MIGRATIONS, SCHEMA_VERSION, Cache
from wayback_verify.models import utcnow


@pytest.fixture
async def cache(tmp_path: Path) -> Cache:
    return Cache(tmp_path / "c.sqlite")


def capture(ts: str = "20240101000000", digest: str | None = "A" * 32) -> Capture:
    return Capture("https://example.gov/a.pdf", ts, 200, "application/pdf", digest, 99)


def snapshot() -> FileSnapshot:
    return FileSnapshot(
        "/x", 1, 2, 3, None, 4, 5, "linux", "B" * 32, "0" * 40, utcnow()
    )


async def test_new_database_is_at_latest_schema(cache: Cache, tmp_path: Path) -> None:
    assert cache.schema_version == SCHEMA_VERSION
    conn = sqlite3.connect(tmp_path / "c.sqlite")
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


async def test_migrates_existing_v1_database(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(MIGRATIONS[0] + "\nPRAGMA user_version = 1;")
    conn.execute(
        """INSERT INTO file_snapshots (path, size, mtime_ns, ctime_ns, birthtime_ns,
           inode, device, platform, sha1_b32, sha1_hex, hashed_at)
           VALUES ('/x', 1, 2, 3, NULL, 4, 5, 'linux', 'B32', 'hex',
                   '2024-01-01T00:00:00.000000+00:00')"""
    )
    conn.execute(
        """INSERT INTO access_checks (original, timestamp, http_status, accessible,
           checked_at) VALUES ('u', '20240101000000', 200, 1,
           '2024-01-01T00:00:00.000000+00:00')"""
    )
    conn.commit()
    conn.close()

    cache = Cache(path)
    assert cache.schema_version == SCHEMA_VERSION
    [snap] = await cache.snapshots_by_digest("B32")
    assert snap.stable and snap.sha1_b32 == "B32"
    [check] = await cache.access_checks()
    assert check.accessible and not check.redirected
    await cache.close()


async def test_newer_schema_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "future.sqlite"
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.close()
    with pytest.raises(RuntimeError):
        Cache(path)


async def test_history_tables_are_append_only(cache: Cache) -> None:
    result = VerificationResult("u", Outcome.MATCH, "A" * 32, capture=capture())
    await cache.add_verifications([result])
    await cache.add_verifications([replace(result, outcome=Outcome.MISMATCH)])
    history = await cache.verifications(url="u")
    assert [r.outcome for r in history] == [Outcome.MATCH, Outcome.MISMATCH]
    latest = await cache.latest_verification("u")
    assert latest is not None and latest.outcome is Outcome.MISMATCH
    await cache.add_captures("u", [capture()])
    await cache.add_access_checks([AccessCheck("u", "20240101000000", 200, True)])
    await cache.add_snapshot(snapshot())
    for table in ("verifications", "file_snapshots", "access_checks", "captures"):
        with pytest.raises(sqlite3.IntegrityError, match="append-only|immutable"):
            cache._conn.execute(f"UPDATE {table} SET rowid = rowid")


async def test_captures_are_immutable_first_write_wins(cache: Cache) -> None:
    c = capture()
    await cache.add_captures("https://example.gov/a.pdf", [c])
    await cache.add_captures(
        "https://example.gov/a.pdf", [replace(c, statuscode=500, length=1)]
    )
    [stored] = await cache.captures_for("https://example.gov/a.pdf")
    assert stored == c


async def test_dash_digest_round_trips_as_none(cache: Cache) -> None:
    await cache.add_captures("u", [capture(digest=None)])
    [stored] = await cache.captures_for("u")
    assert stored.digest is None


async def test_submission_upsert_and_pending(cache: Cache) -> None:
    sub = Submission("u", SubmissionStatus.PENDING, job_id="j1")
    await cache.save_submission(sub)
    assert [s.job_id for s in await cache.pending_submissions()] == ["j1"]
    done = replace(sub, status=SubmissionStatus.SUCCESS, timestamp="20240101000000")
    await cache.save_submission(done)
    assert await cache.pending_submissions() == []
    stored = await cache.get_submission("j1")
    assert stored is not None and stored.status is SubmissionStatus.SUCCESS
    assert len(await cache.submissions(url="u")) == 1


async def test_recent_submission_window(cache: Cache) -> None:
    old = Submission(
        "u",
        SubmissionStatus.SUCCESS,
        job_id="old",
        submitted_at=utcnow() - timedelta(days=3),
        timestamp="20240101000000",
    )
    await cache.save_submission(old)
    assert await cache.recent_submission("u", timedelta(days=1)) is None
    assert await cache.recent_submission("u", timedelta(days=5)) is not None


async def test_access_check_ttl(cache: Cache) -> None:
    old = AccessCheck(
        "u", "20240101000000", 200, True, checked_at=utcnow() - timedelta(days=2)
    )
    await cache.add_access_checks([old])
    assert (
        await cache.recent_access_check("u", old.timestamp, timedelta(days=1)) is None
    )
    hit = await cache.recent_access_check("u", old.timestamp, timedelta(days=3))
    assert hit is not None and hit.from_cache


async def test_prune(cache: Cache) -> None:
    long_ago = utcnow() - timedelta(days=100)
    await cache.add_verifications(
        [
            VerificationResult("u", Outcome.MATCH, "A", checked_at=long_ago),
            VerificationResult("u", Outcome.MATCH, "A"),
        ]
    )
    await cache.save_submission(
        Submission("u", SubmissionStatus.PENDING, job_id="p", updated_at=long_ago)
    )
    counts = await cache.prune(timedelta(days=30))
    assert counts["verifications"] == 1 and counts["submissions"] == 0
    assert len(await cache.verifications()) == 1
    assert len(await cache.pending_submissions()) == 1  # pending jobs are kept
