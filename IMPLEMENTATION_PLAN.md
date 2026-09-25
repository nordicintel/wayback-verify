# Implementation Plan: Wayback archiving and verification package

## Context

This is a standalone, reusable Python package with these jobs:

- Archive official source files through the Internet Archive's Wayback Machine.
- Check that those archived files match files already downloaded locally.
- Keep a local sqlite3 history and result cache.

It also includes ease-of-use features:

- Bulk submission and verification.
- File-path inputs, with hashing handled for you.
- Async progress bars.
- Tracking each local file's state, meaning its size and its modification and creation times, alongside every hash generated from it.

**Scope:** archiving and verification only. The calling application handles file storage, observations, and scheduling.

The package name is a placeholder: `wayback-verify` (import name `wayback_verify`).

## Existing packages to evaluate first (step 0)

The plan above currently implements all HTTP calls directly with `aiohttp`. It does **not** use the Internet Archive's own `internetarchive` Python package yet. That package must be evaluated before implementation starts, and the outcome recorded in the README.

- **`internetarchive`**, the official package (`pip install internetarchive`):
  - Its main job is archive.org *items*: upload, download, metadata, and search. The requirements note that its upload workflow is different from Save Page Now.
  - It is synchronous and built on `requests`, so it cannot be used inside the `aiohttp` request path without threads.
  - Whether it wraps Save Page Now or CDX at all is **unverified**. Check the current source and documentation.
  - Its most likely useful parts:
    - Credential handling: `ia configure` stores IA S3 keys in `ia.ini`, and the same keys authenticate Save Page Now.
    - Session and config conventions.

  If those hold, reuse its config file and credential loading, or depend on it only for that, rather than inventing a separate credential format.
- **Third-party packages to check as well:**
  - `wayback`, from EDGI: a CDX search and memento client.
  - `waybackpy`: Save Page Now, CDX, and availability wrappers.
  - `savepagenow`: a Save Page Now wrapper.

  For each, record:
  - whether it is maintained
  - whether it is sync or async
  - whether it supports SPN2 with authentication
  - whether it exposes the raw CDX `digest` for exact-capture selection
  - its license

**Decision rule:** use an existing package where it covers a component correctly and works with async and bulk use, for example by wrapping the calls with `asyncio.to_thread` where that is acceptable. Otherwise, keep the direct `aiohttp` implementation. The file-snapshot tracking, the sqlite cache, bulk and progress handling, and the verification outcomes are specific to this package in either case.

## Package layout

```
wayback-verify/
  pyproject.toml            # deps: aiohttp, tqdm; test deps: pytest, pytest-asyncio, aioresponses
  src/wayback_verify/
    __init__.py             # public API
    client.py               # ArchiveClient: owns its aiohttp session, config, cache
    config.py               # credentials, rate limits, timeouts, retry policy, cache TTLs, concurrency
    models.py               # dataclasses + outcome enums
    files.py                # local file snapshots (stat properties) + SHA-1 → Base32 hashing
    http.py                 # throttling, retry/backoff, Retry-After
    spn.py                  # Save Page Now submission + status
    cdx.py                  # CDX capture lookup + capture selection
    access.py               # archived-file accessibility check
    verify.py               # digest comparison and outcomes
    bulk.py                 # bulk submit / wait / verify / check with progress
    report.py               # failure report
    cache.py                # sqlite3 history and result cache
  tests/
```

## Public API

`ArchiveClient` is an async context manager. It creates and closes its own `aiohttp.ClientSession`.

```python
async with ArchiveClient(config, cache_path="wayback.sqlite") as client:
    # single items
    job = await client.submit(url)
    sub = await client.capture_status(job.job_id)
    snap = await client.hash_file(path)             # FileSnapshot (hash + file properties)
    result = await client.verify_file(url, path, timestamp=sub.timestamp)
    access = await client.check_access(result.capture)

    # bulk, with progress bars
    subs = await client.submit_many(urls, progress=True)
    done = await client.wait_for_captures(subs, progress=True)
    results = await client.verify_many({url: path, ...}, progress=True)
    accesses = await client.check_access_many([r.capture for r in results], progress=True)

    report = client.report(since=...)
```

Passing `cache_path=None` disables the cache. When the cache is disabled, file snapshots still work within a single run, but nothing is reused across runs.

## Components

### 1. `files.py`: local file snapshots and hashing

A hash is never tied only to a path. Each hash is stored with the file state it was computed from, as a `FileSnapshot`:

| Field | Source |
| --- | --- |
| `path` | Absolute, resolved path |
| `size` | `st_size` |
| `mtime_ns` | `st_mtime_ns` |
| `ctime_ns` | `st_ctime_ns` (metadata-change time on Unix, creation time on Windows before 3.12). Stored raw, together with the platform. |
| `birthtime_ns` | `st_birthtime` where the OS exposes it (macOS/BSD, and Windows on 3.12+), otherwise `None` |
| `inode`, `device` | `st_ino`, `st_dev`, used to detect a replaced file at the same path |
| `sha1_b32` | SHA-1 of the content, Base32-encoded to match the CDX digest format |
| `sha1_hex` | Hex form, for convenience |
| `hashed_at` | When the hash was computed (UTC) |
| `platform` | `sys.platform`, so the timestamp semantics can be interpreted later |

Behavior:

- `hash_file(path)` calls `stat` on the file. If the cache holds a snapshot for that path with the same `size`, `mtime_ns`, `ctime_ns`, `inode`, and `device`, it reuses the stored hash. Otherwise it rehashes and writes a new snapshot row. Old snapshots are kept, so the history of a path shows every content state seen.
- `force=True` always rehashes.
- If the file changes during hashing (a `stat` before and after gives different values), the snapshot is marked unstable and hashed again once.
- Hashing streams 1 MiB chunks and runs in `asyncio.to_thread`. Bulk hashing uses a bounded worker pool.
- `hex_sha1_to_base32()` and `sha1_base32(bytes | BinaryIO)` are available for inputs that are not files.
- Digests are normalized before comparison: strip any `sha1:` prefix and upper-case.

Every verification result and verification history row references the `FileSnapshot` it used. A result therefore records which file state was compared, not just which path.

### 2. `spn.py`: Save Page Now

This uses the Wayback Machine's Save Page Now capture of an existing public URL. It does not use the `internetarchive` library's upload workflow.

- `submit(url, **options)` sends `POST https://web.archive.org/save` with `Accept: application/json`. When credentials are configured, it also sends `Authorization: LOW access:secret`. The call returns a `job_id`.
- `capture_status(job_id)` sends `GET https://web.archive.org/save/status/{job_id}` and returns one of three results:
  - `pending`
  - `success`, with the timestamp, original URL, and dated capture link
  - `error`, with the reported error code
- **Confirm during implementation** and document in the README, with the date each item was checked:
  - the supported submission interface
  - authentication requirements
  - anonymous vs. authenticated limits
  - rate limits and quotas
  - concurrent-job limits
  - capture delays
  - error codes

  Do not assume a bulk-submission quota.

### 3. `cdx.py`: capture metadata

- Query `https://web.archive.org/cdx/search/cdx` with `matchType=exact`, `output=json`, and `fl=timestamp,original,statuscode,mimetype,digest,length`. Add `from` and `to` when a timestamp is known.
- Select the specific capture of the requested URL:
  - Use the exact timestamp when it is known, for example from a successful submission.
  - Otherwise, apply an explicit policy such as "first capture after a datetime". The default datetime is the snapshot's `mtime` or `hashed_at`, so the capture chosen is the one closest to the local file state.
- Compare `original` against the requested URL, so that a canonicalized variant is not accepted silently.
- Keep `length` in the model but document it as the archived record length. It is never used as a file-size check.

### 4. `verify.py`: digest comparison

- `verify_file(url, path, *, timestamp | selection)` hashes the file through `hash_file`, using the cached hash when the file is unchanged. `verify_digest(url, local_digest, ...)` accepts a precomputed digest instead.
- Outcomes:
  - `MATCH`
  - `MISMATCH`
  - `DIGEST_UNAVAILABLE`: the capture exists but its digest is missing or `-`. Verification is unavailable in this case.
  - `NO_CAPTURE`
  - `CAPTURE_FAILED`
  - `LOOKUP_ERROR`: a network or HTTP failure. This is not a verdict.
- Each result contains:
  - the `FileSnapshot` (path, size, mtime, ctime or birthtime, and `hashed_at`)
  - the archive digest
  - the selected capture's timestamp, status code, and MIME type
  - the dated capture link, `https://web.archive.org/web/{timestamp}/{original}`
  - `checked_at`
- On a mismatch, the result compares the file's `mtime` and `hashed_at` with the capture timestamp. It notes that the agency may have replaced the file between the local download and the Archive's fetch.
- A `MATCH` establishes only content correspondence. Accessibility is a separate check.

### 5. `access.py`: accessibility of an archived file

- Send `HEAD` to `https://web.archive.org/web/{timestamp}id_/{original}`. If that fails, fall back to a `GET` with `Range: bytes=0-0` and close the connection without reading the body.
- Record the HTTP status, whether the Archive redirected to a different capture, and the check time.

### 6. `bulk.py`: bulk operations and progress tracking

- Bulk functions:
  - `submit_many(urls)`
  - `wait_for_captures(submissions, poll_interval, timeout)`
  - `hash_files(paths)`
  - `verify_many(mapping | iterable of (url, path | digest))`
  - `check_access_many(captures)`
- Accepted inputs:
  - any iterable, or an async iterable
  - a text file with one URL per line
  - a directory or glob, for `hash_files`
- Concurrency goes through the rate limiters in `http.py` and an `asyncio.Semaphore`. Submissions respect the Save Page Now limits, so bulk mode never fires everything at once.
- Deduplication:
  - URLs that repeat within a batch are submitted only once.
  - URLs with a recent successful capture in the cache are skipped. The skip window is configurable.
- A failure on one item never aborts the batch. Each item returns its own result, errors included.
- Progress:
  - `progress=True` uses `tqdm.asyncio.tqdm`, which is tqdm's async variant: `tqdm.asyncio.tqdm.as_completed` wraps the task set.
  - Hashing progress can be shown in bytes as well as in files.
  - `progress=False` disables the bar. Passing a `tqdm` instance, or any callable `on_progress(item, result)`, allows custom reporting.
  - The bar postfix shows running counts: matched, mismatched, failed, and pending.
- Resuming: pending submissions are stored in the cache. `wait_for_captures()` can be called with no arguments in a later run to resume polling every pending job.

### 7. `report.py`: failure reporting

- Groups results into:
  - failed captures
  - hash mismatches
  - unavailable digests
  - inaccessible captures
- Each entry includes the file snapshot properties.
- Reports can be built from results passed in directly or from the cache history.
- The output is a dataclass that can be serialized to a dict or JSON.

### 8. `cache.py`: sqlite3 history and result cache

Storage and access:

- Uses stdlib `sqlite3` in WAL mode.
- Tracks the schema version with `PRAGMA user_version` and runs migrations when the database opens.
- Runs every database call in `asyncio.to_thread`.
- Batches writes in bulk runs, with one transaction per chunk.

Tables:

| Table | Contents |
| --- | --- |
| `file_snapshots` | `id`, `path`, `size`, `mtime_ns`, `ctime_ns`, `birthtime_ns`, `inode`, `device`, `platform`, `sha1_b32`, `sha1_hex`, `hashed_at`. Indexed on (`path`, `size`, `mtime_ns`, `ctime_ns`, `inode`, `device`) for reuse lookup, and on `sha1_b32`. |
| `submissions` | `job_id`, `url`, `submitted_at`, `status`, `error_code`, `capture_timestamp`, `original_url`, `updated_at`, `raw_json` |
| `captures` | CDX rows, keyed by (`original`, `timestamp`, `digest`), plus `statuscode`, `mimetype`, `length`, `first_seen_at` |
| `cdx_queries` | `url`, `params_hash`, `queried_at`, `result_count`. Used for "no capture" caching. |
| `verifications` | Append-only: `url`, `capture_timestamp`, `file_snapshot_id` (FK), `local_digest`, `archive_digest`, `outcome`, `checked_at`, `note` |
| `access_checks` | Append-only: `original`, `timestamp`, `http_status`, `accessible`, `checked_at` |

Cache policy (TTLs are configurable):

- A CDX row for a given (URL, timestamp) is immutable, so it is cached indefinitely.
- "No capture" results have a short TTL.
- Pending submissions are always refreshed from the Archive. Only final `success` and `error` results are cached.
- `submit()` and `submit_many()` return a recent successful capture of the same URL instead of resubmitting, within a configurable window.
- Access checks have their own TTL.
- A file snapshot is reused only when all of its recorded stat properties match. Snapshots are never overwritten.

Helpers:

- `history(url)`
- `file_history(path)`: every snapshot and hash seen for the path
- `snapshots_by_digest(digest)`
- `latest_verification(url)`
- `pending_submissions()`
- `prune(older_than)`

### 9. `http.py`: throttling and retries

- Separate configurable limits for Save Page Now and CDX, each with a concurrency cap and a minimum spacing between requests.
- Transient failures (429, 5xx, connection errors) are retried with exponential backoff. `Retry-After` is honored, and the number of attempts is bounded.
- Save Page Now quota and rate-limit errors are returned as results, not retried in a loop. In bulk mode they pause further submissions until the backoff expires.

## Implementation order

0. Evaluate `internetarchive` and the third-party Wayback packages as described above, and record the decision.
1. Scaffold the package, `models.py`, and `config.py`, including loading credentials from `ia.ini` if that is adopted. Build `files.py` (snapshots and hashing) with tests.
2. `cache.py`, starting with `file_snapshots`, and snapshot reuse in `hash_file`.
3. `cdx.py` and capture selection, then `verify.py`.
4. `spn.py` and `http.py`, including cached submissions.
5. `access.py`.
6. `bulk.py` with tqdm progress, deduplication, and resumable polling.
7. `report.py`.
8. README covering:
   - usage, including bulk examples
   - the meaning of each outcome
   - the file-snapshot semantics per platform (ctime and birthtime)
   - known causes of mismatches
   - the confirmed Save Page Now limits, with the dates they were checked

## Verification

- **Unit tests** use pytest, pytest-asyncio, aioresponses, and recorded Archive responses. They cover:
  - known SHA-1/Base32 test vectors
  - snapshot reuse and invalidation:
    - touching `mtime` forces a rehash
    - replacing a file at the same path (new inode) forces a rehash
    - an unchanged file reuses its hash
    - a file modified during hashing is detected
  - CDX parsing, including `-` digests and exact timestamp selection
  - every verification outcome
  - Save Page Now status and error mapping
  - the access fallback from `HEAD` to a ranged `GET`
  - bulk operations:
    - deduplication
    - per-item error isolation
    - progress callback counts
    - resuming pending jobs from the cache
  - cache behavior:
    - TTLs
    - immutable captures
    - append-only history
    - migrating an existing database
- **Opt-in live test** (`pytest -m live`):
  1. Bulk-submit a few small, stable public files and wait for their captures.
  2. Download the same URLs locally and run `verify_many`. Assert `MATCH` and that each capture is accessible.
  3. Rerun and confirm that nothing is resubmitted and nothing is rehashed.
- **Static checks:** `ruff` and `mypy --strict`.