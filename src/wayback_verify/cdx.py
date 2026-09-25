"""CDX capture lookup and explicit capture selection."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from urllib.parse import urlsplit, urlunsplit

from .cache import Cache
from .config import Config
from .files import normalize_digest
from .http import RateLimiter, Transport, TransportError
from .models import (
    Capture,
    UrlMatch,
    parse_wayback_timestamp,
    to_wayback_timestamp,
    utcnow,
)

FIELDS = ("timestamp", "original", "statuscode", "mimetype", "digest", "length")
_DEFAULT_PORTS = {"http": 80, "https": 443}


class SelectionPolicy(StrEnum):
    EXACT = "exact"
    FIRST_AFTER = "first_after"
    LAST_BEFORE = "last_before"
    NEAREST = "nearest"
    LATEST = "latest"


@dataclass(frozen=True, slots=True)
class Selection:
    """Which capture of a URL to compare against."""

    policy: SelectionPolicy
    timestamp: str | None = None
    # By default only 2xx (and "-" revisit) rows qualify; an explicit exact
    # timestamp is honored whatever its status.
    require_ok: bool = True

    @classmethod
    def exact(cls, timestamp: str) -> Selection:
        if not (timestamp.isdigit() and len(timestamp) == 14):
            raise ValueError(
                f"exact selection needs a 14-digit timestamp: {timestamp!r}"
            )
        return cls(SelectionPolicy.EXACT, timestamp, require_ok=False)

    @classmethod
    def first_after(cls, when: datetime) -> Selection:
        return cls(SelectionPolicy.FIRST_AFTER, to_wayback_timestamp(when))

    @classmethod
    def last_before(cls, when: datetime) -> Selection:
        return cls(SelectionPolicy.LAST_BEFORE, to_wayback_timestamp(when))

    @classmethod
    def nearest(cls, when: datetime) -> Selection:
        return cls(SelectionPolicy.NEAREST, to_wayback_timestamp(when))

    @classmethod
    def latest(cls) -> Selection:
        return cls(SelectionPolicy.LATEST)

    def describe(self) -> str:
        return f"{self.policy}" + (f" {self.timestamp}" if self.timestamp else "")


def _norm(url: str) -> tuple[str, str, str]:
    """(scheme, netloc, rest) with case-folded scheme/host and no default port."""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    try:
        port = parts.port
    except ValueError:
        port = None
    netloc = host if port in (None, _DEFAULT_PORTS.get(scheme)) else f"{host}:{port}"
    if parts.username or parts.password:
        netloc = f"{parts.username or ''}:{parts.password or ''}@{netloc}"
    rest = urlunsplit(("", "", parts.path or "/", parts.query, ""))
    return scheme, netloc, rest


def compare_urls(requested: str, original: str) -> UrlMatch:
    if requested == original:
        return UrlMatch.EXACT
    r, o = _norm(requested), _norm(original)
    if r == o:
        return UrlMatch.NORMALIZED
    if r[1:] == o[1:] and {r[0], o[0]} <= {"http", "https"}:
        return UrlMatch.SCHEME_DIFFERS
    return UrlMatch.DIFFERENT


def _int(value: str | None) -> int | None:
    try:
        return int(value) if value not in (None, "", "-") else None
    except ValueError:
        return None


def parse_cdx_json(url: str, body: bytes | str) -> list[Capture]:
    """Parse ``output=json`` (a header row followed by value rows)."""
    text = body.decode("utf-8") if isinstance(body, bytes) else body
    rows = json.loads(text) if text.strip() else []
    if not rows:
        return []
    header, *values = rows
    captures = []
    for row in values:
        rec = dict(zip(header, row, strict=False))
        original = rec.get("original") or url
        captures.append(
            Capture(
                original=original,
                timestamp=rec["timestamp"],
                statuscode=_int(rec.get("statuscode")),
                mimetype=rec.get("mimetype") or None,
                digest=normalize_digest(rec.get("digest")),
                length=_int(rec.get("length")),
                url_match=compare_urls(url, original),
            )
        )
    return captures


def _ok(c: Capture) -> bool:
    # statuscode "-" (None) marks revisit records, which point at an earlier
    # identical payload and carry its digest.
    return c.statuscode is None or 200 <= c.statuscode < 300


_MATCH_RANK = {m: i for i, m in enumerate(UrlMatch)}


def select_capture(
    captures: list[Capture],
    selection: Selection,
    accept: frozenset[UrlMatch],
) -> Capture | None:
    """Apply ``selection`` to candidate rows. Rows whose ``original`` only
    loosely matches the requested URL are dropped unless ``accept`` allows it;
    among equal timestamps the closest URL match wins."""
    pool = [c for c in captures if c.url_match in accept]
    if selection.require_ok:
        pool = [c for c in pool if _ok(c)]
    ts = selection.timestamp
    match selection.policy:
        case SelectionPolicy.EXACT:
            pool = [c for c in pool if c.timestamp == ts]
            return min(
                pool, key=lambda c: (_MATCH_RANK[c.url_match], not _ok(c)), default=None
            )
        case SelectionPolicy.FIRST_AFTER:
            pool = [c for c in pool if ts is None or c.timestamp >= ts]
            return min(
                pool,
                key=lambda c: (c.timestamp, _MATCH_RANK[c.url_match]),
                default=None,
            )
        case SelectionPolicy.LAST_BEFORE:
            pool = [c for c in pool if ts is None or c.timestamp <= ts]
            return min(
                pool,
                key=lambda c: (_neg(c.timestamp), _MATCH_RANK[c.url_match]),
                default=None,
            )
        case SelectionPolicy.LATEST:
            return min(
                pool,
                key=lambda c: (_neg(c.timestamp), _MATCH_RANK[c.url_match]),
                default=None,
            )
        case SelectionPolicy.NEAREST:
            assert ts is not None
            target = parse_wayback_timestamp(ts)
            return min(
                pool,
                key=lambda c: (
                    abs((c.captured_at - target).total_seconds()),
                    _MATCH_RANK[c.url_match],
                ),
                default=None,
            )
    raise ValueError(selection.policy)


def _neg(ts: str) -> int:
    return -int(ts)


@dataclass(frozen=True, slots=True)
class Lookup:
    """Result of finding a capture for one URL."""

    capture: Capture | None
    candidates: int
    from_cache: bool
    note: str | None = None


class CdxClient:
    def __init__(
        self,
        transport: Transport,
        limiter: RateLimiter,
        cache: Cache,
        config: Config,
    ) -> None:
        self.transport = transport
        self.limiter = limiter
        self.cache = cache
        self.config = config

    async def query(
        self,
        url: str,
        *,
        from_ts: str | None = None,
        to_ts: str | None = None,
        limit: int | None = None,
    ) -> list[Capture]:
        """One CDX request (``matchType=exact``). Raises ``TransportError``."""
        params = {
            "url": url,
            "matchType": "exact",
            "output": "json",
            "fl": ",".join(FIELDS),
        }
        if from_ts:
            params["from"] = from_ts
        if to_ts:
            params["to"] = to_ts
        if limit:
            params["limit"] = str(limit)
        resp = await self.transport.request(
            "GET",
            f"{self.config.base_url}/cdx/search/cdx",
            limiter=self.limiter,
            params=params,
            headers={"User-Agent": self.config.user_agent},
        )
        if not resp.ok:
            raise TransportError(
                f"CDX query for {url} failed: HTTP {resp.status}", status=resp.status
            )
        try:
            return parse_cdx_json(url, resp.body)
        except (ValueError, KeyError, TypeError) as exc:
            raise TransportError(f"unreadable CDX response for {url}: {exc}") from exc

    async def _cached_query(
        self,
        url: str,
        *,
        from_ts: str | None,
        to_ts: str | None,
        limit: int | None,
        immutable: bool,
    ) -> tuple[list[Capture], bool]:
        """Query through the cache. Exact rows are served forever; range
        results for ``ttl.cdx_query`` and empty results for ``ttl.no_capture``."""
        cached = await self.cache.captures_for(url, from_ts=from_ts, to_ts=to_ts)
        if immutable and cached:
            return cached, True
        key = json.dumps([from_ts, to_ts, limit])
        params_hash = hashlib.sha1(key.encode()).hexdigest()
        last = await self.cache.last_cdx_query(url, params_hash)
        if last is not None and not immutable:
            queried_at, count = last
            age = utcnow() - queried_at
            ttl = self.config.ttl.cdx_query if count else self.config.ttl.no_capture
            if age <= ttl:
                return cached, True
        rows = await self.query(url, from_ts=from_ts, to_ts=to_ts, limit=limit)
        # An exact timestamp that is not indexed yet is not logged, so the
        # "no capture" TTL never hides a capture SPN already reported.
        await self.cache.add_captures(
            url, rows, params_hash=None if immutable and not rows else params_hash
        )
        return rows, False

    async def find(self, url: str, selection: Selection) -> Lookup:
        """Find the capture ``selection`` picks. Raises ``TransportError``."""
        n = self.config.cdx_limit_rows
        ts = selection.timestamp
        accept = self.config.accept_url_match
        policy = selection.policy
        if policy is SelectionPolicy.EXACT:
            rows, hit = await self._cached_query(
                url, from_ts=ts, to_ts=ts, limit=None, immutable=True
            )
        elif policy is SelectionPolicy.FIRST_AFTER:
            rows, hit = await self._cached_query(
                url, from_ts=ts, to_ts=None, limit=n, immutable=False
            )
        elif policy in (SelectionPolicy.LAST_BEFORE, SelectionPolicy.LATEST):
            rows, hit = await self._cached_query(
                url, from_ts=None, to_ts=ts, limit=-n, immutable=False
            )
        else:
            after, hit_a = await self._cached_query(
                url, from_ts=ts, to_ts=None, limit=n, immutable=False
            )
            before, hit_b = await self._cached_query(
                url, from_ts=None, to_ts=ts, limit=-n, immutable=False
            )
            rows, hit = [*before, *after], hit_a and hit_b
        capture = select_capture(rows, selection, accept)
        note = None
        if capture is None and rows:
            rejected = {c.url_match for c in rows} - accept
            note = (
                f"{len(rows)} CDX row(s) found but none qualified for "
                f"{selection.describe()}"
                + (f" (URL variants rejected: {sorted(rejected)})" if rejected else "")
            )
        elif capture is not None and capture.url_match is not UrlMatch.EXACT:
            note = (
                f"archived original {capture.original!r} differs from the requested "
                f"URL ({capture.url_match})"
            )
        return Lookup(capture, len(rows), hit, note)
