"""Small, process-local catalogue snapshot with bounded freshness and refresh waits.

Only public catalogue data belongs here. Authentication, CSRF, entries and outbox
records are never cached. A database failure after expiry does not serve stale data.
"""

from copy import deepcopy
from math import isfinite
from threading import Lock
from time import monotonic


class CatalogBusy(RuntimeError):
    """Another refresh did not finish within the request's waiting budget."""


class CatalogCache:
    def __init__(self, *, clock=monotonic, wait_timeout=1.0, on_event=None):
        self._clock = clock
        self._wait_timeout = wait_timeout
        self._on_event = on_event or (lambda event: None)
        self._refresh_lock = Lock()
        self._snapshot = None

    def _fresh(self):
        snapshot = self._snapshot
        if snapshot is not None and self._clock() < snapshot[0]:
            self._on_event("hit")
            return deepcopy(snapshot[1])
        return None

    def get(self, load, *, ttl):
        if not isfinite(ttl) or not 0 <= ttl <= 5:
            raise ValueError("catalogue TTL must be finite and between 0 and 5 seconds")
        if ttl == 0:
            self._on_event("disabled")
            return load()
        result = self._fresh()
        if result is not None:
            return result
        if not self._refresh_lock.acquire(timeout=self._wait_timeout):
            self._on_event("wait_timeout")
            raise CatalogBusy("catalogue refresh wait expired")
        try:
            result = self._fresh()
            if result is not None:
                return result
            started = self._clock()
            try:
                result = load()
            except Exception:
                self._on_event("load_error")
                raise
            self._snapshot = (started + ttl, deepcopy(result))
            self._on_event("load")
            return result
        finally:
            self._refresh_lock.release()
