"""Local file snapshots (stat properties) and SHA-1 → Base32 hashing."""

from __future__ import annotations

import base64
import hashlib
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO

from .models import FileSnapshot, FileState, utcnow

CHUNK_SIZE = 1 << 20
_B32_ALPHABET = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567")
_HEX = set("0123456789abcdefABCDEF")


def hex_sha1_to_base32(hex_digest: str) -> str:
    """Convert a 40-character hex SHA-1 to the Base32 form used by CDX."""
    raw = bytes.fromhex(hex_digest.strip())
    if len(raw) != 20:
        raise ValueError("not a SHA-1 hex digest")
    return base64.b32encode(raw).decode("ascii")


def base32_to_hex(b32_digest: str) -> str:
    return base64.b32decode(normalize_digest(b32_digest) or "").hex()


def sha1_base32(data: bytes | BinaryIO) -> str:
    """Base32 SHA-1 of bytes or a binary stream (read in 1 MiB chunks)."""
    h = hashlib.sha1()
    if isinstance(data, bytes | bytearray | memoryview):
        h.update(data)
    else:
        while chunk := data.read(CHUNK_SIZE):
            h.update(chunk)
    return base64.b32encode(h.digest()).decode("ascii")


def is_digest(value: str) -> bool:
    """True for a Base32 or hex SHA-1, with or without a ``sha1:`` prefix."""
    v = value.strip()
    if v.lower().startswith("sha1:"):
        v = v[5:]
    return (len(v) == 32 and set(v.upper()) <= _B32_ALPHABET) or (
        len(v) == 40 and set(v) <= _HEX
    )


def normalize_digest(value: str | None) -> str | None:
    """Strip any ``sha1:`` prefix and upper-case; hex SHA-1s become Base32.

    Missing digests (``None``, ``""``, ``"-"``) normalize to ``None``.
    """
    if value is None:
        return None
    v = value.strip()
    if v.lower().startswith("sha1:"):
        v = v[5:]
    if v in ("", "-"):
        return None
    if len(v) == 40 and set(v) <= _HEX:
        return hex_sha1_to_base32(v)
    return v.upper()


def stat_file(path: str | os.PathLike[str]) -> FileState:
    resolved = Path(path).resolve(strict=True)
    st = resolved.stat()
    if not resolved.is_file():
        raise IsADirectoryError(str(resolved))
    birth = getattr(st, "st_birthtime_ns", None)
    if birth is None and (b := getattr(st, "st_birthtime", None)) is not None:
        birth = int(b * 1e9)
    return FileState(
        path=str(resolved),
        size=st.st_size,
        mtime_ns=st.st_mtime_ns,
        ctime_ns=st.st_ctime_ns,
        birthtime_ns=birth,
        inode=st.st_ino,
        device=st.st_dev,
        platform=sys.platform,
    )


def _hash_path(path: str, on_bytes: Callable[[int], None] | None) -> bytes:
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        while chunk := fh.read(CHUNK_SIZE):
            h.update(chunk)
            if on_bytes:
                on_bytes(len(chunk))
    return h.digest()


def snapshot_file(
    path: str | os.PathLike[str],
    *,
    on_bytes: Callable[[int], None] | None = None,
) -> FileSnapshot:
    """Hash a file and record the state it was hashed from (blocking).

    The file is stat'ed before and after hashing. If the two differ the file
    was modified mid-read, so it is hashed once more; if it changes again the
    snapshot is returned with ``stable=False`` (and is never reused).
    """
    for attempt in (1, 2):
        before = stat_file(path)
        digest = _hash_path(before.path, on_bytes)
        after = stat_file(before.path)
        stable = before.same_state(after)
        if stable or attempt == 2:
            break
    return FileSnapshot(
        path=after.path,
        size=after.size,
        mtime_ns=after.mtime_ns,
        ctime_ns=after.ctime_ns,
        birthtime_ns=after.birthtime_ns,
        inode=after.inode,
        device=after.device,
        platform=after.platform,
        sha1_b32=base64.b32encode(digest).decode("ascii"),
        sha1_hex=digest.hex(),
        hashed_at=utcnow(),
        stable=stable,
    )
