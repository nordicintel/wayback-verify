from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from aioresponses import aioresponses

from wayback_verify import Config, Credentials, RateLimit, RetryPolicy

DATA = Path(__file__).parent / "data"
CDX = re.compile(r"^https://web\.archive\.org/cdx/search/cdx\?.*$")
SAVE = "https://web.archive.org/save"
STATUS = re.compile(r"^https://web\.archive\.org/save/status/.*$")


def load(name: str) -> Any:
    return json.loads((DATA / name).read_text(encoding="utf-8"))


async def no_sleep(_: float) -> None:
    return None


def fast_config(**overrides: Any) -> Config:
    unlimited = RateLimit(concurrency=8, min_interval=0)
    defaults: dict[str, Any] = {
        "credentials": Credentials("key", "secret"),
        "spn_limit": unlimited,
        "status_limit": unlimited,
        "cdx_limit": unlimited,
        "access_limit": unlimited,
        "retry": RetryPolicy(max_attempts=3, base_delay=0, max_delay=0),
        "spn_rate_limit_backoff": 0,
        "poll_interval": 0,
    }
    defaults.update(overrides)
    return Config(**defaults)


def calls(m: aioresponses, method: str, pattern: str) -> list[Any]:
    """Recorded request kwargs for URLs containing ``pattern``."""
    return [
        call
        for (meth, url), reqs in m.requests.items()
        if meth == method and pattern in str(url)
        for call in reqs
    ]


def cdx_rows(*rows: list[str]) -> list[list[str]]:
    header = ["timestamp", "original", "statuscode", "mimetype", "digest", "length"]
    return [header, *rows] if rows else []
