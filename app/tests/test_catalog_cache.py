from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from catalog_cache import CatalogCache, CatalogBusy


def test_snapshot_is_reused_only_until_its_ttl_and_cannot_be_mutated():
    clock = [10.0]
    cache = CatalogCache(clock=lambda: clock[0])
    loads = []

    def load():
        loads.append(True)
        return [{"id": len(loads), "title": "original"}]

    first = cache.get(load, ttl=1.0)
    first[0]["title"] = "changed"
    assert cache.get(load, ttl=1.0) == [{"id": 1, "title": "original"}]
    clock[0] = 11.0
    assert cache.get(load, ttl=1.0)[0]["id"] == 2
    assert len(loads) == 2


def test_disabled_cache_always_loads():
    cache = CatalogCache()
    calls = []
    for _ in range(2):
        cache.get(lambda: calls.append(True) or [], ttl=0)
    assert len(calls) == 2


def test_concurrent_expired_reads_refresh_once():
    cache = CatalogCache()
    entered = Event()
    release = Event()
    calls = []

    def load():
        calls.append(True)
        entered.set()
        assert release.wait(2)
        return [{"id": 1}]

    with ThreadPoolExecutor(max_workers=8) as workers:
        first = workers.submit(cache.get, load, ttl=5.0)
        assert entered.wait(2)
        rest = [workers.submit(cache.get, load, ttl=5.0) for _ in range(7)]
        release.set()
        assert first.result() == [{"id": 1}]
        assert all(task.result() == [{"id": 1}] for task in rest)
    assert len(calls) == 1


def test_waiting_for_a_stuck_refresh_is_bounded():
    cache = CatalogCache(wait_timeout=0.01)
    entered = Event()
    release = Event()

    def load():
        entered.set()
        assert release.wait(2)
        return []

    with ThreadPoolExecutor(max_workers=1) as workers:
        first = workers.submit(cache.get, load, ttl=1)
        assert entered.wait(2)
        try:
            with pytest.raises(CatalogBusy):
                cache.get(load, ttl=1)
        finally:
            release.set()
        first.result()


def test_failed_refresh_never_serves_an_expired_snapshot_or_poisons_lock():
    clock = [0.0]
    cache = CatalogCache(clock=lambda: clock[0])
    assert cache.get(lambda: [{"id": 1}], ttl=1) == [{"id": 1}]
    clock[0] = 2.0

    def fail():
        raise OSError("database unavailable")

    with pytest.raises(OSError):
        cache.get(fail, ttl=1)
    assert cache.get(lambda: [{"id": 2}], ttl=1) == [{"id": 2}]


def test_slow_load_does_not_extend_the_freshness_window():
    clock = [0.0]
    cache = CatalogCache(clock=lambda: clock[0])
    calls = []

    def load():
        calls.append(True)
        clock[0] += 2
        return []

    cache.get(load, ttl=1)
    cache.get(load, ttl=1)
    assert len(calls) == 2


@pytest.mark.parametrize("ttl", [-1, float("inf"), float("nan"), 6])
def test_out_of_contract_ttl_is_rejected(ttl):
    with pytest.raises(ValueError):
        CatalogCache().get(lambda: [], ttl=ttl)
