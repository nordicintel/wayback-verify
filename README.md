# wayback-verify

Async archiving of official source files through the Internet Archive's Wayback
Machine, verification that the archived copies match files you already
downloaded, and a local sqlite3 history and result cache.

- Save Page Now 2 (SPN2) submission and job status, one URL or thousands.
- CDX lookups with explicit capture selection and digest comparison.
- File paths in, hashing handled for you: every hash is stored with the file
  state (size, mtime, ctime/birthtime, inode, device) it was computed from.
- Bulk operations with tqdm progress bars, deduplication, per-item error
  isolation and resumable polling.
- Failure reports from results or from the cache history.

Scope is archiving and verification only; storing files, recording
observations and scheduling belong to the calling application.

## Install

```sh
uv add wayback-verify   # or: pip install wayback-verify
```

Requires Python 3.13+, `aiohttp` and `tqdm`.

## Credentials

SPN2 authenticates with Internet Archive S3-style keys
(<https://archive.org/account/s3.php>). `Config.from_environment()` reads them
the same way the `ia` command line tool stores them (`ia configure`):

1. the file in `IA_CONFIG_FILE`, else `$XDG_CONFIG_HOME/internetarchive/ia.ini`
   (default `~/.config/internetarchive/ia.ini`), `~/.config/ia.ini`, `~/.ia`,
   reading `[s3] access` and `secret`;
2. overridden by `IA_ACCESS_KEY_ID` and `IA_SECRET_ACCESS_KEY` (both or neither).

`ArchiveClient()` without a config uses `Config.from_environment()`. Pass
`Config(credentials=Credentials(access, secret))` to set keys explicitly, or
`Config()` to run anonymously. CDX lookups and access checks never send keys.

## Usage

```python
import asyncio
from wayback_verify import ArchiveClient, Config


async def main() -> None:
    async with ArchiveClient(
        Config.from_environment(), cache_path="wayback.sqlite"
    ) as client:
        url = "https://example.gov/data/report.pdf"

        # single items
        job = await client.submit(url)  # Submission (pending)
        status = await client.capture_status(job)  # pending / success / error
        [sub] = await client.wait_for_captures([job])  # poll until final
        snap = await client.hash_file("downloads/report.pdf")  # FileSnapshot
        result = await client.verify_file(
            url, "downloads/report.pdf", timestamp=sub.timestamp
        )
        print(result.outcome, result.capture_url, result.note)
        if result.capture:
            access = await client.check_access(result.capture)


asyncio.run(main())
```

`cache_path=None` (the default) keeps the cache in memory: hashes, captures and
results are reused within the run, but nothing survives it.

### Bulk

```python
async with ArchiveClient(cache_path="wayback.sqlite") as client:
    # URLs from an iterable, an async iterable, or a file with one per line
    subs = await client.submit_many("urls.txt", progress=True)
    done = await client.wait_for_captures(subs, progress=True)
    results = await client.verify_many(
        [(sub, f"downloads/{sub.url.rsplit('/', 1)[-1]}") for sub in done],
        progress=True,
    )
    accesses = await client.check_access_many(results, progress=True)
    snaps = await client.hash_files("downloads/**/*.pdf", progress=True, unit="bytes")
    report = await client.report()
    print(report.to_json())
```

- `submit_many` submits each distinct URL once. URLs with a successful capture
  or a still-pending job newer than `config.ttl.recent_capture` (1 day) are
  returned from the cache instead of being resubmitted.
- `wait_for_captures()` with no arguments resumes polling every pending job
  stored in the cache, for example in a later run.
- `verify_many` takes a mapping `{url: path}`, pairs `(url | Submission, path |
  digest | FileSnapshot)` or `VerifyItem`s. A successful `Submission` selects
  its exact capture (and the final URL SPN reported after redirects). Digests
  may be Base32, hex, or `sha1:`-prefixed.
- `hash_files` takes paths, directories (recursive) and globs.
- `check_access_many` takes captures, verification results or
  `(original, timestamp)` pairs; `None` entries stay `None`.
- One failing item never aborts a batch; every item gets its own result.
- `progress=True` shows a `tqdm.asyncio.tqdm` bar whose postfix counts matched,
  mismatched, failed and pending items. Pass your own `tqdm` instance, or any
  callable `on_progress(item, result)`, for custom reporting.

`report()` is a coroutine (it reads the cache in a worker thread). It builds a
`FailureReport` from the results you pass, or from the latest history entries
in the cache, grouped into failed captures, mismatches, unavailable digests,
inaccessible captures and unverified items. Each entry carries the compared
file's snapshot properties. `to_dict()` / `to_json()` serialize it.

### Capture selection

`verify_file`/`verify_digest` compare against one specific capture:

| Given | Selected capture |
| --- | --- |
| `timestamp="20240301120000"` | exactly that capture |
| `submission=` a successful `Submission` | its exact capture |
| `selection=Selection.first_after(dt)` / `last_before(dt)` / `nearest(dt)` / `latest()` | per policy |
| nothing, with a file | the capture nearest to the file's mtime |
| nothing, with a bare digest | the latest capture |

Policies other than an exact timestamp skip captures whose recorded HTTP status
is not 2xx. The CDX server folds URL variants together (`http`/`https`, `:80`,
`www.`, query order), so each capture's `original` is compared with the
requested URL (`Capture.url_match`): `exact`, `normalized` (case, default
port), `scheme_differs` and `different`. `different` rows are rejected by
default (`Config.accept_url_match`); accepted non-exact matches are named in
the result's `note`, never accepted silently.

## Outcomes

| Outcome | Meaning |
| --- | --- |
| `MATCH` | The capture's SHA-1 equals the local file's. Establishes content correspondence only; accessibility is a separate check. |
| `MISMATCH` | Digests differ. The note relates the file's mtime and hash time to the capture time. |
| `DIGEST_UNAVAILABLE` | The capture exists but its CDX digest is missing or `-`; verification is unavailable. |
| `NO_CAPTURE` | No qualifying capture. The note says if a capture is still pending or SPN reported one the index does not list yet. |
| `CAPTURE_FAILED` | No capture, and the latest submission for the URL failed (error code in the note). |
| `LOOKUP_ERROR` | Network or HTTP failure talking to the Archive. Not a verdict; try again. |
| `FILE_ERROR` | The local file could not be read. Not a verdict. (Addition to the original plan.) |

Every result carries the `FileSnapshot` it used, the archive digest, the
capture's timestamp, status code and MIME type, the dated capture link
`https://web.archive.org/web/{timestamp}/{original}` and `checked_at`.

`AccessCheck` sends `HEAD` to the raw replay
`https://web.archive.org/web/{timestamp}id_/{original}` and falls back to a
`GET` with `Range: bytes=0-0` whose connection is closed unread. It records
the HTTP status, whether the Archive redirected to a different capture, and the
check time. A redirect to another capture counts as not accessible.

### Known causes of mismatches

- The source replaced the file between your download and the Archive's fetch
  (or after it). Compare `snapshot.mtime` / `hashed_at` with the capture time.
- `Content-Encoding`: the CDX digest covers the payload as the server sent it
  (still gzip-compressed if it was served that way). A download that was
  transparently decompressed differs. Download with
  `Accept-Encoding: identity`, or without automatic decompression.
- A capture of an error page, login wall, CAPTCHA or redirect stub rather than
  the file. Check `capture.statuscode` / `mimetype`.
- Dynamic content (timestamps, session tokens) in HTML or generated files.
- A different URL variant captured by the CDX canonicalizer (see `url_match`).
- Local modifications or line-ending conversion (e.g. git `autocrlf`).

`Capture.length` is the archived (compressed WARC) record length, not the file
size, and is never used as a size check.

## File snapshots

A hash is never tied only to a path. `hash_file(path)` stats the file and
reuses a cached hash only when `path`, `size`, `mtime_ns`, `ctime_ns`, `inode`
and `device` all match a stored, stable snapshot; otherwise it rehashes (1 MiB
chunks in a worker thread) and appends a new snapshot row. Old snapshots are
kept, so `file_history(path)` lists every content state seen. `force=True`
always rehashes. The file is stat'ed before and after hashing; if it changed it
is hashed once more, and if it changed again the snapshot is stored with
`stable=False` and never reused.

Timestamp semantics differ per platform, so `platform` (`sys.platform`) is
stored with every snapshot and `ctime_ns` is kept raw:

| Platform | `ctime_ns` | `birthtime_ns` |
| --- | --- | --- |
| Linux | last metadata (inode) change | `None` (`statx` birth time is not exposed by `os.stat`) |
| macOS / BSD | last metadata change | creation time (`st_birthtime`) |
| Windows, Python 3.12+ | creation time (deprecated meaning; may become metadata change time in a future Python) | creation time (`st_birthtime`) |

`inode`/`device` detect a file replaced at the same path (for example an
atomic rename), which also changes `ctime` on Unix and creation time on Windows.

## Cache

stdlib `sqlite3` in WAL mode; the schema version lives in `PRAGMA user_version`
and migrations run on open. Every database call runs in `asyncio.to_thread`;
bulk runs write verification and access results in one transaction per chunk
(`Config.cache_batch_size`).

| Table | Contents |
| --- | --- |
| `file_snapshots` | append-only snapshots and hashes |
| `submissions` | SPN jobs with status, error code, capture timestamp and raw JSON |
| `captures`, `capture_urls` | immutable CDX rows and which queried URL returned them |
| `cdx_queries` | query log for "no capture" and range-query TTLs |
| `verifications` | append-only verdicts referencing their `file_snapshots` row |
| `access_checks` | append-only accessibility checks |

Policy (`Config.ttl`): exact (URL, timestamp) CDX rows are cached forever;
"no capture" answers for 1 hour (never for an exact timestamp SPN reported);
other range queries for 6 hours; access checks for 1 day. Pending submissions
are always refreshed from the Archive; only final results are cached.
Update triggers make the history tables append-only.

Helpers: `history(url)`, `file_history(path)`, `snapshots_by_digest(digest)`,
`latest_verification(url)`, `pending_submissions()`, `prune(older_than)`.

## Save Page Now: confirmed behavior

Source: "Save Page Now 2 Public API Docs" (Google Doc linked from
<https://web.archive.org/save>), revision dated 2026-07-22, **checked
2026-09-25**. None of these limits were exercised live by this package's tests.

| Item | Documented |
| --- | --- |
| Submission | `POST https://web.archive.org/save` with `url=` and options (form-encoded), `Accept: application/json`. Returns `{"url", "job_id"}` (plus `message` when a recent capture is reused). Options are on only for `1`/`on`. |
| Status | `GET https://web.archive.org/save/status/{job_id}` → `pending`, `success` (`timestamp`, `original_url` after redirects) or `error` (`status_ext`, `message`). Kept for about 1 hour. |
| Authentication | `Authorization: LOW access:secret` (S3 keys, preferred) or login cookies. The docs say SPN2 requires authentication but also list anonymous limits. |
| Rate | 7 captures/minute authenticated, 3/minute anonymous. |
| Daily quotas | 30k captures authenticated, 200 anonymous; 5 GB of captured data authenticated; anonymous 2 GB (`error:max-daily-bandwidth-from-ip`) or 500 MB (limitations table) — the doc contradicts itself; 100 GB per target host; a URL 5 times per day (limitations table) or 10 (`error:too-many-daily-captures`). |
| Concurrency | per-account concurrent session limit, error `error:user-session-limit`; `GET /save/status/user` returns `{"available", "processing"}`. |
| Capture delays | more than 20 concurrent captures (last 60 s) on one host delay new ones by count/5 seconds; after a target's HTTP 429, captures to it are delayed 10–20 s for 60 s; `delay_wb_availability=1` makes a capture visible after ~12 h. Newly captured URLs can take a while to appear in the CDX index. |
| Other limits | 2 GB max resource size, 3 redirects followed, 50 s page load, 2 min total capture time, 10 s connect timeout. |
| Error codes | `error:bad-gateway`, `bad-request`, `bandwidth-limit-exceeded`, `blocked`, `blocked-client-ip`, `blocked-url`, `browsing-timeout`, `capture-location-error`, `cannot-fetch`, `celery`, `filesize-limit`, `ftp-access-denied`, `gateway-timeout`, `http-version-not-supported`, `internal-server-error`, `invalid-url-syntax`, `invalid-server-response`, `invalid-host-resolution`, `job-failed`, `method-not-allowed`, `not-implemented`, `no-browsers-available`, `network-authentication-required`, `no-access`, `not-found`, `proxy-error`, `protocol-error`, `read-timeout`, `soft-time-limit-exceeded`, `service-unavailable`, `too-many-daily-captures`, `too-many-redirects`, `too-many-requests` (the *target* throttles SPN), `user-session-limit`, `unauthorized`, `max-daily-bandwidth`, `max-daily-bandwidth-from-ip`, `max-daily-bandwidth-host`. |

How the package applies this:

- Default SPN pacing: one request start every ~9 s (2 concurrent) with
  credentials, every 21 s (1 concurrent) without (`Config.spn_limit`). No bulk
  quota is assumed beyond the documented per-minute rate.
- HTTP 429 and `error:user-session-limit` are returned as results. In bulk mode
  they pause all further submissions for `Retry-After` (or
  `spn_rate_limit_backoff`, 60 s) and retry that URL at most
  `spn_rate_limit_retries` (2) times. `error:max-daily-bandwidth*` stops the
  batch; remaining URLs get `skipped:quota`.
- 5xx and connection errors are retried with exponential backoff (bounded,
  `Retry-After` honored).
- If a job's status has expired (HTTP 404), the CDX index is searched for a
  capture made since the submission. This mapping of an expired job to 404 is
  an assumption, not documented behavior.
- CDX requests default to one every 2.5 s and `/web/` replay checks to 8 per
  second: 80% of the 30/min and 600/min limits the Archive asked EDGI's
  `wayback` client to respect.

## Existing packages evaluated (step 0)

Checked 2026-09-25 against the current releases' source.

| Package | Latest release | Maintained | Sync/async | SPN2 + auth | Raw CDX `digest` | License |
| --- | --- | --- | --- | --- | --- | --- |
| `internetarchive` (official) | 5.11.1, 2026-08-19 | yes | sync (`requests`) | no SPN or CDX wrappers at all (items: upload, download, metadata, search) | no | AGPL-3.0 |
| `wayback` (EDGI) | 0.5.1, 2026-06-19 | yes | sync (`requests`) | no SPN | yes (`CdxRecord.digest`, from/to, rate limiting) | BSD-3-Clause |
| `waybackpy` | 3.0.6, 2022-03-15 | no (no release since 2022) | sync (`requests`) | `GET /save/<url>` without keys or job status | yes | MIT |
| `savepagenow` | 1.3.1, 2026-01-19 | yes | sync (`requests`) | blocking `GET /save/<url>`, optional `LOW` keys from `SAVEPAGENOW_*` env vars; no job ids or status polling | no CDX | MIT |

Decision:

- **No runtime dependency on any of them.** None is async, so each call would
  need `asyncio.to_thread`, a second HTTP stack (`requests`) and a second set
  of rate limiters that cannot coordinate with the bulk scheduler. SPN2 job
  submission plus status polling, which bulk mode and resumable waits need, is
  not offered by any of them.
- **Credentials follow `internetarchive`'s conventions** (`ia.ini` search order,
  `[s3] access/secret`, `IA_ACCESS_KEY_ID`/`IA_SECRET_ACCESS_KEY`) through a
  small compatible reader instead of a dependency: the package is AGPL-3.0 and
  pulls in `requests`, `jsonpatch` and `urllib3` for about 30 lines of config
  parsing.
- CDX handling is a single exact-match query, so EDGI's `wayback` would add
  little; its documented rate limits informed the defaults above.

## Development

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then:

```sh
uv sync --locked
uv run ruff format .
uv run ruff check .
uv run mypy          # --strict, configured in pyproject.toml
uv run pytest        # unit tests; recorded Archive responses in tests/data
uv run pytest -m live
```

`pytest -m live` talks to the real Archive. `test_live_verify_existing_capture`
only reads (CDX plus one archived file). `test_live_submit_verify_rerun`
creates real captures of two RFC text files (override with
`WAYBACK_VERIFY_LIVE_URLS`), verifies downloads against them, checks access,
then reruns and asserts nothing is resubmitted or rehashed. It uses your
`ia.ini` keys when present.

The dev dependency group pins `aiohttp<3.14` because `aioresponses` 0.7.9
cannot build aiohttp 3.14 responses yet (pnuckowski/aioresponses#288); the
runtime requirement stays `aiohttp>=3.10,<4`.

Code lives in `src/wayback_verify/`, tests in `tests/`, and utilities in
`scripts/`. Python 3.13 is used locally and in Ubuntu CI. Commit `uv.lock`
when dependencies change.

## Releases

PyPI publishing is not configured. To build local distributions, run `uv build`.

## License

No license has been selected. Add one when appropriate.

## Implementation plan

See [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) for the original scope,
API and verification requirements. Deviations: `report()` is a coroutine;
`cache_path` defaults to `None` (in-memory); a `FILE_ERROR` outcome and a
`NOT_SUBMITTED` submission status were added.
