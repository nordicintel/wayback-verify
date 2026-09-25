"""sqlite3 history and result cache.

Every public coroutine runs its SQL in ``asyncio.to_thread`` on one shared
connection guarded by a lock. ``path=None`` keeps the database in memory, so
everything still works within a run but nothing survives it.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
from collections.abc import Callable, Iterable, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar

from .files import normalize_digest
from .models import (
    AccessCheck,
    Capture,
    FileSnapshot,
    FileState,
    Outcome,
    Submission,
    SubmissionStatus,
    UrlMatch,
    VerificationResult,
    utcnow,
)

T = TypeVar("T")

# Each entry migrates the schema from version ``index`` to ``index + 1``.
MIGRATIONS: list[str] = [
    # v1: the tables from the implementation plan.
    """
    CREATE TABLE file_snapshots (
        id INTEGER PRIMARY KEY,
        path TEXT NOT NULL,
        size INTEGER NOT NULL,
        mtime_ns INTEGER NOT NULL,
        ctime_ns INTEGER NOT NULL,
        birthtime_ns INTEGER,
        inode INTEGER NOT NULL,
        device INTEGER NOT NULL,
        platform TEXT NOT NULL,
        sha1_b32 TEXT NOT NULL,
        sha1_hex TEXT NOT NULL,
        hashed_at TEXT NOT NULL
    );
    CREATE INDEX ix_snapshots_state
        ON file_snapshots (path, size, mtime_ns, ctime_ns, inode, device);
    CREATE INDEX ix_snapshots_digest ON file_snapshots (sha1_b32);

    CREATE TABLE submissions (
        id INTEGER PRIMARY KEY,
        job_id TEXT UNIQUE,
        url TEXT NOT NULL,
        submitted_at TEXT NOT NULL,
        status TEXT NOT NULL,
        error_code TEXT,
        capture_timestamp TEXT,
        original_url TEXT,
        updated_at TEXT NOT NULL,
        raw_json TEXT
    );
    CREATE INDEX ix_submissions_url ON submissions (url, status, updated_at);

    CREATE TABLE captures (
        original TEXT NOT NULL,
        timestamp TEXT NOT NULL,
        digest TEXT NOT NULL,
        statuscode INTEGER,
        mimetype TEXT,
        length INTEGER,
        first_seen_at TEXT NOT NULL,
        PRIMARY KEY (original, timestamp, digest)
    );
    -- Which queried URL returned which capture (CDX folds URL variants).
    CREATE TABLE capture_urls (
        url TEXT NOT NULL,
        original TEXT NOT NULL,
        timestamp TEXT NOT NULL,
        digest TEXT NOT NULL,
        url_match TEXT NOT NULL,
        PRIMARY KEY (url, original, timestamp, digest)
    );
    CREATE INDEX ix_capture_urls ON capture_urls (url, timestamp);

    CREATE TABLE cdx_queries (
        id INTEGER PRIMARY KEY,
        url TEXT NOT NULL,
        params_hash TEXT NOT NULL,
        queried_at TEXT NOT NULL,
        result_count INTEGER NOT NULL
    );
    CREATE INDEX ix_cdx_queries ON cdx_queries (url, params_hash, queried_at);

    CREATE TABLE verifications (
        id INTEGER PRIMARY KEY,
        url TEXT NOT NULL,
        capture_timestamp TEXT,
        file_snapshot_id INTEGER REFERENCES file_snapshots (id),
        local_digest TEXT,
        archive_digest TEXT,
        outcome TEXT NOT NULL,
        checked_at TEXT NOT NULL,
        note TEXT
    );
    CREATE INDEX ix_verifications_url ON verifications (url, checked_at);

    CREATE TABLE access_checks (
        id INTEGER PRIMARY KEY,
        original TEXT NOT NULL,
        timestamp TEXT NOT NULL,
        http_status INTEGER,
        accessible INTEGER NOT NULL,
        checked_at TEXT NOT NULL
    );
    CREATE INDEX ix_access_checks ON access_checks (original, timestamp, checked_at);
    """,
    # v2: snapshot stability, richer verification and access history, and
    # triggers that make the history tables append-only.
    """
    ALTER TABLE file_snapshots ADD COLUMN stable INTEGER NOT NULL DEFAULT 1;
    ALTER TABLE submissions ADD COLUMN message TEXT;
    ALTER TABLE verifications ADD COLUMN capture_original TEXT;
    ALTER TABLE verifications ADD COLUMN capture_statuscode INTEGER;
    ALTER TABLE verifications ADD COLUMN capture_mimetype TEXT;
    ALTER TABLE verifications ADD COLUMN url_match TEXT;
    ALTER TABLE access_checks ADD COLUMN redirected INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE access_checks ADD COLUMN final_timestamp TEXT;
    ALTER TABLE access_checks ADD COLUMN final_url TEXT;
    ALTER TABLE access_checks ADD COLUMN method TEXT;
    ALTER TABLE access_checks ADD COLUMN error TEXT;
    CREATE TRIGGER file_snapshots_no_update BEFORE UPDATE ON file_snapshots
        BEGIN SELECT RAISE(ABORT, 'file_snapshots are append-only'); END;
    CREATE TRIGGER verifications_no_update BEFORE UPDATE ON verifications
        BEGIN SELECT RAISE(ABORT, 'verifications are append-only'); END;
    CREATE TRIGGER access_checks_no_update BEFORE UPDATE ON access_checks
        BEGIN SELECT RAISE(ABORT, 'access_checks are append-only'); END;
    CREATE TRIGGER captures_no_update BEFORE UPDATE ON captures
        BEGIN SELECT RAISE(ABORT, 'captures are immutable'); END;
    """,
]
SCHEMA_VERSION = len(MIGRATIONS)


def _iso(dt: datetime) -> str:
    # Fixed width, so ISO strings compare in time order.
    return dt.astimezone(UTC).isoformat(timespec="microseconds")


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _snapshot(row: sqlite3.Row) -> FileSnapshot:
    return FileSnapshot(
        path=row["path"],
        size=row["size"],
        mtime_ns=row["mtime_ns"],
        ctime_ns=row["ctime_ns"],
        birthtime_ns=row["birthtime_ns"],
        inode=row["inode"],
        device=row["device"],
        platform=row["platform"],
        sha1_b32=row["sha1_b32"],
        sha1_hex=row["sha1_hex"],
        hashed_at=_dt(row["hashed_at"]),
        stable=bool(row["stable"]),
        id=row["id"],
    )


def _submission(row: sqlite3.Row) -> Submission:
    raw = json.loads(row["raw_json"]) if row["raw_json"] else None
    return Submission(
        url=row["url"],
        status=SubmissionStatus(row["status"]),
        job_id=row["job_id"],
        submitted_at=_dt(row["submitted_at"]),
        updated_at=_dt(row["updated_at"]),
        timestamp=row["capture_timestamp"],
        original_url=row["original_url"],
        error_code=row["error_code"],
        message=row["message"],
        from_cache=True,
        raw=raw,
    )


def _capture(row: sqlite3.Row) -> Capture:
    return Capture(
        original=row["original"],
        timestamp=row["timestamp"],
        statuscode=row["statuscode"],
        mimetype=row["mimetype"],
        digest=None if row["digest"] == "-" else row["digest"],
        length=row["length"],
        url_match=UrlMatch(row["url_match"]),
    )


def _access(row: sqlite3.Row) -> AccessCheck:
    return AccessCheck(
        original=row["original"],
        timestamp=row["timestamp"],
        http_status=row["http_status"],
        accessible=bool(row["accessible"]),
        redirected=bool(row["redirected"]),
        final_timestamp=row["final_timestamp"],
        final_url=row["final_url"],
        method=row["method"] or "HEAD",
        checked_at=_dt(row["checked_at"]),
        error=row["error"],
        from_cache=True,
    )


class Cache:
    """Async facade over a sqlite3 database of hashes, captures and results."""

    def __init__(self, path: str | os.PathLike[str] | None) -> None:
        self.path = None if path is None else os.fspath(path)
        self._conn = sqlite3.connect(
            self.path or ":memory:",
            check_same_thread=False,
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            if self.path:
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=10000")
            self._migrate()

    # -- plumbing ---------------------------------------------------------

    def _migrate(self) -> None:
        version: int = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"cache schema v{version} is newer than this package (v{SCHEMA_VERSION})"
            )
        for target, script in enumerate(MIGRATIONS[version:], start=version + 1):
            self._conn.executescript(
                f"BEGIN;\n{script}\nPRAGMA user_version = {target};\nCOMMIT;"
            )

    @property
    def schema_version(self) -> int:
        with self._lock:
            return int(self._conn.execute("PRAGMA user_version").fetchone()[0])

    def _tx(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                result = fn(self._conn)
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")
            return result

    def _query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    async def _run(self, fn: Callable[..., T], *args: Any) -> T:
        return await asyncio.to_thread(fn, *args)

    async def close(self) -> None:
        await self._run(self._close)

    def _close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- file snapshots ---------------------------------------------------

    async def find_snapshot(self, state: FileState) -> FileSnapshot | None:
        """Latest stable snapshot whose recorded stat properties all match."""
        rows = await self._run(
            self._query,
            """SELECT * FROM file_snapshots
               WHERE path=? AND size=? AND mtime_ns=? AND ctime_ns=?
                 AND inode=? AND device=? AND stable=1
               ORDER BY id DESC LIMIT 1""",
            (
                state.path,
                state.size,
                state.mtime_ns,
                state.ctime_ns,
                state.inode,
                state.device,
            ),
        )
        return _snapshot(rows[0]) if rows else None

    async def add_snapshot(self, snap: FileSnapshot) -> FileSnapshot:
        def insert(conn: sqlite3.Connection) -> int:
            cur = conn.execute(
                """INSERT INTO file_snapshots (path, size, mtime_ns, ctime_ns,
                   birthtime_ns, inode, device, platform, sha1_b32, sha1_hex,
                   hashed_at, stable) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    snap.path,
                    snap.size,
                    snap.mtime_ns,
                    snap.ctime_ns,
                    snap.birthtime_ns,
                    snap.inode,
                    snap.device,
                    snap.platform,
                    snap.sha1_b32,
                    snap.sha1_hex,
                    _iso(snap.hashed_at),
                    int(snap.stable),
                ),
            )
            return int(cur.lastrowid or 0)

        snap_id = await self._run(self._tx, insert)
        return replace(snap, id=snap_id)

    async def file_history(self, path: str | os.PathLike[str]) -> list[FileSnapshot]:
        """Every snapshot and hash seen for ``path``, oldest first."""
        resolved = os.path.realpath(path)
        rows = await self._run(
            self._query,
            "SELECT * FROM file_snapshots WHERE path=? ORDER BY id",
            (resolved,),
        )
        return [_snapshot(r) for r in rows]

    async def snapshots_by_digest(self, digest: str) -> list[FileSnapshot]:
        rows = await self._run(
            self._query,
            "SELECT * FROM file_snapshots WHERE sha1_b32=? ORDER BY id",
            (normalize_digest(digest),),
        )
        return [_snapshot(r) for r in rows]

    async def get_snapshot(self, snapshot_id: int) -> FileSnapshot | None:
        rows = await self._run(
            self._query, "SELECT * FROM file_snapshots WHERE id=?", (snapshot_id,)
        )
        return _snapshot(rows[0]) if rows else None

    # -- submissions ------------------------------------------------------

    async def save_submission(self, sub: Submission) -> None:
        await self._run(self._tx, lambda c: _upsert_submission(c, sub))

    async def get_submission(self, job_id: str) -> Submission | None:
        rows = await self._run(
            self._query, "SELECT * FROM submissions WHERE job_id=?", (job_id,)
        )
        return _submission(rows[0]) if rows else None

    async def recent_submission(
        self,
        url: str,
        within: timedelta,
        statuses: Iterable[SubmissionStatus] = (SubmissionStatus.SUCCESS,),
    ) -> Submission | None:
        """Latest submission of ``url`` in one of ``statuses`` submitted within
        ``within``; successes are preferred over pending jobs."""
        wanted = [str(s) for s in statuses]
        marks = ",".join("?" * len(wanted))
        rows = await self._run(
            self._query,
            f"""SELECT * FROM submissions WHERE url=? AND status IN ({marks})
               AND submitted_at >= ?
               ORDER BY status='success' DESC, submitted_at DESC LIMIT 1""",
            (url, *wanted, _iso(utcnow() - within)),
        )
        return _submission(rows[0]) if rows else None

    async def latest_submission(self, url: str) -> Submission | None:
        rows = await self._run(
            self._query,
            "SELECT * FROM submissions WHERE url=? ORDER BY updated_at DESC, id DESC "
            "LIMIT 1",
            (url,),
        )
        return _submission(rows[0]) if rows else None

    async def pending_submissions(self) -> list[Submission]:
        rows = await self._run(
            self._query,
            "SELECT * FROM submissions WHERE status='pending' AND job_id IS NOT NULL "
            "ORDER BY submitted_at",
        )
        return [_submission(r) for r in rows]

    async def submissions(
        self, *, url: str | None = None, since: datetime | None = None
    ) -> list[Submission]:
        sql = "SELECT * FROM submissions WHERE 1=1"
        params: list[Any] = []
        if url is not None:
            sql, params = sql + " AND url=?", [*params, url]
        if since is not None:
            sql, params = sql + " AND updated_at >= ?", [*params, _iso(since)]
        rows = await self._run(self._query, sql + " ORDER BY id", params)
        return [_submission(r) for r in rows]

    # -- CDX captures -----------------------------------------------------

    async def add_captures(
        self,
        url: str,
        captures: Sequence[Capture],
        *,
        params_hash: str | None = None,
    ) -> None:
        """Store CDX rows (immutable, first write wins) and log the query."""
        now = _iso(utcnow())

        def insert(conn: sqlite3.Connection) -> None:
            for c in captures:
                digest = c.digest or "-"
                conn.execute(
                    """INSERT OR IGNORE INTO captures (original, timestamp, digest,
                       statuscode, mimetype, length, first_seen_at)
                       VALUES (?,?,?,?,?,?,?)""",
                    (
                        c.original,
                        c.timestamp,
                        digest,
                        c.statuscode,
                        c.mimetype,
                        c.length,
                        now,
                    ),
                )
                conn.execute(
                    """INSERT OR IGNORE INTO capture_urls
                       (url, original, timestamp, digest, url_match)
                       VALUES (?,?,?,?,?)""",
                    (url, c.original, c.timestamp, digest, str(c.url_match)),
                )
            if params_hash is not None:
                conn.execute(
                    """INSERT INTO cdx_queries (url, params_hash, queried_at,
                       result_count) VALUES (?,?,?,?)""",
                    (url, params_hash, now, len(captures)),
                )

        await self._run(self._tx, insert)

    async def captures_for(
        self, url: str, *, from_ts: str | None = None, to_ts: str | None = None
    ) -> list[Capture]:
        sql = """SELECT c.*, u.url_match FROM capture_urls u JOIN captures c
                 USING (original, timestamp, digest) WHERE u.url=?"""
        params: list[Any] = [url]
        if from_ts:
            sql, params = sql + " AND c.timestamp >= ?", [*params, from_ts]
        if to_ts:
            sql, params = sql + " AND c.timestamp <= ?", [*params, to_ts]
        rows = await self._run(self._query, sql + " ORDER BY c.timestamp", params)
        return [_capture(r) for r in rows]

    async def last_cdx_query(
        self, url: str, params_hash: str
    ) -> tuple[datetime, int] | None:
        rows = await self._run(
            self._query,
            """SELECT queried_at, result_count FROM cdx_queries
               WHERE url=? AND params_hash=? ORDER BY queried_at DESC LIMIT 1""",
            (url, params_hash),
        )
        if not rows:
            return None
        return _dt(rows[0]["queried_at"]), int(rows[0]["result_count"])

    # -- verifications ----------------------------------------------------

    async def add_verifications(self, results: Iterable[VerificationResult]) -> None:
        items = list(results)
        if items:
            await self._run(self._tx, lambda c: _insert_verifications(c, items))

    async def latest_verification(self, url: str) -> VerificationResult | None:
        found = await self.verifications(url=url, latest_only=True)
        return found[0] if found else None

    async def verifications(
        self,
        *,
        url: str | None = None,
        since: datetime | None = None,
        latest_only: bool = False,
    ) -> list[VerificationResult]:
        """Verification history; ``latest_only`` keeps the newest row per
        (URL, file path)."""
        sql = """SELECT v.*, s.id AS s_id FROM verifications v
                 LEFT JOIN file_snapshots s ON s.id = v.file_snapshot_id WHERE 1=1"""
        params: list[Any] = []
        if url is not None:
            sql, params = sql + " AND v.url=?", [*params, url]
        if since is not None:
            sql, params = sql + " AND v.checked_at >= ?", [*params, _iso(since)]
        rows = await self._run(self._query, sql + " ORDER BY v.id", params)
        snaps = await self._snapshots_by_id(
            {r["file_snapshot_id"] for r in rows if r["file_snapshot_id"]}
        )
        results = [_verification(r, snaps.get(r["file_snapshot_id"])) for r in rows]
        if latest_only:
            latest: dict[tuple[str, str | None], VerificationResult] = {}
            for r in results:
                latest[(r.url, r.snapshot.path if r.snapshot else None)] = r
            results = sorted(latest.values(), key=lambda r: r.checked_at, reverse=True)
        return results

    async def _snapshots_by_id(self, ids: set[int]) -> dict[int, FileSnapshot]:
        if not ids:
            return {}
        marks = ",".join("?" * len(ids))
        rows = await self._run(
            self._query,
            f"SELECT * FROM file_snapshots WHERE id IN ({marks})",
            list(ids),
        )
        return {r["id"]: _snapshot(r) for r in rows}

    # -- access checks ----------------------------------------------------

    async def add_access_checks(self, checks: Iterable[AccessCheck]) -> None:
        items = list(checks)
        if items:
            await self._run(self._tx, lambda c: _insert_access(c, items))

    async def recent_access_check(
        self, original: str, timestamp: str, within: timedelta
    ) -> AccessCheck | None:
        rows = await self._run(
            self._query,
            """SELECT * FROM access_checks WHERE original=? AND timestamp=?
               AND checked_at >= ? AND error IS NULL
               ORDER BY checked_at DESC LIMIT 1""",
            (original, timestamp, _iso(utcnow() - within)),
        )
        return _access(rows[0]) if rows else None

    async def access_checks(
        self, *, since: datetime | None = None, latest_only: bool = False
    ) -> list[AccessCheck]:
        sql = "SELECT * FROM access_checks WHERE 1=1"
        params: list[Any] = []
        if since is not None:
            sql, params = sql + " AND checked_at >= ?", [_iso(since)]
        rows = await self._run(self._query, sql + " ORDER BY id", params)
        checks = [_access(r) for r in rows]
        if latest_only:
            checks = list({(c.original, c.timestamp): c for c in checks}.values())
        return checks

    # -- history / maintenance -------------------------------------------

    async def history(self, url: str) -> dict[str, list[Any]]:
        """Submissions, captures, verifications and access checks for ``url``."""
        subs = await self.submissions(url=url)
        caps = await self.captures_for(url)
        vers = await self.verifications(url=url)
        originals = {c.original for c in caps} | {url}
        marks = ",".join("?" * len(originals))
        rows = await self._run(
            self._query,
            f"SELECT * FROM access_checks WHERE original IN ({marks}) ORDER BY id",
            list(originals),
        )
        return {
            "submissions": subs,
            "captures": caps,
            "verifications": vers,
            "access_checks": [_access(r) for r in rows],
        }

    async def prune(self, older_than: timedelta | datetime) -> dict[str, int]:
        """Delete history older than the cutoff. Captures (immutable facts),
        pending submissions, snapshots still referenced by a verification and
        the newest snapshot of each path are kept."""
        cutoff = _iso(
            older_than if isinstance(older_than, datetime) else utcnow() - older_than
        )

        def prune(conn: sqlite3.Connection) -> dict[str, int]:
            counts = {
                "verifications": conn.execute(
                    "DELETE FROM verifications WHERE checked_at < ?", (cutoff,)
                ).rowcount,
                "access_checks": conn.execute(
                    "DELETE FROM access_checks WHERE checked_at < ?", (cutoff,)
                ).rowcount,
                "cdx_queries": conn.execute(
                    "DELETE FROM cdx_queries WHERE queried_at < ?", (cutoff,)
                ).rowcount,
                "submissions": conn.execute(
                    "DELETE FROM submissions WHERE updated_at < ? "
                    "AND status != 'pending'",
                    (cutoff,),
                ).rowcount,
            }
            counts["file_snapshots"] = conn.execute(
                """DELETE FROM file_snapshots WHERE hashed_at < ?
                   AND id NOT IN (SELECT file_snapshot_id FROM verifications
                                  WHERE file_snapshot_id IS NOT NULL)
                   AND id NOT IN (SELECT MAX(id) FROM file_snapshots GROUP BY path)""",
                (cutoff,),
            ).rowcount
            return counts

        return await self._run(self._tx, prune)


def _upsert_submission(conn: sqlite3.Connection, sub: Submission) -> None:
    values = (
        sub.url,
        _iso(sub.submitted_at),
        str(sub.status),
        sub.error_code,
        sub.timestamp,
        sub.original_url,
        _iso(sub.updated_at),
        json.dumps(sub.raw) if sub.raw is not None else None,
        sub.message,
    )
    if sub.job_id is not None:
        updated = conn.execute(
            """UPDATE submissions SET url=?, submitted_at=?, status=?, error_code=?,
               capture_timestamp=?, original_url=?, updated_at=?,
               raw_json=COALESCE(?, raw_json), message=? WHERE job_id=?""",
            (*values, sub.job_id),
        ).rowcount
        if updated:
            return
    conn.execute(
        """INSERT INTO submissions (url, submitted_at, status, error_code,
           capture_timestamp, original_url, updated_at, raw_json, message, job_id)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (*values, sub.job_id),
    )


def _insert_verifications(
    conn: sqlite3.Connection, results: Sequence[VerificationResult]
) -> None:
    for r in results:
        c = r.capture
        conn.execute(
            """INSERT INTO verifications (url, capture_timestamp, file_snapshot_id,
               local_digest, archive_digest, outcome, checked_at, note,
               capture_original, capture_statuscode, capture_mimetype, url_match)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                r.url,
                c.timestamp if c else None,
                r.snapshot.id if r.snapshot else None,
                r.local_digest,
                r.archive_digest,
                str(r.outcome),
                _iso(r.checked_at),
                r.note,
                c.original if c else None,
                c.statuscode if c else None,
                c.mimetype if c else None,
                str(c.url_match) if c else None,
            ),
        )


def _verification(
    row: sqlite3.Row, snapshot: FileSnapshot | None
) -> VerificationResult:
    capture = None
    if row["capture_timestamp"]:
        capture = Capture(
            original=row["capture_original"] or row["url"],
            timestamp=row["capture_timestamp"],
            statuscode=row["capture_statuscode"],
            mimetype=row["capture_mimetype"],
            digest=row["archive_digest"],
            length=None,
            url_match=UrlMatch(row["url_match"] or "exact"),
        )
    return VerificationResult(
        url=row["url"],
        outcome=Outcome(row["outcome"]),
        local_digest=row["local_digest"],
        snapshot=snapshot,
        capture=capture,
        checked_at=_dt(row["checked_at"]),
        note=row["note"],
    )


def _insert_access(conn: sqlite3.Connection, checks: Sequence[AccessCheck]) -> None:
    for a in checks:
        conn.execute(
            """INSERT INTO access_checks (original, timestamp, http_status,
               accessible, checked_at, redirected, final_timestamp, final_url,
               method, error) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                a.original,
                a.timestamp,
                a.http_status,
                int(a.accessible),
                _iso(a.checked_at),
                int(a.redirected),
                a.final_timestamp,
                a.final_url,
                a.method,
                a.error,
            ),
        )
