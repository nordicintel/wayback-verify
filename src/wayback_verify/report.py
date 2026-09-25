"""Failure reporting over results or cached history."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from .models import (
    AccessCheck,
    Outcome,
    Submission,
    SubmissionStatus,
    VerificationResult,
    _jsonable,
    utcnow,
)


@dataclass(frozen=True, slots=True)
class ReportEntry:
    url: str
    kind: str
    status: str
    capture_timestamp: str | None = None
    capture_url: str | None = None
    error_code: str | None = None
    local_digest: str | None = None
    archive_digest: str | None = None
    note: str | None = None
    # Path, size, mtime, ctime/birthtime, hashed_at, ... of the compared file.
    file: dict[str, Any] | None = None
    at: datetime | None = None


@dataclass(frozen=True, slots=True)
class FailureReport:
    generated_at: datetime
    since: datetime | None
    failed_captures: list[ReportEntry] = field(default_factory=list)
    mismatches: list[ReportEntry] = field(default_factory=list)
    digest_unavailable: list[ReportEntry] = field(default_factory=list)
    inaccessible: list[ReportEntry] = field(default_factory=list)
    # Verifications that ended without a verdict (no capture, lookup errors).
    unverified: list[ReportEntry] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (
            self.failed_captures
            or self.mismatches
            or self.digest_unavailable
            or self.inaccessible
            or self.unverified
        )

    @property
    def counts(self) -> dict[str, int]:
        return {
            "failed_captures": len(self.failed_captures),
            "mismatches": len(self.mismatches),
            "digest_unavailable": len(self.digest_unavailable),
            "inaccessible": len(self.inaccessible),
            "unverified": len(self.unverified),
        }

    def to_dict(self) -> dict[str, Any]:
        d = _jsonable(asdict(self))
        d["counts"] = self.counts
        return d  # type: ignore[no-any-return]

    def to_json(self, **kwargs: Any) -> str:
        kwargs.setdefault("indent", 2)
        return json.dumps(self.to_dict(), **kwargs)


def _from_verification(r: VerificationResult, kind: str) -> ReportEntry:
    return ReportEntry(
        url=r.url,
        kind=kind,
        status=str(r.outcome),
        capture_timestamp=r.capture.timestamp if r.capture else None,
        capture_url=r.capture_url,
        local_digest=r.local_digest,
        archive_digest=r.archive_digest,
        note=r.note,
        file=r.snapshot.to_dict() if r.snapshot else None,
        at=r.checked_at,
    )


def build_report(
    *,
    verifications: Iterable[VerificationResult] = (),
    submissions: Iterable[Submission] = (),
    access_checks: Iterable[AccessCheck | None] = (),
    since: datetime | None = None,
) -> FailureReport:
    report = FailureReport(generated_at=utcnow(), since=since)
    for s in submissions:
        if since and s.updated_at < since:
            continue
        if s.status in (SubmissionStatus.ERROR, SubmissionStatus.NOT_SUBMITTED):
            report.failed_captures.append(
                ReportEntry(
                    url=s.url,
                    kind="capture",
                    status=str(s.status),
                    error_code=s.error_code,
                    note=s.message,
                    at=s.updated_at,
                )
            )
    for r in verifications:
        if since and r.checked_at < since:
            continue
        match r.outcome:
            case Outcome.MISMATCH:
                report.mismatches.append(_from_verification(r, "mismatch"))
            case Outcome.DIGEST_UNAVAILABLE:
                report.digest_unavailable.append(
                    _from_verification(r, "digest_unavailable")
                )
            case Outcome.CAPTURE_FAILED:
                report.failed_captures.append(_from_verification(r, "capture"))
            case Outcome.MATCH:
                pass
            case _:
                report.unverified.append(_from_verification(r, "unverified"))
    for a in access_checks:
        if a is None or (since and a.checked_at < since) or a.accessible:
            continue
        report.inaccessible.append(
            ReportEntry(
                url=a.original,
                kind="access",
                status=str(a.http_status) if a.http_status else "error",
                capture_timestamp=a.timestamp,
                capture_url=a.raw_url,
                note=a.error
                or (
                    f"redirected to capture {a.final_timestamp}"
                    if a.redirected
                    else None
                ),
                at=a.checked_at,
            )
        )
    # A failed submission and the CAPTURE_FAILED verdict it caused are one
    # failure; keep the later (richer) entry per URL.
    latest = {e.url: e for e in report.failed_captures}
    report.failed_captures[:] = list(latest.values())
    return report
