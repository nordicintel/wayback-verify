from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from aioresponses import aioresponses
from helpers import CDX, cdx_rows, load

from wayback_verify import (
    ArchiveClient,
    Outcome,
    Selection,
    Submission,
    SubmissionStatus,
    UrlMatch,
    sha1_base32,
)
from wayback_verify.cdx import compare_urls, parse_cdx_json, select_capture
from wayback_verify.models import utcnow

W3 = "https://www.w3.org/TR/PNG/iso_8859-1.txt"
W3_DIGEST = "TADQLG7CSMDKCJCJ25YA6MRBZ73BKNS4"
URL = "https://example.gov/data/report.pdf"
CONTENT = b"%PDF-1.7 official report"
DIGEST = sha1_base32(CONTENT)
ACCEPT = frozenset({UrlMatch.EXACT, UrlMatch.NORMALIZED, UrlMatch.SCHEME_DIFFERS})


def cdx_params(m: aioresponses) -> list[dict[str, str]]:
    return [
        {k: v[0] for k, v in parse_qs(urlsplit(str(url)).query).items()}
        for (method, url) in m.requests
        if "/cdx/" in str(url)
        for _ in m.requests[(method, url)]
    ]


def test_parse_recorded_cdx() -> None:
    rows = load("cdx_w3_iso8859.json")
    captures = parse_cdx_json(W3, str(rows).replace("'", '"'))
    assert len(captures) == 4
    first = captures[0]
    assert first.original == "http://www.w3.org:80/TR/PNG/iso_8859-1.txt"
    assert first.url_match is UrlMatch.SCHEME_DIFFERS  # :80 and http folded in
    assert (first.statuscode, first.mimetype, first.length) == (200, "text/plain", 1953)
    assert first.digest == W3_DIGEST
    assert first.capture_url.startswith("https://web.archive.org/web/20031228192746/")


def test_parse_dash_digest_and_empty() -> None:
    body = '[["timestamp","original","statuscode","mimetype","digest","length"],'
    body += f'["20240101000000","{URL}","-","warc/revisit","-","-"]]'
    [c] = parse_cdx_json(URL, body)
    assert c.digest is None and c.statuscode is None and c.length is None
    assert parse_cdx_json(URL, "[]") == []
    assert parse_cdx_json(URL, "") == []


def test_compare_urls() -> None:
    assert compare_urls(URL, URL) is UrlMatch.EXACT
    assert compare_urls(URL, "HTTPS://Example.gov:443/data/report.pdf") is (
        UrlMatch.NORMALIZED
    )
    assert compare_urls(URL, "http://example.gov/data/report.pdf") is (
        UrlMatch.SCHEME_DIFFERS
    )
    assert compare_urls(URL, "https://www.example.gov/data/report.pdf") is (
        UrlMatch.DIFFERENT
    )
    assert compare_urls(URL, URL + "?a=1") is UrlMatch.DIFFERENT


def test_selection_policies() -> None:
    rows = parse_cdx_json(
        URL,
        str(
            cdx_rows(
                ["20240101000000", URL, "200", "a", DIGEST, "1"],
                ["20240201000000", URL, "404", "a", "X" * 32, "1"],
                ["20240301000000", URL, "200", "a", DIGEST, "1"],
                [
                    "20240301000000",
                    "http://example.gov/data/report.pdf",
                    "200",
                    "a",
                    "Y" * 32,
                    "1",
                ],
                [
                    "20240401000000",
                    "https://www.example.gov/data/report.pdf",
                    "200",
                    "a",
                    "Z" * 32,
                    "1",
                ],
            )
        ).replace("'", '"'),
    )

    def pick(sel: Selection) -> str | None:
        c = select_capture(rows, sel, ACCEPT)
        return c.timestamp if c else None

    mar = datetime(2024, 3, 1, tzinfo=UTC)
    assert pick(Selection.exact("20240201000000")) == "20240201000000"  # explicit
    assert pick(Selection.first_after(datetime(2024, 1, 15, tzinfo=UTC))) == (
        "20240301000000"  # the 404 row is skipped
    )
    assert pick(Selection.last_before(datetime(2024, 2, 20, tzinfo=UTC))) == (
        "20240101000000"
    )
    assert pick(Selection.nearest(datetime(2024, 2, 20, tzinfo=UTC))) == (
        "20240301000000"
    )
    assert pick(Selection.latest()) == "20240301000000"  # www. variant rejected
    exact = select_capture(rows, Selection.exact("20240301000000"), ACCEPT)
    assert exact is not None and exact.url_match is UrlMatch.EXACT
    assert (
        select_capture(rows, Selection.first_after(mar + timedelta(days=1)), ACCEPT)
        is None
    )


async def test_exact_lookup_is_cached_forever(
    client: ArchiveClient, mock: aioresponses
) -> None:
    ts = "20240301000000"
    mock.get(CDX, payload=cdx_rows([ts, URL, "200", "application/pdf", DIGEST, "9"]))
    first = await client.find_capture(URL, timestamp=ts)
    second = await client.find_capture(URL, timestamp=ts)
    assert first.capture == second.capture and first.capture is not None
    assert not first.from_cache and second.from_cache
    [params] = cdx_params(mock)
    assert params["from"] == params["to"] == ts
    assert params["matchType"] == "exact" and params["output"] == "json"
    assert params["fl"] == "timestamp,original,statuscode,mimetype,digest,length"


async def test_missing_exact_timestamp_is_not_negatively_cached(
    client: ArchiveClient, mock: aioresponses
) -> None:
    ts = "20240301000000"
    mock.get(CDX, payload=[])
    mock.get(CDX, payload=cdx_rows([ts, URL, "200", "application/pdf", DIGEST, "9"]))
    assert (await client.find_capture(URL, timestamp=ts)).capture is None
    assert (await client.find_capture(URL, timestamp=ts)).capture is not None


async def test_no_capture_ttl(client: ArchiveClient, mock: aioresponses) -> None:
    mock.get(CDX, payload=[], repeat=True)
    sel = Selection.latest()
    assert (await client.find_capture(URL, selection=sel)).capture is None
    again = await client.find_capture(URL, selection=sel)
    assert again.capture is None and again.from_cache
    assert len(cdx_params(mock)) == 1
    # Age the cached "no capture" answer past its TTL.
    client.cache._conn.execute(
        "UPDATE cdx_queries SET queried_at = ?",
        ((utcnow() - timedelta(hours=2)).isoformat(timespec="microseconds"),),
    )
    assert not (await client.find_capture(URL, selection=sel)).from_cache
    assert len(cdx_params(mock)) == 2


# -- verification outcomes ----------------------------------------------------


def write(tmp_path: Path, data: bytes = CONTENT) -> Path:
    f = tmp_path / "report.pdf"
    f.write_bytes(data)
    return f


async def test_match(client: ArchiveClient, mock: aioresponses, tmp_path: Path) -> None:
    ts = "20240301000000"
    mock.get(CDX, payload=cdx_rows([ts, URL, "200", "application/pdf", DIGEST, "9"]))
    result = await client.verify_file(URL, write(tmp_path), timestamp=ts)
    assert result.outcome is Outcome.MATCH and result.matched
    assert result.snapshot is not None and result.snapshot.size == len(CONTENT)
    assert result.archive_digest == result.local_digest == DIGEST
    assert result.capture_url == f"https://web.archive.org/web/{ts}/{URL}"
    assert result.capture is not None and result.capture.mimetype == "application/pdf"
    [stored] = await client.cache.verifications(url=URL)
    assert stored.snapshot is not None and stored.snapshot.id == result.snapshot.id


async def test_mismatch_explains_timing(
    client: ArchiveClient, mock: aioresponses, tmp_path: Path
) -> None:
    ts = "20240301000000"
    mock.get(CDX, payload=cdx_rows([ts, URL, "200", "application/pdf", "Q" * 32, "9"]))
    result = await client.verify_file(URL, write(tmp_path), timestamp=ts)
    assert result.outcome is Outcome.MISMATCH
    assert result.note and "local mtime" in result.note and "hashed" in result.note
    assert "replaced the file after the Archive's fetch" in result.note


async def test_digest_unavailable(
    client: ArchiveClient, mock: aioresponses, tmp_path: Path
) -> None:
    ts = "20240301000000"
    mock.get(CDX, payload=cdx_rows([ts, URL, "200", "application/pdf", "-", "9"]))
    result = await client.verify_file(URL, write(tmp_path), timestamp=ts)
    assert result.outcome is Outcome.DIGEST_UNAVAILABLE
    assert result.capture is not None and result.archive_digest is None


async def test_no_capture(client: ArchiveClient, mock: aioresponses) -> None:
    mock.get(CDX, payload=[], repeat=True)
    result = await client.verify_digest(URL, f"sha1:{DIGEST}")
    assert result.outcome is Outcome.NO_CAPTURE and result.capture is None


async def test_capture_failed(client: ArchiveClient, mock: aioresponses) -> None:
    mock.get(CDX, payload=[], repeat=True)
    await client.cache.save_submission(
        Submission(
            URL,
            SubmissionStatus.ERROR,
            job_id="j",
            error_code="error:not-found",
            message="Target URL not found",
        )
    )
    result = await client.verify_digest(URL, DIGEST)
    assert result.outcome is Outcome.CAPTURE_FAILED
    assert result.note and "error:not-found" in result.note


async def test_lookup_error(client: ArchiveClient, mock: aioresponses) -> None:
    mock.get(CDX, status=503, repeat=True)
    result = await client.verify_digest(URL, DIGEST)
    assert result.outcome is Outcome.LOOKUP_ERROR
    assert result.note and "503" in result.note
    assert len(cdx_params(mock)) == 3  # bounded retries


async def test_file_error(client: ArchiveClient, tmp_path: Path) -> None:
    result = await client.verify_file(URL, tmp_path / "missing.pdf")
    assert result.outcome is Outcome.FILE_ERROR


async def test_default_selection_is_nearest_to_mtime(
    client: ArchiveClient, mock: aioresponses, tmp_path: Path
) -> None:
    f = write(tmp_path)
    mock.get(CDX, payload=cdx_rows(["20200101000000", URL, "200", "a", "Q" * 32, "1"]))
    mock.get(CDX, payload=cdx_rows(["20240101000000", URL, "200", "a", DIGEST, "1"]))
    result = await client.verify_file(URL, f)
    assert result.outcome is Outcome.MATCH
    assert result.capture is not None and result.capture.timestamp == "20240101000000"
    after, before = cdx_params(mock)
    assert "from" in after and after["limit"] == "50"
    assert "to" in before and before["limit"] == "-50"


async def test_successful_submission_selects_exact_capture(
    client: ArchiveClient, mock: aioresponses
) -> None:
    final = "https://example.gov/data/report-v2.pdf"  # SPN followed a redirect
    sub = Submission(
        URL,
        SubmissionStatus.SUCCESS,
        job_id="j",
        timestamp="20240301000000",
        original_url=final,
    )
    mock.get(CDX, payload=cdx_rows(["20240301000000", final, "200", "a", DIGEST, "1"]))
    result = await client.verify_digest(URL, DIGEST, submission=sub)
    assert result.outcome is Outcome.MATCH
    [params] = cdx_params(mock)
    assert params["url"] == final and params["from"] == "20240301000000"


async def test_canonicalized_variant_is_flagged_not_silent(
    client: ArchiveClient, mock: aioresponses
) -> None:
    ts = "20031228192746"
    mock.get(
        CDX,
        payload=cdx_rows(
            [
                ts,
                "http://www.w3.org:80/TR/PNG/iso_8859-1.txt",
                "200",
                "t",
                W3_DIGEST,
                "1",
            ]
        ),
    )
    result = await client.verify_digest(W3, W3_DIGEST, timestamp=ts)
    assert result.outcome is Outcome.MATCH
    assert result.capture is not None
    assert result.capture.url_match is UrlMatch.SCHEME_DIFFERS
    assert result.note and "differs from the requested URL" in result.note
