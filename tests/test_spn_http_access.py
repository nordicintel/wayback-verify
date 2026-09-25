from __future__ import annotations

import asyncio
import time
from datetime import timedelta
from urllib.parse import parse_qs

import aiohttp
import pytest
from aioresponses import aioresponses
from helpers import CDX, SAVE, STATUS, calls, cdx_rows, fast_config, load, no_sleep

from wayback_verify import (
    ArchiveClient,
    Capture,
    Config,
    RateLimit,
    RetryPolicy,
    Submission,
    SubmissionStatus,
)
from wayback_verify.http import (
    RateLimiter,
    Transport,
    TransportError,
    parse_retry_after,
)
from wayback_verify.models import utcnow

URL = "http://brewster.kahle.org/"
JOB = "ac58789b-f3ca-48d0-9ea6-1d1225e98695"
RAW = "https://web.archive.org/web/20180326070330id_/http://brewster.kahle.org/"


# -- Save Page Now ------------------------------------------------------------


async def test_submit_posts_with_auth_and_options(
    client: ArchiveClient, mock: aioresponses
) -> None:
    mock.post(SAVE, payload={"url": URL, "job_id": JOB})
    sub = await client.submit(URL, force_get=True, js_behavior_timeout=0)
    assert sub.status is SubmissionStatus.PENDING and sub.job_id == JOB
    [call] = calls(mock, "POST", "/save")
    headers = call.kwargs["headers"]
    assert headers["Authorization"] == "LOW key:secret"
    assert headers["Accept"] == "application/json"
    assert call.kwargs["data"] == {
        "url": URL,
        "force_get": "1",
        "js_behavior_timeout": "0",
    }
    [pending] = await client.pending_submissions()
    assert pending.job_id == JOB


async def test_anonymous_submit_has_no_authorization(mock: aioresponses) -> None:
    mock.post(SAVE, payload={"url": URL, "job_id": JOB})
    async with ArchiveClient(fast_config(credentials=None), sleep=no_sleep) as c:
        await c.submit(URL)
    [call] = calls(mock, "POST", "/save")
    assert "Authorization" not in call.kwargs["headers"]


@pytest.mark.parametrize(
    ("status", "payload", "headers", "code", "retry_after"),
    [
        (
            429,
            {"message": "Too many requests"},
            {"Retry-After": "30"},
            "http-429",
            30.0,
        ),
        (
            200,
            {
                "status": "error",
                "status_ext": "error:user-session-limit",
                "message": "x",
            },
            {},
            "error:user-session-limit",
            None,
        ),
        (401, {"message": "bad key"}, {}, "http-401", None),
    ],
)
async def test_submit_error_mapping(
    client: ArchiveClient,
    mock: aioresponses,
    status: int,
    payload: dict[str, str],
    headers: dict[str, str],
    code: str,
    retry_after: float | None,
) -> None:
    mock.post(SAVE, status=status, payload=payload, headers=headers)
    sub = await client.submit(URL)
    assert sub.status is SubmissionStatus.NOT_SUBMITTED
    assert sub.error_code == code and sub.retry_after == retry_after
    assert len(calls(mock, "POST", "/save")) == 1  # never retried in a loop


async def test_submit_reuses_recent_success(
    client: ArchiveClient, mock: aioresponses
) -> None:
    await client.cache.save_submission(
        Submission(
            URL, SubmissionStatus.SUCCESS, job_id="old", timestamp="20240101000000"
        )
    )
    sub = await client.submit(URL)
    assert sub.from_cache and sub.job_id == "old"
    assert calls(mock, "POST", "/save") == []


@pytest.mark.parametrize(
    ("fixture", "status", "check"),
    [
        (
            "spn_status_success.json",
            SubmissionStatus.SUCCESS,
            lambda s: (
                s.timestamp == "20180326070330"
                and s.capture_url == "https://web.archive.org/web/20180326070330/" + URL
            ),
        ),
        ("spn_status_pending.json", SubmissionStatus.PENDING, lambda s: True),
        (
            "spn_status_error.json",
            SubmissionStatus.ERROR,
            lambda s: (
                s.error_code == "error:invalid-host-resolution"
                and "resolve host" in (s.message or "")
            ),
        ),
    ],
)
async def test_status_mapping(
    client: ArchiveClient,
    mock: aioresponses,
    fixture: str,
    status: object,
    check: object,
) -> None:
    mock.get(STATUS, payload=load(fixture))
    sub = await client.capture_status(JOB)
    assert sub.status is status
    assert check(sub)  # type: ignore[operator]


async def test_final_status_is_cached_pending_is_refreshed(
    client: ArchiveClient, mock: aioresponses
) -> None:
    mock.get(STATUS, payload=load("spn_status_pending.json"))
    mock.get(STATUS, payload=load("spn_status_success.json"))
    assert (await client.capture_status(JOB)).status is SubmissionStatus.PENDING
    assert (await client.capture_status(JOB)).status is SubmissionStatus.SUCCESS
    cached = await client.capture_status(JOB)
    assert cached.from_cache and cached.status is SubmissionStatus.SUCCESS
    assert len(calls(mock, "GET", "/save/status/")) == 2


async def test_expired_status_resolves_from_cdx(
    client: ArchiveClient, mock: aioresponses
) -> None:
    sub = Submission(URL, SubmissionStatus.PENDING, job_id=JOB, submitted_at=utcnow())
    await client.cache.save_submission(sub)
    mock.get(STATUS, status=404, body="not found")
    later = (utcnow() + timedelta(minutes=1)).strftime("%Y%m%d%H%M%S")
    mock.get(CDX, payload=cdx_rows([later, URL, "200", "text/html", "A" * 32, "1"]))
    resolved = await client.capture_status(JOB)
    assert resolved.status is SubmissionStatus.SUCCESS and resolved.timestamp == later


# -- HTTP retries and throttling ----------------------------------------------


def test_parse_retry_after() -> None:
    assert parse_retry_after("12") == 12.0
    assert parse_retry_after(None) is None
    assert parse_retry_after("garbage") is None
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0


async def test_retries_transient_errors_and_honors_retry_after(
    mock: aioresponses,
) -> None:
    sleeps: list[float] = []

    async def record(seconds: float) -> None:
        sleeps.append(seconds)

    mock.get("https://x.test/a", status=503, headers={"Retry-After": "7"})
    mock.get("https://x.test/a", exception=aiohttp.ClientConnectionError("reset"))
    mock.get("https://x.test/a", status=200, body="ok")
    async with aiohttp.ClientSession() as session:
        transport = Transport(
            session,
            RetryPolicy(max_attempts=4, base_delay=1, max_delay=60),
            sleep=record,
        )
        resp = await transport.request(
            "GET", "https://x.test/a", limiter=RateLimiter(RateLimit(1))
        )
    assert resp.status == 200 and resp.text == "ok" and resp.attempts == 3
    assert sleeps[0] == 7  # Retry-After
    assert 1.6 <= sleeps[1] <= 2.4  # exponential backoff with jitter


async def test_retries_are_bounded(mock: aioresponses) -> None:
    mock.get("https://x.test/b", exception=aiohttp.ClientConnectionError(), repeat=True)
    async with aiohttp.ClientSession() as session:
        transport = Transport(session, RetryPolicy(max_attempts=2), sleep=no_sleep)
        with pytest.raises(TransportError):
            await transport.request(
                "GET", "https://x.test/b", limiter=RateLimiter(RateLimit(1))
            )
    assert len(calls(mock, "GET", "x.test/b")) == 2


async def test_rate_limiter_spacing_and_pause() -> None:
    limiter = RateLimiter(RateLimit(concurrency=5, min_interval=0.05))
    starts: list[float] = []

    async def hit() -> None:
        async with limiter:
            starts.append(time.monotonic())

    await asyncio.gather(*(hit() for _ in range(4)))
    gaps = [b - a for a, b in zip(starts, starts[1:], strict=False)]
    assert all(g >= 0.045 for g in gaps)
    limiter.pause(0.1)
    before = time.monotonic()
    await hit()
    assert starts[-1] - before >= 0.09


async def test_rate_limiter_concurrency_cap() -> None:
    limiter = RateLimiter(RateLimit(concurrency=2))
    active = peak = 0

    async def hit() -> None:
        nonlocal active, peak
        async with limiter:
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1

    await asyncio.gather(*(hit() for _ in range(6)))
    assert peak == 2


# -- access checks ------------------------------------------------------------

CAPTURE = Capture(URL, "20180326070330", 200, "text/html", "A" * 32, 1)


async def test_access_head_ok(client: ArchiveClient, mock: aioresponses) -> None:
    mock.head(RAW, status=200)
    check = await client.check_access(CAPTURE)
    assert check.accessible and check.method == "HEAD" and not check.redirected
    assert check.http_status == 200
    cached = await client.check_access(CAPTURE)
    assert cached.from_cache and len(calls(mock, "HEAD", "id_")) == 1


async def test_access_falls_back_to_ranged_get(
    client: ArchiveClient, mock: aioresponses
) -> None:
    mock.head(RAW, status=405)
    mock.get(RAW, status=206, body=b"<")
    check = await client.check_access(CAPTURE)
    assert check.accessible and check.method == "GET" and check.http_status == 206
    [get] = calls(mock, "GET", "id_")
    assert get.kwargs["headers"]["Range"] == "bytes=0-0"


async def test_access_detects_redirect_to_other_capture(
    client: ArchiveClient, mock: aioresponses
) -> None:
    other = "https://web.archive.org/web/20190101000000id_/http://brewster.kahle.org/"
    mock.head(RAW, status=302, headers={"Location": other})
    mock.head(other, status=200)
    # aioresponses follows a redirected HEAD with GET (pnuckowski/aioresponses#296).
    mock.get(other, status=200)
    check = await client.check_access(CAPTURE)
    assert check.redirected and not check.accessible
    assert check.final_timestamp == "20190101000000"


async def test_access_failure_is_recorded(
    client: ArchiveClient, mock: aioresponses
) -> None:
    mock.head(RAW, status=404)
    mock.get(RAW, status=404)
    check = await client.check_access(CAPTURE)
    assert not check.accessible and check.http_status == 404
    [stored] = await client.cache.access_checks()
    assert not stored.accessible


async def test_default_config_uses_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IA_ACCESS_KEY_ID", "EA")
    monkeypatch.setenv("IA_SECRET_ACCESS_KEY", "ES")
    client = ArchiveClient()
    assert isinstance(client.config, Config)
    assert client.config.credentials is not None
    with pytest.raises(RuntimeError):
        _ = client.cache


def test_form_encoding_roundtrip() -> None:
    # SPN treats anything other than "1"/"on" as off: booleans must become "1".
    from wayback_verify.spn import _options

    assert _options({"a": True, "b": False, "c": 5, "d": None}) == {
        "a": "1",
        "b": "0",
        "c": "5",
    }
    assert parse_qs("url=x&force_get=1") == {"url": ["x"], "force_get": ["1"]}
