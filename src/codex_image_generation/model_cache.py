"""Small, atomic model-directory caches. Never store provider credentials."""

from __future__ import annotations

import json
import asyncio
import math
import os
import tempfile
import time
from pathlib import Path
from typing import Any
from weakref import WeakKeyDictionary, WeakValueDictionary

from .local_files import read_regular_bytes

MAX_CACHE_BYTES = 1_000_000
MAX_CACHE_DEPTH = 32
_CACHE_LOCKS: WeakKeyDictionary[Any, WeakValueDictionary[str, asyncio.Lock]] = WeakKeyDictionary()


def cache_lock(path: Path) -> asyncio.Lock:
    """A process-local per-loop lock; callers recheck the cache after acquiring it."""
    loop = asyncio.get_running_loop()
    locks = _CACHE_LOCKS.setdefault(loop, WeakValueDictionary())
    key = os.path.normcase(str(path.absolute()))
    lock = locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        locks[key] = lock
    return lock


def _valid_timestamp(value: Any) -> bool:
    try:
        return type(value) in {int, float} and math.isfinite(value) and 0 < value <= time.time() + 300
    except (OverflowError, ValueError):
        return False


def _valid_nesting(value: Any) -> bool:
    """Bound container depth explicitly instead of relying on JSON decoder recursion."""
    iterators = [iter((value,))]
    while iterators:
        try:
            current = next(iterators[-1])
        except StopIteration:
            iterators.pop()
            continue
        if isinstance(current, (dict, list, tuple)):
            if len(iterators) > MAX_CACHE_DEPTH:
                return False
            children = current.values() if isinstance(current, dict) else current
            iterators.append(iter(children))
    return True


def read_cache(path: Path, scope: str) -> dict[str, Any] | None:
    try:
        data = read_regular_bytes(path, MAX_CACHE_BYTES)
        entry = json.loads(data)
        if not isinstance(entry, dict) or type(entry.get("version")) is not int or entry["version"] != 1 or entry.get("scope") != scope:
            return None
        fetched = entry.get("fetched_at")
        if not _valid_timestamp(fetched) or not _valid_nesting(entry):
            return None
        return entry
    except (OSError, ValueError, TypeError, RecursionError, OverflowError):
        return None


def fresh(entry: dict[str, Any] | None, ttl: float) -> bool:
    return (isinstance(entry, dict) and _valid_timestamp(entry.get("fetched_at"))
            and time.time() - entry["fetched_at"] < ttl)


def write_cache(path: Path, entry: dict[str, Any]) -> bool:
    temporary = None
    try:
        if not _valid_nesting(entry):
            return False
        data = json.dumps(entry, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(data) > MAX_CACHE_BYTES:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".models-", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
        os.replace(temporary, path)
        return True
    except (OSError, ValueError, TypeError, RecursionError, OverflowError):
        return False
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
