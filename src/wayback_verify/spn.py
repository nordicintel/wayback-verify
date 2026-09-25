"""Save Page Now 2 (SPN2) submission and job status.

API reference: "Save Page Now 2 Public API Docs" (Google Doc linked from
https://web.archive.org/save), revision of 2026-07-22, checked 2026-09-25.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from .config import Config
from .http import RateLimiter, Response, Transport, TransportError
from .models import Submission, SubmissionStatus, utcnow

# Codes that mean "slow down": the item may be retried after a pause.
RATE_LIMIT_ERRORS = frozenset({"http-429", "error:user-session-limit"})
# Account-, IP- or host-wide daily limits: further submissions will fail too.
QUOTA_ERRORS = frozenset(
    {
        "error:max-daily-bandwidth",
        "error:max-daily-bandwidth-from-ip",
        "error:max-daily-bandwidth-host",
    }
)
# Our own code for a status job the Archive no longer remembers.
STATUS_EXPIRED = "status-expired"


def _options(options: Mapping[str, Any]) -> dict[str, str]:
    """SPN2 treats anything but "1"/"on" as off, so booleans become "1"/"0"."""
    out: dict[str, str] = {}
    for key, value in options.items():
        if isinstance(value, bool):
            out[key] = "1" if value else "0"
        elif value is not None:
            out[key] = str(value)
    return out


def _json(resp: Response) -> dict[str, Any] | None:
    try:
        data = json.loads(resp.body)
    except (ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def headers(config: Config) -> dict[str, str]:
    h = {"Accept": "application/json", "User-Agent": config.user_agent}
    if config.credentials:
        h["Authorization"] = config.credentials.authorization
    return h


def parse_submit_response(url: str, resp: Response) -> Submission:
    now = utcnow()
    data = _json(resp)
    base: dict[str, Any] = {"url": url, "submitted_at": now, "updated_at": now}
    if resp.status == 429:
        return Submission(
            **base,
            status=SubmissionStatus.NOT_SUBMITTED,
            error_code="http-429",
            message=(data or {}).get("message") or "rate limited by Save Page Now",
            retry_after=resp.retry_after,
            raw=data,
        )
    if data and data.get("job_id"):
        return Submission(
            **base,
            status=SubmissionStatus.PENDING,
            job_id=str(data["job_id"]),
            message=data.get("message") or None,
            raw=data,
        )
    if data and (data.get("status") == "error" or data.get("status_ext")):
        return Submission(
            **base,
            status=SubmissionStatus.NOT_SUBMITTED,
            error_code=data.get("status_ext") or "error:unknown",
            message=data.get("message"),
            retry_after=resp.retry_after,
            raw=data,
        )
    message = (data or {}).get("message") or resp.text[:500] or None
    return Submission(
        **base,
        status=SubmissionStatus.NOT_SUBMITTED,
        error_code=f"http-{resp.status}",
        message=message,
        retry_after=resp.retry_after,
        raw=data,
    )


def parse_status_response(
    job_id: str, resp: Response, previous: Submission | None = None
) -> Submission:
    data = _json(resp) or {}
    now = utcnow()
    url = (previous.url if previous else None) or data.get("original_url") or ""
    submitted_at = previous.submitted_at if previous else now
    common: dict[str, Any] = {
        "url": url,
        "job_id": job_id,
        "submitted_at": submitted_at,
        "updated_at": now,
        "raw": data or None,
    }
    status = data.get("status")
    if status == "success":
        return Submission(
            **common,
            status=SubmissionStatus.SUCCESS,
            timestamp=str(data.get("timestamp") or "") or None,
            original_url=data.get("original_url") or url,
            message=data.get("message"),
        )
    if status == "error":
        return Submission(
            **common,
            status=SubmissionStatus.ERROR,
            error_code=data.get("status_ext") or "error:unknown",
            message=data.get("message") or data.get("exception"),
        )
    if status == "pending":
        return Submission(**common, status=SubmissionStatus.PENDING)
    raise TransportError(
        f"unexpected status response for job {job_id}: HTTP {resp.status} "
        f"{resp.text[:200]!r}",
        status=resp.status,
    )


async def submit(
    transport: Transport,
    limiter: RateLimiter,
    config: Config,
    url: str,
    options: Mapping[str, Any] | None = None,
) -> Submission:
    """POST a capture request. Network failures raise ``TransportError``;
    everything the Archive answers (including 429) becomes a ``Submission``."""
    data = {"url": url, **_options({**config.spn_options, **(options or {})})}
    resp = await transport.request(
        "POST",
        f"{config.base_url}/save",
        limiter=limiter,
        data=data,
        headers=headers(config),
        # Rate-limit answers are results; only server errors are retried.
        retry_statuses=config.retry.retry_statuses - {429},
    )
    return parse_submit_response(url, resp)


async def capture_status(
    transport: Transport,
    limiter: RateLimiter,
    config: Config,
    job_id: str,
    previous: Submission | None = None,
) -> Submission:
    resp = await transport.request(
        "GET",
        f"{config.base_url}/save/status/{job_id}",
        limiter=limiter,
        headers=headers(config),
    )
    if resp.status == 404 and _json(resp) is None:
        now = utcnow()
        return Submission(
            url=previous.url if previous else "",
            status=SubmissionStatus.PENDING,
            job_id=job_id,
            submitted_at=previous.submitted_at if previous else now,
            updated_at=now,
            error_code=STATUS_EXPIRED,
            message="the Archive no longer reports this job's status",
        )
    return parse_status_response(job_id, resp, previous)
