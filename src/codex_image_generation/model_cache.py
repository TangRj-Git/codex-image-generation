"""Small, atomic model-directory caches. Never store provider credentials."""

from __future__ import annotations

import json
import math
import os
import tempfile
import time
from pathlib import Path
from typing import Any

MAX_CACHE_BYTES = 1_000_000


def read_cache(path: Path, scope: str) -> dict[str, Any] | None:
    try:
        with path.open("rb") as handle:
            data = handle.read(MAX_CACHE_BYTES + 1)
        if len(data) > MAX_CACHE_BYTES:
            return None
        entry = json.loads(data)
        if not isinstance(entry, dict) or entry.get("version") != 1 or entry.get("scope") != scope:
            return None
        fetched = entry.get("fetched_at")
        if type(fetched) not in {int, float} or not math.isfinite(fetched) or not 0 < fetched <= time.time() + 300:
            return None
        return entry
    except (OSError, ValueError, TypeError):
        return None


def fresh(entry: dict[str, Any] | None, ttl: float) -> bool:
    return entry is not None and time.time() - entry["fetched_at"] < ttl


def write_cache(path: Path, entry: dict[str, Any]) -> bool:
    temporary = None
    try:
        data = json.dumps(entry, ensure_ascii=False).encode("utf-8")
        if len(data) > MAX_CACHE_BYTES:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".models-", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
        os.replace(temporary, path)
        return True
    except (OSError, ValueError, TypeError):
        return False
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
