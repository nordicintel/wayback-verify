"""``ArchiveClient``: owns the aiohttp session, config, limiters and cache."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterable, Callable, Iterable
from dataclasses import replace
from datetime import datetime, timedelta
from types import TracebackType
from typing import Any, Self

import aiohttp

from . import access, bulk, spn
from .cache import Cache
from .cdx import CdxClient, Lookup, Selection
from .config import Config
from .files import normalize_digest, snapshot_file, stat_file
from .http import RateLimiter, Sleep, Transport, TransportError
from .models import (
    AccessCheck,
    Capture,
    FileSnapshot,
    Outcome,
    Submission,
    SubmissionStatus,
    VerificationResult,
)
from .report import FailureReport, build_report
from .verify import compare, default_selection, error_result


class ArchiveClient:
    """Async client for archiving and verifying files through the Wayback Machine.

    ``cache_path=None`` keeps the cache in memory: hashes, captures and
    results are reused within the run, but nothing survives it.
    """

    def __init__(
        self,
        config: Config | None = None,
        cache_path: str | os.PathLike[str] | None = None,
        *,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.config = config if config is not None else Config.from_environment()
        self.cache_path = cache_path
        self.sleep = sleep
        self._session: aiohttp.ClientSession | None = None
        self._cache: Cache | None = None
        self._transport: Transport | None = None
        self._cdx_client: CdxClient | None = None
        c = self.config
        self.spn_limiter = RateLimiter(c.effective_spn_limit, sleep=sleep)
        self.status_limiter = RateLimiter(c.status_limit, sleep=sleep)
        self.cdx_limiter = RateLimiter(c.cdx_limit, sleep=sleep)
        self.access_limiter = RateLimiter(c.access_limit, sleep=sleep)
        self._hash_sem = asyncio.Semaphore(max(1, c.hash_workers))

    # -- lifecycle ------------------------------------------------------------

    async def __aenter__(self) -> Self:
        self._cache = await asyncio.to_thread(Cache, self.cache_path)
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.config.request_timeout),
            headers={"User-Agent": self.config.user_agent},
        )
        self._transport = Transport(self._session, self.config.retry, sleep=self.sleep)
        self._cdx_client = CdxClient(
            self._transport, self.cdx_limiter, self._cache, self.config
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None
        if self._cache is not None:
            await self._cache.close()
            self._cache = None
        self._transport = self._cdx_client = None

    @property
    def cache(self) -> Cache:
        if self._cache is None:
            raise RuntimeError("use 'async with ArchiveClient(...)' first")
        return self._cache

    @property
    def transport(self) -> Transport:
        if self._transport is None:
            raise RuntimeError("use 'async with ArchiveClient(...)' first")
        return self._transport

    @property
    def _cdx(self) -> CdxClient:
        if self._cdx_client is None:
            raise RuntimeError("use 'async with ArchiveClient(...)' first")
        return self._cdx_client

    # -- files ----------------------------------------------------------------

    async def hash_file(
        self,
        path: str | os.PathLike[str],
        *,
        force: bool = False,
        on_bytes: Callable[[int], None] | None = None,
    ) -> FileSnapshot:
        """Snapshot + SHA-1 of a file, reusing the cached hash when every
        recorded stat property (size, mtime, ctime, inode, device) matches."""
        state = await asyncio.to_thread(stat_file, path)
        if not force:
            cached = await self.cache.find_snapshot(state)
            if cached is not None:
                return replace(cached, reused=True)
        async with self._hash_sem:
            snap = await asyncio.to_thread(snapshot_file, state.path, on_bytes=on_bytes)
        return await self.cache.add_snapshot(snap)

    # -- Save Page Now --------------------------------------------------------

    async def submit(
        self, url: str, *, reuse_recent: bool = True, **options: Any
    ) -> Submission:
        """Request a capture of ``url``. A successful capture within
        ``config.ttl.recent_capture`` is returned instead of resubmitting.
        Network failures raise ``TransportError``."""
        if reuse_recent:
            recent = await self.cache.recent_submission(
                url, self.config.ttl.recent_capture
            )
            if recent is not None:
                return recent
        sub = await spn.submit(
            self.transport, self.spn_limiter, self.config, url, options
        )
        await self.cache.save_submission(sub)
        return sub

    async def capture_status(self, job: str | Submission) -> Submission:
        """Job status; final (success/error) answers are cached, pending ones
        are always refreshed from the Archive."""
        job_id = job if isinstance(job, str) else job.job_id
        if job_id is None:
            raise ValueError("submission has no job_id")
        known = await self.cache.get_submission(job_id)
        if known is not None and known.is_final:
            return known
        previous = known or (job if isinstance(job, Submission) else None)
        sub = await spn.capture_status(
            self.transport, self.status_limiter, self.config, job_id, previous
        )
        if sub.error_code == spn.STATUS_EXPIRED:
            sub = await self._resolve_expired(sub)
        # Pending jobs are only written when first seen, so they can be resumed.
        if sub.is_final or known is None:
            await self.cache.save_submission(sub)
        return sub

    async def _resolve_expired(self, sub: Submission) -> Submission:
        """The Archive forgets job status after about an hour; look for a
        capture made since the submission instead."""
        start = sub.submitted_at - timedelta(minutes=5)
        try:
            lookup = await self._cdx.find(sub.url, Selection.first_after(start))
        except TransportError:
            return sub
        if lookup.capture is None:
            return sub
        return replace(
            sub,
            status=SubmissionStatus.SUCCESS,
            timestamp=lookup.capture.timestamp,
            original_url=lookup.capture.original,
            error_code=None,
            message="job status expired; capture found in the CDX index",
        )

    # -- captures and verification --------------------------------------------

    def _selection(
        self,
        timestamp: str | None,
        selection: Selection | None,
        snapshot: FileSnapshot | None,
        submission: Submission | None,
    ) -> Selection:
        if timestamp and selection:
            raise ValueError("pass either timestamp or selection, not both")
        if timestamp:
            return Selection.exact(timestamp)
        if selection:
            return selection
        if submission and submission.status is SubmissionStatus.SUCCESS:
            if submission.timestamp:
                return Selection.exact(submission.timestamp)
        return default_selection(snapshot)

    async def find_capture(
        self,
        url: str,
        *,
        timestamp: str | None = None,
        selection: Selection | None = None,
    ) -> Lookup:
        """CDX lookup of one capture. Raises ``TransportError``."""
        sel = self._selection(timestamp, selection, None, None)
        return await self._cdx.find(url, sel)

    async def verify_digest(
        self,
        url: str,
        local_digest: str,
        *,
        timestamp: str | None = None,
        selection: Selection | None = None,
        submission: Submission | None = None,
        snapshot: FileSnapshot | None = None,
        record: bool = True,
    ) -> VerificationResult:
        """Compare a precomputed SHA-1 (Base32, hex, or ``sha1:``-prefixed)
        with the selected capture of ``url``."""
        sel = self._selection(timestamp, selection, snapshot, submission)
        lookup_url = url
        if (
            submission is not None
            and submission.status is SubmissionStatus.SUCCESS
            and sel.timestamp == submission.timestamp
            and submission.original_url
        ):
            # SPN reports the URL it finally captured (after redirects).
            lookup_url = submission.original_url
        try:
            lookup = await self._cdx.find(lookup_url, sel)
        except TransportError as exc:
            result = error_result(
                url,
                Outcome.LOOKUP_ERROR,
                str(exc),
                local_digest=local_digest,
                snapshot=snapshot,
            )
        else:
            if lookup.capture is None and submission is None:
                submission = await self.cache.latest_submission(url)
            result = compare(
                url, local_digest, lookup, snapshot=snapshot, submission=submission
            )
        if record:
            await self.cache.add_verifications([result])
        return result

    async def verify_file(
        self,
        url: str,
        path: str | os.PathLike[str],
        *,
        timestamp: str | None = None,
        selection: Selection | None = None,
        submission: Submission | None = None,
        force_hash: bool = False,
        record: bool = True,
    ) -> VerificationResult:
        """Hash ``path`` (reusing the cached hash when the file is unchanged)
        and compare it with the selected capture of ``url``."""
        try:
            snap = await self.hash_file(path, force=force_hash)
        except OSError as exc:
            result = error_result(
                url, Outcome.FILE_ERROR, f"{type(exc).__name__}: {exc}"
            )
            if record:
                await self.cache.add_verifications([result])
            return result
        return await self.verify_digest(
            url,
            snap.sha1_b32,
            timestamp=timestamp,
            selection=selection,
            submission=submission,
            snapshot=snap,
            record=record,
        )

    async def check_access(
        self,
        capture: Capture | VerificationResult | tuple[str, str],
        *,
        force: bool = False,
        record: bool = True,
    ) -> AccessCheck:
        """Whether the capture's raw (``id_``) replay is served; checks newer
        than ``config.ttl.access_check`` are reused unless ``force``."""
        key = bulk._access_key(capture)
        if key is None:
            raise ValueError("no capture to check")
        original, timestamp = key
        if not force:
            cached = await self.cache.recent_access_check(
                original, timestamp, self.config.ttl.access_check
            )
            if cached is not None:
                return cached
        result = await access.check_access(
            self.transport, self.access_limiter, self.config, original, timestamp
        )
        if record:
            await self.cache.add_access_checks([result])
        return result

    # -- bulk -----------------------------------------------------------------

    async def submit_many(
        self,
        urls: Iterable[str] | AsyncIterable[str] | str | os.PathLike[str],
        *,
        progress: bulk.ProgressArg = False,
        reuse_recent: bool = True,
        **options: Any,
    ) -> list[Submission]:
        return await bulk.submit_many(
            self, urls, progress=progress, reuse_recent=reuse_recent, **options
        )

    async def wait_for_captures(
        self,
        submissions: Iterable[Submission] | None = None,
        *,
        poll_interval: float | None = None,
        timeout: float | None = None,
        progress: bulk.ProgressArg = False,
    ) -> list[Submission]:
        return await bulk.wait_for_captures(
            self,
            submissions,
            poll_interval=poll_interval,
            timeout=timeout,
            progress=progress,
        )

    async def hash_files(
        self,
        paths: str | os.PathLike[str] | Iterable[str | os.PathLike[str]],
        *,
        progress: bulk.ProgressArg = False,
        unit: str = "files",
        force: bool = False,
    ) -> list[FileSnapshot | bulk.HashFailure]:
        return await bulk.hash_files(
            self, paths, progress=progress, unit=unit, force=force
        )

    async def verify_many(
        self,
        items: bulk.VerifyInput,
        *,
        progress: bulk.ProgressArg = False,
        selection: Selection | None = None,
        force_hash: bool = False,
    ) -> list[VerificationResult]:
        return await bulk.verify_many(
            self, items, progress=progress, selection=selection, force_hash=force_hash
        )

    async def check_access_many(
        self,
        captures: Iterable[bulk.AccessTarget] | AsyncIterable[bulk.AccessTarget],
        *,
        progress: bulk.ProgressArg = False,
        force: bool = False,
    ) -> list[AccessCheck | None]:
        return await bulk.check_access_many(
            self, captures, progress=progress, force=force
        )

    # -- history and reporting ------------------------------------------------

    async def report(
        self,
        since: datetime | None = None,
        *,
        verifications: Iterable[VerificationResult] | None = None,
        submissions: Iterable[Submission] | None = None,
        access_checks: Iterable[AccessCheck | None] | None = None,
    ) -> FailureReport:
        """Failure report from the given results, or from the cache history
        (latest verification per URL and file, latest submission per URL and
        latest access check per capture) when none are given."""
        if verifications is None and submissions is None and access_checks is None:
            verifications = await self.cache.verifications(
                since=since, latest_only=True
            )
            subs = await self.cache.submissions(since=since)
            submissions = list({s.url: s for s in subs}.values())
            access_checks = await self.cache.access_checks(
                since=since, latest_only=True
            )
        return build_report(
            verifications=verifications or (),
            submissions=submissions or (),
            access_checks=access_checks or (),
            since=since,
        )

    async def history(self, url: str) -> dict[str, list[Any]]:
        return await self.cache.history(url)

    async def file_history(self, path: str | os.PathLike[str]) -> list[FileSnapshot]:
        return await self.cache.file_history(path)

    async def snapshots_by_digest(self, digest: str) -> list[FileSnapshot]:
        return await self.cache.snapshots_by_digest(normalize_digest(digest) or "")

    async def latest_verification(self, url: str) -> VerificationResult | None:
        return await self.cache.latest_verification(url)

    async def pending_submissions(self) -> list[Submission]:
        return await self.cache.pending_submissions()

    async def prune(self, older_than: timedelta | datetime) -> dict[str, int]:
        return await self.cache.prune(older_than)
