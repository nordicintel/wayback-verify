"""Credentials, rate limits, retry policy, cache TTLs and concurrency."""

from __future__ import annotations

import os
from collections.abc import Mapping
from configparser import RawConfigParser
from dataclasses import dataclass, field, replace
from datetime import timedelta
from pathlib import Path
from typing import Any

from ._version import __version__
from .models import UrlMatch


@dataclass(frozen=True, slots=True)
class Credentials:
    """Internet Archive S3-style keys (https://archive.org/account/s3.php)."""

    access: str
    secret: str = field(repr=False)

    @property
    def authorization(self) -> str:
        return f"LOW {self.access}:{self.secret}"


def ia_config_candidates() -> list[Path]:
    """Config file search order used by the ``internetarchive`` package (``ia``)."""
    candidates: list[Path] = []
    if env := os.environ.get("IA_CONFIG_FILE"):
        candidates.append(Path(env))
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if not xdg or not os.path.isabs(xdg):
        xdg = os.path.join(os.path.expanduser("~"), ".config")
    candidates.append(Path(xdg, "internetarchive", "ia.ini"))
    candidates.append(Path(os.path.expanduser("~"), ".config", "ia.ini"))
    candidates.append(Path(os.path.expanduser("~"), ".ia"))
    return candidates


def load_ia_credentials(
    config_file: str | os.PathLike[str] | None = None,
) -> Credentials | None:
    """Load keys the way ``ia configure`` stores them, without depending on the
    AGPL-licensed ``internetarchive`` package.

    Order: the ``[s3] access/secret`` of the first existing config file (or
    ``config_file``), overridden by ``IA_ACCESS_KEY_ID``/``IA_SECRET_ACCESS_KEY``.
    """
    access = secret = None
    paths = [Path(config_file)] if config_file else ia_config_candidates()
    for path in paths:
        if path.is_file():
            parser = RawConfigParser()
            parser.read(path, encoding="utf-8")
            if parser.has_section("s3"):
                access = parser.get("s3", "access", fallback=None) or None
                secret = parser.get("s3", "secret", fallback=None) or None
            break
    env_access = os.environ.get("IA_ACCESS_KEY_ID")
    env_secret = os.environ.get("IA_SECRET_ACCESS_KEY")
    if bool(env_access) != bool(env_secret):
        raise ValueError(
            "IA_ACCESS_KEY_ID and IA_SECRET_ACCESS_KEY must be set together"
        )
    if env_access and env_secret:
        access, secret = env_access, env_secret
    if access and secret:
        return Credentials(access, secret)
    return None


@dataclass(frozen=True, slots=True)
class RateLimit:
    """At most ``concurrency`` requests in flight, and request starts at least
    ``min_interval`` seconds apart."""

    concurrency: int
    min_interval: float = 0.0


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Bounded exponential backoff for transient failures."""

    max_attempts: int = 4
    base_delay: float = 2.0
    max_delay: float = 120.0
    retry_statuses: frozenset[int] = frozenset({429, 500, 502, 503, 504})


@dataclass(frozen=True, slots=True)
class CacheTTLs:
    # "No capture" CDX answers; captures appear in the index with a delay.
    no_capture: timedelta = timedelta(hours=1)
    # Positive range queries (first-after, nearest, latest). Exact
    # (URL, timestamp) rows are immutable and cached forever.
    cdx_query: timedelta = timedelta(hours=6)
    access_check: timedelta = timedelta(days=1)
    # submit()/submit_many() reuse a successful capture this recent.
    recent_capture: timedelta = timedelta(days=1)


# Save Page Now 2 limits from the public API docs (checked 2026-09-25):
# 7 captures/minute authenticated, 3/minute anonymous.
AUTH_SPN_LIMIT = RateLimit(concurrency=2, min_interval=60 / 7 + 0.5)
ANON_SPN_LIMIT = RateLimit(concurrency=1, min_interval=60 / 3 + 1.0)
# CDX allows about 30 requests/minute and /web/ replay about 600/minute; the
# Archive has asked clients to stay at 80% of that (as documented by EDGI's
# ``wayback`` package, checked 2026-09-25).
CDX_LIMIT = RateLimit(concurrency=2, min_interval=60 / (0.8 * 30))
ACCESS_LIMIT = RateLimit(concurrency=4, min_interval=60 / (0.8 * 600))


@dataclass(frozen=True, slots=True)
class Config:
    credentials: Credentials | None = None
    user_agent: str = (
        f"wayback-verify/{__version__} (+https://github.com/nordicintel/wayback-verify)"
    )
    base_url: str = "https://web.archive.org"
    # None picks AUTH_SPN_LIMIT or ANON_SPN_LIMIT from ``credentials``.
    spn_limit: RateLimit | None = None
    status_limit: RateLimit = RateLimit(concurrency=2, min_interval=1.0)
    cdx_limit: RateLimit = CDX_LIMIT
    access_limit: RateLimit = ACCESS_LIMIT
    retry: RetryPolicy = RetryPolicy()
    # Pause for bulk submissions after a rate-limit answer without Retry-After.
    spn_rate_limit_backoff: float = 60.0
    # How often one item is resubmitted after a rate-limit pause in bulk mode.
    spn_rate_limit_retries: int = 2
    request_timeout: float = 120.0
    poll_interval: float = 10.0
    # SPN keeps job status for roughly an hour.
    wait_timeout: float = 45 * 60.0
    ttl: CacheTTLs = CacheTTLs()
    # Extra SPN2 POST options, e.g. {"force_get": "1", "skip_first_archive": "1"}.
    spn_options: Mapping[str, str] = field(default_factory=dict)
    # Candidate rows fetched per CDX range query.
    cdx_limit_rows: int = 50
    accept_url_match: frozenset[UrlMatch] = frozenset(
        {UrlMatch.EXACT, UrlMatch.NORMALIZED, UrlMatch.SCHEME_DIFFERS}
    )
    hash_workers: int = 4
    bulk_concurrency: int = 8
    cache_batch_size: int = 100

    @property
    def effective_spn_limit(self) -> RateLimit:
        if self.spn_limit is not None:
            return self.spn_limit
        return AUTH_SPN_LIMIT if self.credentials else ANON_SPN_LIMIT

    @classmethod
    def from_environment(
        cls, config_file: str | os.PathLike[str] | None = None, **overrides: Any
    ) -> Config:
        """A config whose credentials come from ``ia.ini`` / ``IA_*`` variables."""
        overrides.setdefault("credentials", load_ia_credentials(config_file))
        return cls(**overrides)

    def replace(self, **changes: Any) -> Config:
        return replace(self, **changes)
