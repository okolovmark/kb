import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from neo4j import Driver
from neo4j.exceptions import AuthError, ServiceUnavailable

from kb.db import (
    COUNTER_START,
    REQUIRED_CONSTRAINTS,
    REQUIRED_INDEXES,
    allocate_id,
    bootstrap_schema,
    constraint_names,
    counter_value,
    index_names,
    schema_present,
    wait_for_database,
)


def test_bootstrap_schema_is_idempotent(driver: Driver) -> None:
    bootstrap_schema(driver)
    bootstrap_schema(driver)
    assert constraint_names(driver) >= REQUIRED_CONSTRAINTS
    assert index_names(driver) >= REQUIRED_INDEXES
    assert schema_present(driver)
    records, _, _ = driver.execute_query(
        "MATCH (c:Counter) RETURN count(c) AS n", database_="neo4j"
    )
    assert records[0]["n"] == 1
    records, _, _ = driver.execute_query(
        "SHOW CONSTRAINTS YIELD name, type, labelsOrTypes, properties "
        "WHERE name IN ['record_id', 'record_slug'] RETURN name, type, labelsOrTypes, properties",
        database_="neo4j",
    )
    rows = {r["name"]: (r["type"], r["labelsOrTypes"], r["properties"]) for r in records}
    assert rows["record_id"] == ("NODE_PROPERTY_UNIQUENESS", ["Record"], ["id"])
    assert rows["record_slug"] == ("NODE_PROPERTY_UNIQUENESS", ["Record"], ["slug"])


def test_next_id_is_serialized_across_threads(driver: Driver) -> None:
    bootstrap_schema(driver)
    start = counter_value(driver)
    assert start is not None and start >= COUNTER_START

    def worker() -> list[int]:
        return [allocate_id(driver) for _ in range(5)]

    with ThreadPoolExecutor(max_workers=20) as pool:
        futures = [pool.submit(worker) for _ in range(20)]
        ids = [value for future in futures for value in future.result()]

    assert len(ids) == 100
    assert sorted(ids) == list(range(start + 1, start + 101))
    assert counter_value(driver) == start + 100


class FlakyDriver:
    """execute_query raises the queued exceptions in order, then succeeds."""

    def __init__(self, *failures: Exception) -> None:
        self.failures = list(failures)
        self.calls = 0

    def execute_query(self, *_args, **_kwargs):
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return ([], None, [])


def test_wait_for_database_fails_fast_on_auth_error() -> None:
    driver = FlakyDriver(AuthError("bad password"))
    started = time.monotonic()
    with pytest.raises(AuthError):
        wait_for_database(driver, timeout=30)
    assert time.monotonic() - started < 1
    assert driver.calls == 1


def test_wait_for_database_retries_transient_errors_until_the_deadline() -> None:
    driver = FlakyDriver(ServiceUnavailable("starting"), ServiceUnavailable("starting"))
    wait_for_database(driver, timeout=10)
    assert driver.calls == 3

    stuck = FlakyDriver(*[ServiceUnavailable("down")] * 50)
    with pytest.raises(TimeoutError, match="not ready after 0s") as info:
        wait_for_database(stuck, timeout=0)
    assert isinstance(info.value.__cause__, ServiceUnavailable)
