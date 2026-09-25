"""Accessibility check of an archived file's raw (``id_``) replay."""

from __future__ import annotations

import re

from .config import Config
from .http import RateLimiter, Response, Transport, TransportError
from .models import AccessCheck, capture_link, utcnow

_TS_IN_URL = re.compile(r"/web/(\d{4,14})(?:[a-z]{2}_)?/")


def timestamp_from_url(url: str) -> str | None:
    m = _TS_IN_URL.search(url)
    return m.group(1) if m else None


def _result(original: str, timestamp: str, resp: Response, method: str) -> AccessCheck:
    final_ts = timestamp_from_url(resp.url)
    # Following a redirect to another capture means this one is not served.
    redirected = bool(resp.history) and final_ts not in (None, timestamp)
    return AccessCheck(
        original=original,
        timestamp=timestamp,
        http_status=resp.status,
        accessible=resp.ok and not redirected,
        redirected=redirected,
        final_timestamp=final_ts,
        final_url=resp.url,
        method=method,
        checked_at=utcnow(),
    )


async def check_access(
    transport: Transport,
    limiter: RateLimiter,
    config: Config,
    original: str,
    timestamp: str,
) -> AccessCheck:
    """HEAD the ``id_`` URL; if that fails, GET ``Range: bytes=0-0`` and close
    the connection without reading the body."""
    url = capture_link(timestamp, original, raw=True)
    if config.base_url != "https://web.archive.org":
        url = url.replace("https://web.archive.org", config.base_url, 1)
    headers = {"User-Agent": config.user_agent}
    head_error = None
    try:
        resp = await transport.request("HEAD", url, limiter=limiter, headers=headers)
        if resp.ok:
            return _result(original, timestamp, resp, "HEAD")
        head_error = f"HEAD returned HTTP {resp.status}"
    except TransportError as exc:
        head_error = str(exc)
    try:
        resp = await transport.request(
            "GET",
            url,
            limiter=limiter,
            headers={**headers, "Range": "bytes=0-0"},
            read="none",
        )
    except TransportError as exc:
        return AccessCheck(
            original=original,
            timestamp=timestamp,
            http_status=exc.status,
            accessible=False,
            method="GET",
            checked_at=utcnow(),
            error=f"{head_error}; {exc}",
        )
    return _result(original, timestamp, resp, "GET")
