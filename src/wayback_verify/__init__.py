"""Archive files with the Wayback Machine and verify them against local copies."""

from ._version import __version__
from .bulk import HashFailure, VerifyItem
from .cache import Cache
from .cdx import Selection, SelectionPolicy
from .client import ArchiveClient
from .config import (
    CacheTTLs,
    Config,
    Credentials,
    RateLimit,
    RetryPolicy,
    load_ia_credentials,
)
from .files import hex_sha1_to_base32, normalize_digest, sha1_base32
from .http import TransportError
from .models import (
    AccessCheck,
    Capture,
    FileSnapshot,
    Outcome,
    Submission,
    SubmissionStatus,
    UrlMatch,
    VerificationResult,
)
from .report import FailureReport, ReportEntry, build_report

__all__ = [
    "AccessCheck",
    "ArchiveClient",
    "Cache",
    "CacheTTLs",
    "Capture",
    "Config",
    "Credentials",
    "FailureReport",
    "FileSnapshot",
    "HashFailure",
    "Outcome",
    "RateLimit",
    "ReportEntry",
    "RetryPolicy",
    "Selection",
    "SelectionPolicy",
    "Submission",
    "SubmissionStatus",
    "TransportError",
    "UrlMatch",
    "VerificationResult",
    "VerifyItem",
    "__version__",
    "build_report",
    "hex_sha1_to_base32",
    "load_ia_credentials",
    "normalize_digest",
    "sha1_base32",
]
