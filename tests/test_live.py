"""Opt-in tests against the real Internet Archive: ``pytest -m live``.

``test_live_verify_existing_capture`` only reads (CDX + one archived file).
``test_live_submit_verify_rerun`` creates real Save Page Now captures; it uses
``ia.ini`` / ``IA_*`` credentials when present and runs anonymously otherwise.
Override its URLs with ``WAYBACK_VERIFY_LIVE_URLS`` (space-separated).
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import aiohttp
import pytest

from wayback_verify import ArchiveClient, Config, Outcome, SubmissionStatus

pytestmark = pytest.mark.live

# Small, long-stable plain-text files.
DEFAULT_URLS = [
    "https://www.rfc-editor.org/rfc/rfc1149.txt",
    "https://www.rfc-editor.org/rfc/rfc2549.txt",
]
W3 = "https://www.w3.org/TR/PNG/iso_8859-1.txt"
W3_TS = "20160306224107"


async def download(url: str, dest: Path) -> Path:
    # identity: the Archive's digest covers the body as transferred, so a
    # transparently decompressed download could differ from it.
    headers = {"Accept-Encoding": "identity", "User-Agent": "wayback-verify-live-test"}
    async with (
        aiohttp.ClientSession(auto_decompress=False) as session,
        session.get(url, headers=headers) as resp,
    ):
        resp.raise_for_status()
        dest.write_bytes(await resp.read())
    return dest


async def test_live_verify_existing_capture(tmp_path: Path) -> None:
    raw = f"https://web.archive.org/web/{W3_TS}id_/http://www.w3.org/TR/PNG/iso_8859-1.txt"
    local = await download(raw, tmp_path / "iso_8859-1.txt")
    async with ArchiveClient(Config(), tmp_path / "cache.sqlite") as client:
        result = await client.verify_file(W3, local, timestamp=W3_TS)
        assert result.outcome is Outcome.MATCH, result.note
        assert result.capture is not None
        access = await client.check_access(result.capture)
        assert access.accessible, access


async def test_live_submit_verify_rerun(tmp_path: Path) -> None:
    urls = os.environ.get("WAYBACK_VERIFY_LIVE_URLS", "").split() or DEFAULT_URLS
    cache = tmp_path / "cache.sqlite"
    files = {url: tmp_path / f"{i}.txt" for i, url in enumerate(urls)}

    async with ArchiveClient(Config.from_environment(), cache) as client:
        subs = await client.submit_many(urls, progress=True)
        done = await client.wait_for_captures(subs, progress=True)
        failed = [d for d in done if d.status is not SubmissionStatus.SUCCESS]
        assert not failed, failed
        for url, path in files.items():
            await download(url, path)
        # A fresh capture can take a while to appear in the CDX index.
        for _ in range(20):
            results = await client.verify_many(
                [(sub, files[sub.url]) for sub in done], progress=True
            )
            if all(r.outcome is not Outcome.NO_CAPTURE for r in results):
                break
            await asyncio.sleep(30)
        assert all(r.outcome is Outcome.MATCH for r in results), results
        accesses = await client.check_access_many(results, progress=True)
        assert all(a is not None and a.accessible for a in accesses), accesses

    async with ArchiveClient(Config.from_environment(), cache) as client:
        again = await client.submit_many(urls)
        assert all(s.from_cache for s in again)  # nothing resubmitted
        snaps = await client.hash_files(list(files.values()))
        assert all(getattr(s, "reused", False) for s in snaps)  # nothing rehashed
        results = await client.verify_many([(s, files[s.url]) for s in again])
        assert all(r.outcome is Outcome.MATCH for r in results)
        assert all(r.snapshot is not None and r.snapshot.reused for r in results)
