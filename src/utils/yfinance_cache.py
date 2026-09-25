# -*- coding: utf-8 -*-
"""Keep yfinance's ticker-timezone cache in memory.

yfinance stores it in a peewee SQLite database (WAL mode) under the user
cache directory and opens a connection per thread. Long-running servers with
many short-lived download threads leak those connections until the process
runs out of file descriptors, and the WAL file is unsafe on network home
directories. The cache only maps ticker -> timezone, so a process-local dict
keeps the benefit without the files.
"""
from __future__ import annotations

import threading


class _MemoryTzCache:
    def __init__(self):
        self._values = {}
        self._lock = threading.Lock()

    def lookup(self, key):
        with self._lock:
            return self._values.get(key)

    def store(self, key, value):
        with self._lock:
            if value is None:
                self._values.pop(key, None)
            else:
                self._values[key] = value

    @property
    def tz_db(self):
        return None


def use_memory_tz_cache() -> bool:
    """Install the in-memory cache; returns False when yfinance is unavailable."""
    try:
        from yfinance import cache
    except Exception:
        return False
    cache._TzCacheManager._tz_cache = _MemoryTzCache()
    return True
