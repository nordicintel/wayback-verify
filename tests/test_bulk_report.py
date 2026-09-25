from __future__ import annotations

import io
import json
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from aioresponses import aioresponses
from aioresponses.core import CallbackResult
from helpers import CDX, SAVE, STATUS, calls, cdx_rows, fast_config, no_sleep
from tqdm import tqdm

from wayback_verify import (
    AccessCheck,
    ArchiveClient,
    HashFailure,
    Outcome,
    Submission,
    SubmissionStatus,
    VerificationResult,
    VerifyItem,
    build_report,
    sha1_base32,
)

BASE = "https://example.gov/files/"
RAW = re.compile(r"^https://web\.archive\.org/web/\d{14}id_/.*$")


def quiet_bar() -> tqdm:  # type: ignore[type-arg]
    return tqdm(file=io.StringIO())


def status_payload(job: str, url: str, ts: str = "20240301000000") -> dict[str, Any]:
    return {"status": "success", "job_id": job, "original_url": url, "timestamp": ts}


def spn_echo(url: str, **kwargs: Any) -> CallbackResult:
    target = kwargs["data"]["url"]
    return CallbackResult(payload={"url": target, "job_id": "job-" + target[-5:]})


# -- inputs -------------------------------------------------------------------


async def test_submit_many_dedupes_and_accepts_files(
    client: ArchiveClient, mock: aioresponses, tmp_path: Path
) -> None:
    mock.post(SAVE, callback=spn_echo, repeat=True)
    listing = tmp_path / "urls.txt"
    listing.write_text(
        f"# official sources\n{BASE}a.pdf\n\n{BASE}b.pdf\n{BASE}a.pdf\n",
        encoding="utf-8",
    )
    subs = await client.submit_many(listing)
    assert [s.url for s in subs] == [f"{BASE}a.pdf", f"{BASE}b.pdf"]
    assert len(calls(mock, "POST", "/save")) == 2
    # A second run finds the pending jobs in the cache and does not resubmit.
    again = await client.submit_many([f"{BASE}a.pdf", f"{BASE}b.pdf"])
    assert all(s.from_cache for s in again)
    assert len(calls(mock, "POST", "/save")) == 2


async def test_submit_many_accepts_async_iterables(
    client: ArchiveClient, mock: aioresponses
) -> None:
    mock.post(SAVE, callback=spn_echo, repeat=True)

    async def gen() -> AsyncIterator[str]:
        for name in ("x.csv", "y.csv", "x.csv"):
            yield BASE + name

    subs = await client.submit_many(gen(), progress=True)  # a real tqdm bar
    assert len(subs) == 2


async def test_submit_many_isolates_failures(
    client: ArchiveClient, mock: aioresponses
) -> None:
    def respond(url: str, **kwargs: Any) -> CallbackResult:
        target = kwargs["data"]["url"]
        if target.endswith("bad.pdf"):
            return CallbackResult(status=400, payload={"message": "Invalid URL"})
        return spn_echo(url, **kwargs)

    mock.post(SAVE, callback=respond, repeat=True)
    urls = [BASE + "a.pdf", BASE + "bad.pdf", BASE + "c.pdf"]
    subs = await client.submit_many(urls)
    assert [s.status for s in subs] == [
        SubmissionStatus.PENDING,
        SubmissionStatus.NOT_SUBMITTED,
        SubmissionStatus.PENDING,
    ]
    assert subs[1].error_code == "http-400"


async def test_rate_limit_pauses_and_retries_then_quota_stops(
    mock: aioresponses,
) -> None:
    config = fast_config(spn_rate_limit_retries=1)
    config = config.replace(spn_limit=config.spn_limit.__class__(1, 0))
    mock.post(SAVE, status=429, headers={"Retry-After": "0"})
    mock.post(SAVE, payload={"url": BASE + "a", "job_id": "j1"})
    mock.post(
        SAVE,
        payload={"status": "error", "status_ext": "error:max-daily-bandwidth"},
    )
    async with ArchiveClient(config, sleep=no_sleep) as client:
        paused: list[float] = []
        original_pause = client.spn_limiter.pause
        client.spn_limiter.pause = lambda s: (paused.append(s), original_pause(s))[1]  # type: ignore[method-assign]
        subs = await client.submit_many([BASE + "a", BASE + "b", BASE + "c"])
    assert paused == [0.0]
    assert subs[0].job_id == "j1"  # retried after the pause
    assert subs[1].error_code == "error:max-daily-bandwidth"
    assert subs[2].error_code == "skipped:quota"
    assert len(calls(mock, "POST", "/save")) == 3


async def test_network_errors_become_results(
    client: ArchiveClient, mock: aioresponses
) -> None:
    import aiohttp

    mock.post(SAVE, exception=aiohttp.ClientConnectionError("down"), repeat=True)
    [sub] = await client.submit_many([BASE + "a"])
    assert sub.status is SubmissionStatus.NOT_SUBMITTED
    assert sub.error_code == "network-error"
    assert (await client.cache.latest_submission(BASE + "a")) is not None


# -- waiting / resuming -------------------------------------------------------


async def test_wait_polls_until_final(
    client: ArchiveClient, mock: aioresponses
) -> None:
    mock.post(SAVE, payload={"url": BASE + "a", "job_id": "j1"})
    mock.get(STATUS, payload={"status": "pending", "job_id": "j1"})
    mock.get(STATUS, payload=status_payload("j1", BASE + "a"))
    subs = await client.submit_many([BASE + "a"])
    [done] = await client.wait_for_captures(subs)
    assert done.status is SubmissionStatus.SUCCESS
    assert done.timestamp == "20240301000000"


async def test_wait_times_out_as_pending(
    client: ArchiveClient, mock: aioresponses
) -> None:
    mock.get(STATUS, payload={"status": "pending", "job_id": "j1"}, repeat=True)
    sub = Submission(BASE + "a", SubmissionStatus.PENDING, job_id="j1")
    [still] = await client.wait_for_captures([sub], timeout=0)
    assert still.status is SubmissionStatus.PENDING
    assert still.message and "timed out" in still.message


async def test_resume_pending_jobs_from_cache(
    cache_path: Path, mock: aioresponses
) -> None:
    mock.post(SAVE, callback=spn_echo, repeat=True)
    async with ArchiveClient(fast_config(), cache_path, sleep=no_sleep) as first_run:
        await first_run.submit_many([BASE + "a.pdf", BASE + "b.pdf"])

    def status(url: str, **kwargs: Any) -> CallbackResult:
        job = str(url).rsplit("/", 1)[-1]
        return CallbackResult(payload=status_payload(job, BASE + job[-5:]))

    mock.get(STATUS, callback=status, repeat=True)
    async with ArchiveClient(fast_config(), cache_path, sleep=no_sleep) as second_run:
        done = await second_run.wait_for_captures()
        assert {d.status for d in done} == {SubmissionStatus.SUCCESS}
        assert len(done) == 2
        assert await second_run.pending_submissions() == []


async def test_wait_progress_bar_counts(
    client: ArchiveClient, mock: aioresponses
) -> None:
    mock.get(STATUS, payload=status_payload("j1", BASE + "a"))
    mock.get(STATUS, payload={"status": "error", "status_ext": "error:not-found"})
    subs = [
        Submission(BASE + "a", SubmissionStatus.PENDING, job_id="j1"),
        Submission(BASE + "b", SubmissionStatus.PENDING, job_id="j2"),
    ]
    with quiet_bar() as bar:
        await client.wait_for_captures(subs, progress=bar)
        assert bar.n == 2 and bar.total == 2


# -- hashing ------------------------------------------------------------------


async def test_hash_files_directory_glob_and_bytes_progress(
    client: ArchiveClient, tmp_path: Path
) -> None:
    tmp_path = tmp_path / "files"  # keep the cache database out of the tree
    (tmp_path / "sub").mkdir(parents=True)
    (tmp_path / "a.pdf").write_bytes(b"a" * 10)
    (tmp_path / "sub" / "b.csv").write_bytes(b"b" * 5)
    (tmp_path / "sub" / "c.pdf").write_bytes(b"c" * 3)
    snaps = await client.hash_files(tmp_path)
    assert len(snaps) == 3
    pdfs = await client.hash_files(str(tmp_path / "**" / "*.pdf"))
    assert {Path(s.path).name for s in pdfs if not isinstance(s, HashFailure)} == {
        "a.pdf",
        "c.pdf",
    }
    assert all(not isinstance(s, HashFailure) and s.reused for s in pdfs)
    with quiet_bar() as bar:
        await client.hash_files(tmp_path, progress=bar, unit="bytes", force=True)
        assert bar.total == 18 and bar.n == 18


async def test_hash_files_isolates_missing_files(
    client: ArchiveClient, tmp_path: Path
) -> None:
    (tmp_path / "ok.txt").write_bytes(b"ok")
    seen: list[tuple[Any, Any]] = []
    results = await client.hash_files(
        [tmp_path / "ok.txt", tmp_path / "gone.txt"],
        progress=lambda item, result: seen.append((item, result)),
    )
    assert not isinstance(results[0], HashFailure)
    assert isinstance(results[1], HashFailure)
    assert len(seen) == 2


# -- verification ---------------------------------------------------------------


async def test_verify_many_mixed_inputs_and_counts(
    client: ArchiveClient, mock: aioresponses, tmp_path: Path
) -> None:
    good = tmp_path / "good.pdf"
    good.write_bytes(b"good")
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"bad")
    ts = "20240301000000"
    digests = {
        BASE + "good.pdf": sha1_base32(b"good"),
        BASE + "bad.pdf": "Q" * 32,
        BASE + "digest.pdf": sha1_base32(b"precomputed"),
    }

    def cdx(url: str, **kwargs: Any) -> CallbackResult:
        target = str(kwargs["params"]["url"])
        if target.endswith("broken.pdf"):
            return CallbackResult(status=500)
        if target not in digests:
            return CallbackResult(payload=[])
        return CallbackResult(
            payload=cdx_rows(
                [ts, target, "200", "application/pdf", digests[target], "1"]
            )
        )

    mock.get(CDX, callback=cdx, repeat=True)
    counts: list[str] = []
    results = await client.verify_many(
        [
            (BASE + "good.pdf", good),
            (BASE + "bad.pdf", str(bad)),
            (BASE + "digest.pdf", f"sha1:{sha1_base32(b'precomputed')}"),
            VerifyItem(BASE + "none.pdf", digest="A" * 32),
            VerifyItem(BASE + "broken.pdf", digest="A" * 32, timestamp=ts),
            (BASE + "missing.pdf", tmp_path / "missing.pdf"),
        ],
        progress=lambda item, result: counts.append(result.outcome),
    )
    assert [r.outcome for r in results] == [
        Outcome.MATCH,
        Outcome.MISMATCH,
        Outcome.MATCH,
        Outcome.NO_CAPTURE,
        Outcome.LOOKUP_ERROR,
        Outcome.FILE_ERROR,
    ]
    assert sorted(counts) == sorted(r.outcome for r in results)
    # Written in one batch at the end, then read back from history.
    assert len(await client.cache.verifications()) == 6


async def test_verify_many_with_mapping_and_tqdm_postfix(
    client: ArchiveClient, mock: aioresponses, tmp_path: Path
) -> None:
    f = tmp_path / "a.pdf"
    f.write_bytes(b"a")
    mock.get(
        CDX,
        payload=cdx_rows(
            ["20240301000000", BASE + "a.pdf", "200", "x", sha1_base32(b"a"), "1"]
        ),
        repeat=True,
    )
    with quiet_bar() as bar:
        [result] = await client.verify_many({BASE + "a.pdf": f}, progress=bar)
        assert result.outcome is Outcome.MATCH
        assert bar.postfix is not None and "matched=1" in bar.postfix
        assert "pending=0" in bar.postfix


async def test_verify_many_accepts_submissions(
    client: ArchiveClient, mock: aioresponses
) -> None:
    sub = Submission(
        BASE + "a.pdf",
        SubmissionStatus.SUCCESS,
        job_id="j",
        timestamp="20240301000000",
        original_url=BASE + "a.pdf",
    )
    mock.get(
        CDX,
        payload=cdx_rows(["20240301000000", BASE + "a.pdf", "200", "x", "A" * 32, "1"]),
    )
    [result] = await client.verify_many([(sub, "A" * 32)])
    assert result.outcome is Outcome.MATCH


async def test_check_access_many_dedupes_and_keeps_none(
    client: ArchiveClient, mock: aioresponses
) -> None:
    mock.head(RAW, status=200, repeat=True)
    capture = ("https://example.gov/a.pdf", "20240301000000")
    results = await client.check_access_many([capture, None, capture])
    assert results[1] is None
    assert results[0] is results[2] and results[0] is not None
    assert results[0].accessible
    assert len(calls(mock, "HEAD", "id_")) == 1
    assert len(await client.cache.access_checks()) == 1


# -- reporting ------------------------------------------------------------------


async def test_report_groups_failures_with_file_properties(
    client: ArchiveClient, mock: aioresponses, tmp_path: Path
) -> None:
    f = tmp_path / "a.pdf"
    f.write_bytes(b"local")
    ts = "20240301000000"
    mock.get(
        CDX,
        payload=cdx_rows([ts, BASE + "a.pdf", "200", "x", "Q" * 32, "1"]),
        repeat=True,
    )
    mock.post(
        SAVE, status=200, payload={"status": "error", "status_ext": "error:blocked"}
    )
    mock.head(RAW, status=404, repeat=True)
    mock.get(RAW, status=404, repeat=True)

    mismatch = await client.verify_file(BASE + "a.pdf", f, timestamp=ts)
    await client.submit(BASE + "blocked.pdf")
    await client.check_access(mismatch)

    report = await client.report()
    assert report.counts == {
        "failed_captures": 1,
        "mismatches": 1,
        "digest_unavailable": 0,
        "inaccessible": 1,
        "unverified": 0,
    }
    entry = report.mismatches[0]
    assert entry.file is not None and entry.file["size"] == 5
    assert {"mtime", "ctime", "birthtime", "hashed_at", "path"} <= entry.file.keys()
    assert report.failed_captures[0].error_code == "error:blocked"
    data = json.loads(report.to_json())
    assert data["counts"]["mismatches"] == 1 and not report.ok


def test_build_report_from_results() -> None:
    results = [
        VerificationResult("u1", Outcome.MATCH, "A"),
        VerificationResult("u2", Outcome.DIGEST_UNAVAILABLE, "A"),
        VerificationResult("u3", Outcome.LOOKUP_ERROR, "A", note="timeout"),
        VerificationResult("u4", Outcome.CAPTURE_FAILED, "A", note="blocked"),
    ]
    subs = [Submission("u4", SubmissionStatus.ERROR, job_id="j", error_code="e")]
    access = [AccessCheck("u1", "20240101000000", 200, True), None]
    report = build_report(verifications=results, submissions=subs, access_checks=access)
    assert report.counts["digest_unavailable"] == 1
    assert report.counts["unverified"] == 1
    assert report.counts["failed_captures"] == 1  # one entry per URL
    assert report.counts["inaccessible"] == 0
    assert report.to_dict()["generated_at"]
