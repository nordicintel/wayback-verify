from __future__ import annotations

import hashlib
import io
import os
from dataclasses import replace
from pathlib import Path

import pytest
from helpers import fast_config, no_sleep

from wayback_verify import (
    ArchiveClient,
    hex_sha1_to_base32,
    normalize_digest,
    sha1_base32,
)
from wayback_verify import files as files_mod
from wayback_verify.files import base32_to_hex, is_digest, snapshot_file, stat_file

# FIPS 180 test vectors, with their Base32 (CDX) forms.
VECTORS = [
    (
        b"",
        "da39a3ee5e6b4b0d3255bfef95601890afd80709",
        "3I42H3S6NNFQ2MSVX7XZKYAYSCX5QBYJ",
    ),
    (
        b"abc",
        "a9993e364706816aba3e25717850c26c9cd0d89d",
        "VGMT4NSHA2AWVOR6EVYXQUGCNSONBWE5",
    ),
    (
        b"abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq",
        "84983e441c3bd26ebaae4aa1f95129e5e54670f1",
        "QSMD4RA4HPJG5OVOJKQ7SUJJ4XSUM4HR",
    ),
]


@pytest.mark.parametrize(("data", "hex_digest", "b32"), VECTORS)
def test_sha1_vectors(data: bytes, hex_digest: str, b32: str) -> None:
    assert hashlib.sha1(data).hexdigest() == hex_digest
    assert sha1_base32(data) == b32
    assert sha1_base32(io.BytesIO(data)) == b32
    assert hex_sha1_to_base32(hex_digest) == b32
    assert base32_to_hex(b32) == hex_digest


def test_normalize_digest() -> None:
    b32 = VECTORS[1][2]
    assert normalize_digest(f"sha1:{b32.lower()}") == b32
    assert normalize_digest(f"  SHA1:{b32} ") == b32
    assert normalize_digest(VECTORS[1][1]) == b32  # hex is converted
    assert normalize_digest("-") is None
    assert normalize_digest("") is None
    assert normalize_digest(None) is None
    assert is_digest(b32) and is_digest(VECTORS[1][1]) and is_digest(f"sha1:{b32}")
    assert not is_digest("report.pdf")


def test_snapshot_records_file_state(tmp_path: Path) -> None:
    f = tmp_path / "a.bin"
    f.write_bytes(b"abc")
    snap = snapshot_file(f)
    st = f.stat()
    assert snap.path == str(f.resolve())
    assert (snap.size, snap.mtime_ns, snap.ctime_ns) == (
        3,
        st.st_mtime_ns,
        st.st_ctime_ns,
    )
    assert (snap.inode, snap.device) == (st.st_ino, st.st_dev)
    assert snap.sha1_b32 == VECTORS[1][2] and snap.sha1_hex == VECTORS[1][1]
    assert snap.stable and snap.hashed_at.tzinfo is not None
    assert snap.platform
    assert snap.to_dict()["mtime"].startswith(str(snap.mtime.year))


def test_hash_streams_in_chunks(tmp_path: Path) -> None:
    data = os.urandom(files_mod.CHUNK_SIZE * 2 + 17)
    f = tmp_path / "big.bin"
    f.write_bytes(data)
    seen: list[int] = []
    snap = snapshot_file(f, on_bytes=seen.append)
    assert seen == [files_mod.CHUNK_SIZE, files_mod.CHUNK_SIZE, 17]
    assert snap.sha1_b32 == sha1_base32(data)


def test_modified_during_hashing_is_rehashed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = tmp_path / "moving.txt"
    f.write_bytes(b"one")
    real = files_mod._hash_path
    count = {"n": 0}

    def hash_and_modify(path: str, on_bytes: object) -> bytes:
        digest = real(path, None)
        count["n"] += 1
        if count["n"] == 1:
            f.write_bytes(b"two!")  # the file changes while it is being read
        return digest

    monkeypatch.setattr(files_mod, "_hash_path", hash_and_modify)
    snap = snapshot_file(f)
    assert count["n"] == 2
    assert snap.stable
    assert snap.sha1_b32 == sha1_base32(b"two!")


def test_file_changing_twice_is_unstable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = tmp_path / "moving.txt"
    f.write_bytes(b"x")
    real = files_mod._hash_path

    def hash_and_modify(path: str, on_bytes: object) -> bytes:
        digest = real(path, None)
        f.write_bytes(f.read_bytes() + b"x")
        return digest

    monkeypatch.setattr(files_mod, "_hash_path", hash_and_modify)
    assert not snapshot_file(f).stable


def test_state_comparison_uses_every_property(tmp_path: Path) -> None:
    f = tmp_path / "a"
    f.write_bytes(b"a")
    state = stat_file(f)
    assert state.same_state(state)
    for name in ("size", "mtime_ns", "ctime_ns", "inode", "device"):
        changed = replace(state, **{name: getattr(state, name) + 1})
        assert not state.same_state(changed)


async def test_unchanged_file_reuses_hash(
    client: ArchiveClient, tmp_path: Path
) -> None:
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"content")
    first = await client.hash_file(f)
    second = await client.hash_file(f)
    assert not first.reused and second.reused
    assert second.id == first.id and second.sha1_b32 == first.sha1_b32
    assert len(await client.file_history(f)) == 1


async def test_touching_mtime_forces_rehash(
    client: ArchiveClient, tmp_path: Path
) -> None:
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"content")
    first = await client.hash_file(f)
    later = first.mtime_ns + 5_000_000_000
    os.utime(f, ns=(later, later))
    second = await client.hash_file(f)
    assert not second.reused and second.id != first.id
    assert second.sha1_b32 == first.sha1_b32
    history = await client.file_history(f)
    assert [s.id for s in history] == [first.id, second.id]  # old snapshot kept


async def test_replaced_file_forces_rehash(
    client: ArchiveClient, tmp_path: Path
) -> None:
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"old-content")
    first = await client.hash_file(f)
    part = tmp_path / "doc.pdf.part"
    part.write_bytes(b"new-content")  # same size
    os.utime(part, ns=(first.mtime_ns, first.mtime_ns))  # same mtime
    os.replace(part, f)
    second = await client.hash_file(f)
    assert not second.reused
    assert (second.inode, second.ctime_ns) != (first.inode, first.ctime_ns)
    assert second.sha1_b32 == sha1_base32(b"new-content")
    by_digest = await client.snapshots_by_digest(second.sha1_b32)
    assert [s.id for s in by_digest] == [second.id]


async def test_force_rehashes(client: ArchiveClient, tmp_path: Path) -> None:
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"content")
    first = await client.hash_file(f)
    forced = await client.hash_file(f, force=True)
    assert not forced.reused and forced.id != first.id


async def test_unstable_snapshot_is_not_reused(
    client: ArchiveClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"content")
    real = files_mod.snapshot_file
    monkeypatch.setattr(
        "wayback_verify.client.snapshot_file",
        lambda p, on_bytes=None: replace(real(p), stable=False),
    )
    first = await client.hash_file(f)
    monkeypatch.setattr("wayback_verify.client.snapshot_file", real)
    second = await client.hash_file(f)
    assert not first.stable and not second.reused


async def test_snapshots_persist_across_runs(cache_path: Path, tmp_path: Path) -> None:
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"content")
    async with ArchiveClient(fast_config(), cache_path, sleep=no_sleep) as c:
        first = await c.hash_file(f)
    async with ArchiveClient(fast_config(), cache_path, sleep=no_sleep) as c:
        again = await c.hash_file(f)
    assert again.reused and again.id == first.id


async def test_memory_cache_reuses_within_run_only(tmp_path: Path) -> None:
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"content")
    for _ in range(2):
        async with ArchiveClient(fast_config(), None, sleep=no_sleep) as c:
            assert not (await c.hash_file(f)).reused
            assert (await c.hash_file(f)).reused
