"""Result dataclasses and outcome enums shared across the package."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

WAYBACK = "https://web.archive.org"


def utcnow() -> datetime:
    return datetime.now(UTC)


def to_wayback_timestamp(dt: datetime) -> str:
    """Format a datetime as a 14-digit Wayback timestamp (UTC)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y%m%d%H%M%S")


def parse_wayback_timestamp(ts: str) -> datetime:
    """Parse a 4- to 14-digit Wayback timestamp; missing parts default to the start."""
    if not ts.isdigit() or not 4 <= len(ts) <= 14:
        raise ValueError(f"not a Wayback timestamp: {ts!r}")
    padded = ts + "0101000000"[len(ts) - 4 :] if len(ts) < 14 else ts
    return datetime.strptime(padded, "%Y%m%d%H%M%S").replace(tzinfo=UTC)


def capture_link(timestamp: str, original: str, *, raw: bool = False) -> str:
    """Dated capture link; ``raw=True`` gives the unmodified ``id_`` replay URL."""
    return f"{WAYBACK}/web/{timestamp}{'id_' if raw else ''}/{original}"


def _ns_to_dt(ns: int | None) -> datetime | None:
    return None if ns is None else datetime.fromtimestamp(ns / 1e9, UTC)


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    return value


class Outcome(StrEnum):
    """Verdict of comparing a local file with an archived capture."""

    MATCH = "match"
    MISMATCH = "mismatch"
    DIGEST_UNAVAILABLE = "digest_unavailable"
    NO_CAPTURE = "no_capture"
    CAPTURE_FAILED = "capture_failed"
    LOOKUP_ERROR = "lookup_error"
    FILE_ERROR = "file_error"


class SubmissionStatus(StrEnum):
    PENDING = "pending"
    SUCCESS = "success"
    ERROR = "error"
    # The Archive never accepted the request (HTTP error, rate limit, network).
    NOT_SUBMITTED = "not_submitted"


class UrlMatch(StrEnum):
    """How a CDX ``original`` relates to the requested URL."""

    EXACT = "exact"
    # Equal after lower-casing scheme/host and dropping default ports.
    NORMALIZED = "normalized"
    # As NORMALIZED, but http vs https.
    SCHEME_DIFFERS = "scheme_differs"
    # Anything else the CDX canonicalizer folded together (www., query order, ...).
    DIFFERENT = "different"


@dataclass(frozen=True, slots=True)
class FileState:
    """The ``stat`` properties a hash is tied to."""

    path: str
    size: int
    mtime_ns: int
    ctime_ns: int
    birthtime_ns: int | None
    inode: int
    device: int
    platform: str

    def same_state(self, other: FileState) -> bool:
        return (
            self.path == other.path
            and self.size == other.size
            and self.mtime_ns == other.mtime_ns
            and self.ctime_ns == other.ctime_ns
            and self.inode == other.inode
            and self.device == other.device
        )


@dataclass(frozen=True, slots=True)
class FileSnapshot:
    """A content hash together with the file state it was computed from."""

    path: str
    size: int
    mtime_ns: int
    ctime_ns: int
    birthtime_ns: int | None
    inode: int
    device: int
    platform: str
    sha1_b32: str
    sha1_hex: str
    hashed_at: datetime
    # False when the file kept changing while it was hashed; never reused.
    stable: bool = True
    id: int | None = None
    # True when the hash came from the cache instead of reading the file.
    reused: bool = False

    @property
    def state(self) -> FileState:
        return FileState(
            self.path,
            self.size,
            self.mtime_ns,
            self.ctime_ns,
            self.birthtime_ns,
            self.inode,
            self.device,
            self.platform,
        )

    @property
    def mtime(self) -> datetime:
        return datetime.fromtimestamp(self.mtime_ns / 1e9, UTC)

    @property
    def ctime(self) -> datetime:
        return datetime.fromtimestamp(self.ctime_ns / 1e9, UTC)

    @property
    def birthtime(self) -> datetime | None:
        return _ns_to_dt(self.birthtime_ns)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["mtime"] = self.mtime
        d["ctime"] = self.ctime
        d["birthtime"] = self.birthtime
        return _jsonable(d)  # type: ignore[no-any-return]


@dataclass(frozen=True, slots=True)
class Capture:
    """One CDX row. ``length`` is the archived (compressed WARC) record length,
    not the file size, and is never used as a size check."""

    original: str
    timestamp: str
    statuscode: int | None
    mimetype: str | None
    # Normalized Base32 SHA-1, or None when the CDX digest is missing or "-".
    digest: str | None
    length: int | None
    url_match: UrlMatch = UrlMatch.EXACT

    @property
    def captured_at(self) -> datetime:
        return parse_wayback_timestamp(self.timestamp)

    @property
    def capture_url(self) -> str:
        return capture_link(self.timestamp, self.original)

    @property
    def raw_url(self) -> str:
        return capture_link(self.timestamp, self.original, raw=True)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["capture_url"] = self.capture_url
        return d


@dataclass(frozen=True, slots=True)
class Submission:
    """A Save Page Now job and its latest known state."""

    url: str
    status: SubmissionStatus
    job_id: str | None = None
    submitted_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    timestamp: str | None = None
    original_url: str | None = None
    error_code: str | None = None
    message: str | None = None
    # Seconds the Archive asked us to wait (HTTP 429 / Retry-After).
    retry_after: float | None = None
    from_cache: bool = False
    raw: dict[str, Any] | None = field(default=None, repr=False, compare=False)

    @property
    def is_final(self) -> bool:
        return self.status in (SubmissionStatus.SUCCESS, SubmissionStatus.ERROR)

    @property
    def capture_url(self) -> str | None:
        if self.status is not SubmissionStatus.SUCCESS or not self.timestamp:
            return None
        return capture_link(self.timestamp, self.original_url or self.url)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("raw")
        d["capture_url"] = self.capture_url
        return _jsonable(d)  # type: ignore[no-any-return]


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """Digest comparison of one local file (or digest) against one capture."""

    url: str
    outcome: Outcome
    local_digest: str | None
    snapshot: FileSnapshot | None = None
    capture: Capture | None = None
    checked_at: datetime = field(default_factory=utcnow)
    note: str | None = None

    @property
    def archive_digest(self) -> str | None:
        return self.capture.digest if self.capture else None

    @property
    def capture_url(self) -> str | None:
        return self.capture.capture_url if self.capture else None

    @property
    def matched(self) -> bool:
        return self.outcome is Outcome.MATCH

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(  # type: ignore[no-any-return]
            {
                "url": self.url,
                "outcome": str(self.outcome),
                "local_digest": self.local_digest,
                "archive_digest": self.archive_digest,
                "capture": self.capture.to_dict() if self.capture else None,
                "snapshot": self.snapshot.to_dict() if self.snapshot else None,
                "checked_at": self.checked_at,
                "note": self.note,
            }
        )


@dataclass(frozen=True, slots=True)
class AccessCheck:
    """Whether the raw (``id_``) replay of a capture can be fetched."""

    original: str
    timestamp: str
    http_status: int | None
    accessible: bool
    redirected: bool = False
    final_timestamp: str | None = None
    final_url: str | None = None
    method: str = "HEAD"
    checked_at: datetime = field(default_factory=utcnow)
    error: str | None = None
    from_cache: bool = False

    @property
    def raw_url(self) -> str:
        return capture_link(self.timestamp, self.original, raw=True)

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))  # type: ignore[no-any-return]
