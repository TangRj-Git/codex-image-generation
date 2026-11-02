"""Bound reads to ordinary files, including across a stat/open replacement race."""

from __future__ import annotations

import os
from pathlib import Path
import stat


def _check_regular_file(info: os.stat_result, max_bytes: int) -> None:
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("Expected a regular file")
    if not 0 <= info.st_size <= max_bytes:
        raise ValueError("File exceeds the local byte limit")


def read_regular_bytes(path: Path, max_bytes: int) -> bytes:
    """Read at most max_bytes from a regular file without opening special files blindly.

    Nonblocking open, where the platform provides it, prevents a replacement FIFO
    from waiting for a writer before the descriptor can be inspected. File growth
    after either stat is handled by a bounded read with one byte of look-ahead.
    """
    if type(max_bytes) is not int or max_bytes < 0:
        raise ValueError("max_bytes must be a nonnegative integer")
    _check_regular_file(os.stat(path), max_bytes)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    try:
        _check_regular_file(os.fstat(descriptor), max_bytes)
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > max_bytes:
            raise ValueError("File exceeds the local byte limit")
        return data
    finally:
        os.close(descriptor)
