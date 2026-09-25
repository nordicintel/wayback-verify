"""Digest comparison and verification outcomes."""

from __future__ import annotations

from datetime import datetime

from .cdx import Lookup, Selection
from .files import normalize_digest
from .models import (
    Capture,
    FileSnapshot,
    Outcome,
    Submission,
    SubmissionStatus,
    VerificationResult,
    utcnow,
)


def default_selection(snapshot: FileSnapshot | None) -> Selection:
    """The capture closest to the local file state: nearest to the file's
    mtime, or to when it was hashed; the latest capture for a bare digest."""
    if snapshot is None:
        return Selection.latest()
    when = snapshot.mtime if snapshot.mtime_ns > 0 else snapshot.hashed_at
    return Selection.nearest(when)


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%SZ")


def mismatch_note(snapshot: FileSnapshot | None, capture: Capture) -> str:
    """Relate the local file times to the capture time on a mismatch."""
    captured = capture.captured_at
    parts = [f"archive captured {_fmt(captured)}"]
    if snapshot is not None:
        parts.append(f"local mtime {_fmt(snapshot.mtime)}")
        parts.append(f"hashed {_fmt(snapshot.hashed_at)}")
        if snapshot.mtime < captured:
            parts.append(
                "the local copy is older than the capture: the source may have "
                "replaced the file between the local download and the Archive's "
                "fetch"
            )
        else:
            parts.append(
                "the local copy is newer than the capture: the source may have "
                "replaced the file after the Archive's fetch"
            )
    if capture.statuscode is not None and not 200 <= capture.statuscode < 300:
        parts.append(f"the capture recorded HTTP {capture.statuscode}")
    return "; ".join(parts)


def compare(
    url: str,
    local_digest: str,
    lookup: Lookup,
    *,
    snapshot: FileSnapshot | None = None,
    submission: Submission | None = None,
) -> VerificationResult:
    """Turn a CDX lookup into a verdict for ``local_digest``."""
    local = normalize_digest(local_digest)
    capture = lookup.capture
    notes = [lookup.note] if lookup.note else []
    if capture is None:
        failed = submission is not None and submission.status in (
            SubmissionStatus.ERROR,
            SubmissionStatus.NOT_SUBMITTED,
        )
        if failed:
            assert submission is not None
            notes.insert(
                0,
                f"capture failed: {submission.error_code or 'unknown error'}"
                + (f" ({submission.message})" if submission.message else ""),
            )
            outcome = Outcome.CAPTURE_FAILED
        else:
            if submission is not None and submission.status is SubmissionStatus.PENDING:
                notes.insert(0, "capture still pending")
            elif submission is not None and submission.timestamp:
                notes.insert(
                    0,
                    f"Save Page Now reported capture {submission.timestamp}, "
                    "but the CDX index does not list it yet",
                )
            outcome = Outcome.NO_CAPTURE
    elif capture.digest is None:
        outcome = Outcome.DIGEST_UNAVAILABLE
        notes.append("the CDX row has no digest; verification is unavailable")
    elif capture.digest == local:
        outcome = Outcome.MATCH
    else:
        outcome = Outcome.MISMATCH
        notes.append(mismatch_note(snapshot, capture))
    return VerificationResult(
        url=url,
        outcome=outcome,
        local_digest=local,
        snapshot=snapshot,
        capture=capture,
        checked_at=utcnow(),
        note="; ".join(notes) or None,
    )


def error_result(
    url: str,
    outcome: Outcome,
    message: str,
    *,
    local_digest: str | None = None,
    snapshot: FileSnapshot | None = None,
) -> VerificationResult:
    return VerificationResult(
        url=url,
        outcome=outcome,
        local_digest=normalize_digest(local_digest) if local_digest else None,
        snapshot=snapshot,
        note=message,
    )
