import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from neo4j import Driver
from neo4j.exceptions import AuthError, ServiceUnavailable

from kb.db import (
    COUNTER_START,
    FULLTEXT_CREATE,
    REQUIRED_CONSTRAINTS,
    REQUIRED_INDEXES,
    allocate_id,
    bootstrap_schema,
    constraint_names,
    counter_value,
    fulltext_definitions,
    index_names,
    schema_present,
    stale_indexes,
    wait_for_database,
)

# record_text as kb created it before 2026-10-22: no summary, the server's default analyzer
OLD_RECORD_TEXT = "CREATE FULLTEXT INDEX record_text FOR (r:Record) ON EACH [r.title, r.body]"


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


def _hits(driver: Driver, query: str) -> list[int]:
    records, _, _ = driver.execute_query(
        "CALL db.index.fulltext.queryNodes('record_text', $q) YIELD node RETURN node.id AS id",
        q=query,
        database_="neo4j",
    )
    return [r["id"] for r in records]


def test_full_text_index_covers_the_summary_with_the_default_analyzer(driver: Driver) -> None:
    bootstrap_schema(driver)
    assert fulltext_definitions(driver)["record_text"] == (
        ["Record"],
        ["title", "summary", "body"],
        "standard-no-stop-words",
    )
    assert stale_indexes(driver) == []


def test_a_stale_full_text_index_is_rebuilt_and_waited_for(driver: Driver) -> None:
    bootstrap_schema(driver)
    driver.execute_query("DROP INDEX record_text", database_="neo4j")
    driver.execute_query(OLD_RECORD_TEXT, database_="neo4j")
    driver.execute_query(
        "CREATE (:Record {id: -1, title: 'crossing', summary: 'zebra', body: ''})",
        database_="neo4j",
    )
    try:
        driver.execute_query("CALL db.awaitIndexes(60)", database_="neo4j")
        assert stale_indexes(driver) == ["record_text"]
        assert schema_present(driver)  # present by name, which is all IF NOT EXISTS looks at
        driver.execute_query(FULLTEXT_CREATE, database_="neo4j")  # a silent no-op
        assert stale_indexes(driver) == ["record_text"]
        # until kb-setup runs, search keeps answering from the old index
        assert _hits(driver, "crossing") == [-1]
        assert _hits(driver, "zebra") == []

        assert bootstrap_schema(driver) == ["record_text"]
        assert stale_indexes(driver) == []
        assert _hits(driver, "zebra") == [-1]  # online and complete on return
        assert bootstrap_schema(driver) == []
    finally:
        driver.execute_query("MATCH (r:Record {id: -1}) DETACH DELETE r", database_="neo4j")
        bootstrap_schema(driver)


def test_another_analyzer_is_stale_too(driver: Driver) -> None:
    bootstrap_schema(driver)
    driver.execute_query("DROP INDEX record_text", database_="neo4j")
    driver.execute_query(
        "CREATE FULLTEXT INDEX record_text FOR (r:Record) ON EACH [r.title, r.summary, r.body] "
        "OPTIONS {indexConfig: {`fulltext.analyzer`: 'english'}}",
        database_="neo4j",
    )
    try:
        assert stale_indexes(driver) == ["record_text"]
    finally:
        bootstrap_schema(driver)
    assert stale_indexes(driver) == []
