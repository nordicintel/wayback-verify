"""Bulk submit / wait / hash / verify / access checks with progress tracking."""

from __future__ import annotations

import asyncio
import glob
import os
import time
from collections import Counter
from collections.abc import (
    AsyncIterable,
    Awaitable,
    Callable,
    Iterable,
    Mapping,
    Sequence,
)
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tqdm import tqdm as tqdm_base
from tqdm.asyncio import tqdm as tqdm_asyncio

from .cdx import Selection
from .files import is_digest
from .http import TransportError
from .models import (
    AccessCheck,
    Capture,
    FileSnapshot,
    Outcome,
    Submission,
    SubmissionStatus,
    VerificationResult,
    utcnow,
)
from .spn import QUOTA_ERRORS, RATE_LIMIT_ERRORS
from .verify import error_result

if TYPE_CHECKING:
    from .client import ArchiveClient

ProgressCallback = Callable[[Any, Any], None]
ProgressArg = bool | tqdm_base | ProgressCallback | None  # type: ignore[type-arg]
PathArg = str | os.PathLike[str]


# -- progress ---------------------------------------------------------------


class Progress:
    """Adapts ``progress=`` (bool, tqdm instance, or ``on_progress(item,
    result)`` callable) and keeps running counts shown as the bar postfix."""

    def __init__(
        self,
        progress: ProgressArg,
        total: int,
        desc: str,
        *,
        unit: str = "it",
        unit_scale: bool = False,
    ) -> None:
        self.total = total
        self.done = 0
        self.counts: Counter[str] = Counter()
        self.bar: tqdm_base | None = None  # type: ignore[type-arg]
        self.callback: ProgressCallback | None = None
        self._own_bar = False
        self.counts_items = unit != "B"
        if progress is True:
            self.bar = tqdm_asyncio(
                total=total, desc=desc, unit=unit, unit_scale=unit_scale
            )
            self._own_bar = True
        elif isinstance(progress, tqdm_base):
            self.bar = progress
            if progress.total is None:
                progress.total = total
                progress.refresh()
        elif callable(progress):
            self.callback = progress

    def advance(self, item: Any, result: Any, label: str | None) -> None:
        self.done += 1
        if label:
            self.counts[label] += 1
        if self.bar is not None:
            if self.counts_items:
                self.bar.update(1)
            self.bar.set_postfix(
                {**self.counts, "pending": self.total - self.done}, refresh=False
            )
        if self.callback is not None:
            self.callback(item, result)

    def add(self, n: int) -> None:
        """Advance a byte-based bar."""
        if self.bar is not None and not self.counts_items:
            self.bar.update(n)

    def close(self) -> None:
        if self.bar is not None:
            self.bar.refresh()
            if self._own_bar:
                self.bar.close()


async def run_bulk[I, R](
    items: Sequence[I],
    worker: Callable[[I], Awaitable[R]],
    *,
    concurrency: int,
    progress: Progress,
    label: Callable[[R], str | None],
    on_error: Callable[[I, Exception], R],
) -> list[R]:
    """Run ``worker`` over ``items`` with at most ``concurrency`` in flight.

    Results keep input order. An exception from one item becomes that item's
    ``on_error`` result and never aborts the batch.
    """
    results: list[R | None] = [None] * len(items)
    indices = iter(range(len(items)))

    async def run() -> None:
        for i in indices:
            try:
                result = await worker(items[i])
            except Exception as exc:  # isolated per item
                result = on_error(items[i], exc)
            results[i] = result
            progress.advance(items[i], result, label(result))

    try:
        await asyncio.gather(*(run() for _ in range(min(concurrency, len(items)))))
    finally:
        progress.close()
    return results  # type: ignore[return-value]


class WriteBuffer[T]:
    """Collects results and writes them in one transaction per chunk."""

    def __init__(self, write: Callable[[list[T]], Awaitable[None]], size: int) -> None:
        self._write = write
        self._size = max(1, size)
        self._items: list[T] = []

    async def add(self, item: T) -> None:
        self._items.append(item)
        if len(self._items) >= self._size:
            await self.flush()

    async def flush(self) -> None:
        items, self._items = self._items, []
        if items:
            await self._write(items)


# -- inputs -----------------------------------------------------------------


async def collect[T](items: Iterable[T] | AsyncIterable[T]) -> list[T]:
    if isinstance(items, AsyncIterable):
        return [item async for item in items]
    return list(items)


def read_url_file(path: PathArg) -> list[str]:
    """One URL per line; blank lines and ``#`` comments are skipped."""
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return [s for line in lines if (s := line.strip()) and not s.startswith("#")]


async def collect_urls(
    urls: Iterable[str] | AsyncIterable[str] | PathArg,
) -> list[str]:
    if isinstance(urls, os.PathLike) or (
        isinstance(urls, str) and os.path.isfile(urls)
    ):
        return read_url_file(urls)
    if isinstance(urls, str):
        raise TypeError("pass an iterable of URLs or the path of a URL list file")
    return [u.strip() for u in await collect(urls) if u and u.strip()]


def dedupe[T](items: Iterable[T]) -> list[T]:
    return list(dict.fromkeys(items))


def expand_paths(paths: PathArg | Iterable[PathArg]) -> list[Path]:
    """Files named by paths, directories (recursive) or glob patterns."""
    if isinstance(paths, str | os.PathLike):
        paths = [paths]
    out: list[Path] = []
    for p in paths:
        s = os.fspath(p)
        if os.path.isdir(s):
            out.extend(sorted(q for q in Path(s).rglob("*") if q.is_file()))
        elif glob.has_magic(s):
            out.extend(
                sorted(
                    Path(q) for q in glob.glob(s, recursive=True) if os.path.isfile(q)
                )
            )
        else:
            out.append(Path(s))
    return dedupe(out)


# -- submit / wait ----------------------------------------------------------


def _submission_label(sub: Submission) -> str:
    return str(sub.status)


async def submit_many(
    client: ArchiveClient,
    urls: Iterable[str] | AsyncIterable[str] | PathArg,
    *,
    progress: ProgressArg = False,
    reuse_recent: bool = True,
    **options: Any,
) -> list[Submission]:
    """Submit each distinct URL once, in input order.

    URLs with a successful capture (or a still-pending job) within
    ``config.ttl.recent_capture`` are not resubmitted. Rate-limit answers pause
    every further submission for Retry-After (or ``spn_rate_limit_backoff``)
    and retry that URL at most ``spn_rate_limit_retries`` times; a daily quota
    error stops the batch and marks the remaining URLs ``skipped:quota``.
    """
    config = client.config
    unique = dedupe(await collect_urls(urls))
    limiter = client.spn_limiter
    quota: dict[str, str] = {}

    async def one(url: str) -> Submission:
        if reuse_recent:
            recent = await client.cache.recent_submission(
                url,
                config.ttl.recent_capture,
                (SubmissionStatus.SUCCESS, SubmissionStatus.PENDING),
            )
            if recent is not None:
                return recent
        sub: Submission | None = None
        for attempt in range(config.spn_rate_limit_retries + 1):
            if quota:
                return _not_submitted(
                    url, "skipped:quota", f"not submitted after {quota['code']}"
                )
            sub = await client.submit(url, reuse_recent=False, **options)
            code = sub.error_code or ""
            if code in RATE_LIMIT_ERRORS:
                limiter.pause(sub.retry_after or config.spn_rate_limit_backoff)
                if attempt < config.spn_rate_limit_retries:
                    continue
            elif code in QUOTA_ERRORS:
                quota.setdefault("code", code)
            return sub
        assert sub is not None
        return sub

    def on_error(url: str, exc: Exception) -> Submission:
        return _not_submitted(url, "network-error", str(exc))

    bar = Progress(progress, len(unique), "submit")
    results = await run_bulk(
        unique,
        one,
        concurrency=max(1, config.effective_spn_limit.concurrency),
        progress=bar,
        label=_submission_label,
        on_error=on_error,
    )
    failed = [r for r in results if r.error_code in ("network-error", "skipped:quota")]
    for r in failed:
        await client.cache.save_submission(r)
    return results


def _not_submitted(url: str, code: str, message: str) -> Submission:
    return Submission(
        url=url,
        status=SubmissionStatus.NOT_SUBMITTED,
        error_code=code,
        message=message,
    )


async def wait_for_captures(
    client: ArchiveClient,
    submissions: Iterable[Submission] | None = None,
    *,
    poll_interval: float | None = None,
    timeout: float | None = None,
    progress: ProgressArg = False,
) -> list[Submission]:
    """Poll pending jobs until each succeeds, fails, or ``timeout`` expires.

    With no ``submissions`` every pending job stored in the cache is resumed.
    Jobs still pending at the deadline are returned as pending.
    """
    config = client.config
    subs = (
        list(submissions)
        if submissions is not None
        else await client.cache.pending_submissions()
    )
    interval = config.poll_interval if poll_interval is None else poll_interval
    deadline = time.monotonic() + (config.wait_timeout if timeout is None else timeout)

    async def one(sub: Submission) -> Submission:
        if sub.is_final or sub.job_id is None:
            return sub
        current = sub
        while True:
            try:
                current = await client.capture_status(sub.job_id)
            except TransportError as exc:
                current = replace(current, message=f"status check failed: {exc}")
            if current.is_final:
                return current
            if time.monotonic() + interval > deadline:
                return replace(
                    current,
                    message=current.message or "still pending when the wait timed out",
                )
            await client.sleep(interval)

    def on_error(sub: Submission, exc: Exception) -> Submission:
        return replace(sub, message=f"status check failed: {exc}")

    bar = Progress(progress, len(subs), "capture")
    return await run_bulk(
        subs,
        one,
        concurrency=max(1, len(subs)),
        progress=bar,
        label=_submission_label,
        on_error=on_error,
    )


# -- hashing ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HashFailure:
    path: str
    error: str


async def hash_files(
    client: ArchiveClient,
    paths: PathArg | Iterable[PathArg],
    *,
    progress: ProgressArg = False,
    unit: str = "files",
    force: bool = False,
) -> list[FileSnapshot | HashFailure]:
    """Hash files (paths, directories or globs) on a bounded worker pool.

    ``unit="bytes"`` sizes the progress bar in bytes instead of files.
    """
    files = expand_paths(paths)
    loop = asyncio.get_running_loop()
    by_bytes = unit == "bytes"
    sizes = {}
    if by_bytes:
        for f in files:
            try:
                sizes[f] = f.stat().st_size
            except OSError:
                sizes[f] = 0
    bar = Progress(
        progress,
        sum(sizes.values()) if by_bytes else len(files),
        "hash",
        unit="B" if by_bytes else "file",
        unit_scale=by_bytes,
    )

    def on_bytes(n: int) -> None:
        loop.call_soon_threadsafe(bar.add, n)

    async def one(path: Path) -> FileSnapshot | HashFailure:
        snap = await client.hash_file(
            path, force=force, on_bytes=on_bytes if by_bytes else None
        )
        if snap.reused:
            bar.add(sizes.get(path, snap.size))
        return snap

    def on_error(path: Path, exc: Exception) -> HashFailure:
        bar.add(sizes.get(path, 0))
        return HashFailure(str(path), f"{type(exc).__name__}: {exc}")

    def label(r: FileSnapshot | HashFailure) -> str:
        if isinstance(r, HashFailure):
            return "failed"
        return "reused" if r.reused else "hashed"

    return await run_bulk(
        files,
        one,
        concurrency=max(1, client.config.hash_workers),
        progress=bar,
        label=label,
        on_error=on_error,
    )


# -- verification -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VerifyItem:
    """One verification: a URL (or its ``Submission``) and a file or digest."""

    url: str
    path: Path | None = None
    digest: str | None = None
    submission: Submission | None = None
    timestamp: str | None = None
    selection: Selection | None = None


VerifyTarget = PathArg | FileSnapshot
VerifyInput = (
    Mapping[str, VerifyTarget]
    | Iterable[VerifyItem | tuple[str | Submission, VerifyTarget]]
    | AsyncIterable[VerifyItem | tuple[str | Submission, VerifyTarget]]
)


def to_verify_item(
    key: str | Submission | VerifyItem, target: VerifyTarget | None = None
) -> VerifyItem:
    if isinstance(key, VerifyItem):
        return key
    sub = key if isinstance(key, Submission) else None
    url = sub.url if sub else str(key)
    if isinstance(target, FileSnapshot):
        return VerifyItem(url, digest=target.sha1_b32, submission=sub)
    if isinstance(target, str) and is_digest(target) and not os.path.exists(target):
        return VerifyItem(url, digest=target, submission=sub)
    if target is None:
        raise ValueError(f"no file or digest given for {url}")
    return VerifyItem(url, path=Path(target), submission=sub)


async def normalize_verify_input(items: VerifyInput) -> list[VerifyItem]:
    if isinstance(items, Mapping):
        return [to_verify_item(k, v) for k, v in items.items()]
    out = []
    for entry in await collect(items):
        if isinstance(entry, VerifyItem):
            out.append(entry)
        else:
            key, target = entry
            out.append(to_verify_item(key, target))
    return out


_VERIFY_LABELS = {Outcome.MATCH: "matched", Outcome.MISMATCH: "mismatched"}


def _verify_label(r: VerificationResult) -> str:
    return _VERIFY_LABELS.get(r.outcome, "failed")


async def verify_one(
    client: ArchiveClient, item: VerifyItem, *, record: bool, force_hash: bool
) -> VerificationResult:
    kwargs: dict[str, Any] = {
        "timestamp": item.timestamp,
        "selection": item.selection,
        "submission": item.submission,
        "record": record,
    }
    if item.path is not None:
        return await client.verify_file(
            item.url, item.path, force_hash=force_hash, **kwargs
        )
    assert item.digest is not None
    return await client.verify_digest(item.url, item.digest, **kwargs)


async def verify_many(
    client: ArchiveClient,
    items: VerifyInput,
    *,
    progress: ProgressArg = False,
    selection: Selection | None = None,
    force_hash: bool = False,
) -> list[VerificationResult]:
    """Verify many (URL, file-or-digest) pairs; results keep input order.

    ``selection`` applies to items without their own timestamp, selection or
    successful submission.
    """
    work = await normalize_verify_input(items)
    if selection is not None:
        work = [
            w if (w.timestamp or w.selection) else replace(w, selection=selection)
            for w in work
        ]
    buffer: WriteBuffer[VerificationResult] = WriteBuffer(
        client.cache.add_verifications, client.config.cache_batch_size
    )

    async def one(item: VerifyItem) -> VerificationResult:
        result = await verify_one(client, item, record=False, force_hash=force_hash)
        await buffer.add(result)
        return result

    def on_error(item: VerifyItem, exc: Exception) -> VerificationResult:
        outcome = Outcome.LOOKUP_ERROR
        if isinstance(exc, OSError) and not isinstance(exc, TransportError):
            outcome = Outcome.FILE_ERROR
        return error_result(item.url, outcome, f"{type(exc).__name__}: {exc}")

    bar = Progress(progress, len(work), "verify")
    try:
        return await run_bulk(
            work,
            one,
            concurrency=max(1, client.config.bulk_concurrency),
            progress=bar,
            label=_verify_label,
            on_error=on_error,
        )
    finally:
        await buffer.flush()


# -- access -----------------------------------------------------------------

AccessTarget = Capture | VerificationResult | tuple[str, str] | None


def _access_key(target: AccessTarget) -> tuple[str, str] | None:
    if isinstance(target, VerificationResult):
        target = target.capture
    if target is None:
        return None
    if isinstance(target, Capture):
        return target.original, target.timestamp
    return target


async def check_access_many(
    client: ArchiveClient,
    captures: Iterable[AccessTarget] | AsyncIterable[AccessTarget],
    *,
    progress: ProgressArg = False,
    force: bool = False,
) -> list[AccessCheck | None]:
    """Check each capture once; ``None`` entries (no capture) stay ``None``."""
    targets = [_access_key(t) for t in await collect(captures)]
    unique = dedupe(t for t in targets if t is not None)
    buffer: WriteBuffer[AccessCheck] = WriteBuffer(
        client.cache.add_access_checks, client.config.cache_batch_size
    )

    async def one(key: tuple[str, str]) -> AccessCheck:
        check = await client.check_access(key, force=force, record=False)
        if not check.from_cache:
            await buffer.add(check)
        return check

    def on_error(key: tuple[str, str], exc: Exception) -> AccessCheck:
        return AccessCheck(
            original=key[0],
            timestamp=key[1],
            http_status=None,
            accessible=False,
            checked_at=utcnow(),
            error=f"{type(exc).__name__}: {exc}",
        )

    def label(a: AccessCheck) -> str:
        return "accessible" if a.accessible else "inaccessible"

    bar = Progress(progress, len(unique), "access")
    try:
        checked = await run_bulk(
            unique,
            one,
            concurrency=max(1, client.config.bulk_concurrency),
            progress=bar,
            label=label,
            on_error=on_error,
        )
    finally:
        await buffer.flush()
    by_key = dict(zip(unique, checked, strict=True))
    return [by_key[t] if t is not None else None for t in targets]
