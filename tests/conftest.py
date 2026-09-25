from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from aioresponses import aioresponses
from helpers import fast_config, no_sleep

from wayback_verify import ArchiveClient


@pytest.fixture
def mock() -> Iterator[aioresponses]:
    with aioresponses() as m:
        yield m


@pytest.fixture
def cache_path(tmp_path: Path) -> Path:
    return tmp_path / "wayback.sqlite"


@pytest.fixture
async def client(cache_path: Path) -> AsyncIterator[ArchiveClient]:
    async with ArchiveClient(fast_config(), cache_path, sleep=no_sleep) as c:
        yield c
