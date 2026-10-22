"""Neo4j access: connection, readiness, schema bootstrap, id counter."""

import socket
import time
from pathlib import Path

from neo4j import Driver, GraphDatabase, ManagedTransaction
from neo4j.exceptions import AuthError, DriverError, Neo4jError

from kb.config import Config
from kb.paths import auth_path

BOLT_HOST = "127.0.0.1"
DATABASE = "neo4j"
COUNTER_NAME = "record"
# ids 1..264 are reserved for the migrated state.md items; the first next_id() returns 265
COUNTER_START = 264
MAX_RETRY_SECONDS = 2.0
CONNECT_TIMEOUT_SECONDS = 3.0

SCHEMA = (
    "CREATE CONSTRAINT record_id IF NOT EXISTS FOR (r:Record) REQUIRE r.id IS UNIQUE",
    "CREATE CONSTRAINT record_slug IF NOT EXISTS FOR (r:Record) REQUIRE r.slug IS UNIQUE",
    "CREATE CONSTRAINT counter_name IF NOT EXISTS FOR (c:Counter) REQUIRE c.name IS UNIQUE",
    "CREATE CONSTRAINT meta_name IF NOT EXISTS FOR (m:Meta) REQUIRE m.name IS UNIQUE",
    "CREATE CONSTRAINT session_id IF NOT EXISTS FOR (s:Session) REQUIRE s.session_id IS UNIQUE",
    "CREATE INDEX record_kind IF NOT EXISTS FOR (r:Record) ON (r.kind)",
    "CREATE INDEX record_scope IF NOT EXISTS FOR (r:Record) ON (r.scope)",
    "CREATE INDEX record_external_id IF NOT EXISTS FOR (r:Record) ON (r.external_id)",
    "CREATE INDEX event_kind IF NOT EXISTS FOR (e:Event) ON (e.kind)",
)
REQUIRED_CONSTRAINTS = frozenset(
    {"record_id", "record_slug", "counter_name", "meta_name", "session_id"}
)
REQUIRED_INDEXES = frozenset(
    {"record_text", "record_kind", "record_scope", "record_external_id", "event_kind"}
)
# The full-text index is checked by definition, not by name: CREATE ... IF NOT EXISTS is a
# silent no-op when an index of that name exists with other properties or another analyzer,
# and Neo4j refuses a second full-text index on the same label and properties (docs/adr/0006).
FULLTEXT_INDEX = "record_text"
FULLTEXT_LABEL = "Record"
FULLTEXT_PROPERTIES = ("title", "summary", "body")
# Neo4j's default analyzer, named so the definition does not depend on the server's default
FULLTEXT_ANALYZER = "standard-no-stop-words"
FULLTEXT_CREATE = (
    f"CREATE FULLTEXT INDEX {FULLTEXT_INDEX} IF NOT EXISTS FOR (r:{FULLTEXT_LABEL}) "
    f"ON EACH [{', '.join(f'r.{p}' for p in FULLTEXT_PROPERTIES)}] "
    f"OPTIONS {{indexConfig: {{`fulltext.analyzer`: '{FULLTEXT_ANALYZER}'}}}}"
)
INDEX_WAIT_SECONDS = 300
COUNTER_INIT = "MERGE (c:Counter {name: $name}) ON CREATE SET c.value = $start"
NEXT_ID = "MATCH (c:Counter {name: $name}) SET c.value = c.value + 1 RETURN c.value AS value"


def bolt_uri(port: int, host: str = BOLT_HOST) -> str:
    """``bolt://<host>:<port>``."""
    return f"bolt://{host}:{port}"


def read_auth(path: Path) -> tuple[str, str] | None:
    """``neo4j:<password>`` from *path*; None when the file is absent (auth disabled)."""
    if not path.exists():
        return None
    user, sep, password = path.read_text().strip().partition(":")
    if not sep or not password:
        raise ValueError(f"{path}: expected one line 'neo4j:<password>'")
    return user, password


def connect(cfg: Config, auth_file: Path | None = None, home: Path | None = None) -> Driver:
    """A driver for the configured bolt port; auth from the auth file, none when it is absent.

    Short retry and connect timeouts: a stopped service must fail in seconds, not the
    driver's default 30 s.
    """
    auth = read_auth(auth_file or auth_path(home or Path.home()))
    return GraphDatabase.driver(
        bolt_uri(cfg.neo4j.bolt_port),
        auth=auth,
        max_transaction_retry_time=MAX_RETRY_SECONDS,
        connection_timeout=CONNECT_TIMEOUT_SECONDS,
        # a query on a property or relationship type nobody wrote yet is normal here
        notifications_min_severity="OFF",
    )


def bolt_open(host: str, port: int, timeout: float = 1.0) -> bool:
    """True when a TCP connection to host:port succeeds within *timeout* seconds."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def wait_for_bolt(host: str, port: int, timeout: float) -> None:
    """Poll every 0.5 s until bolt accepts TCP; TimeoutError after *timeout* seconds."""
    deadline = time.monotonic() + timeout
    while not bolt_open(host, port):
        if time.monotonic() >= deadline:
            raise TimeoutError(f"bolt on {host}:{port} not reachable after {timeout:g}s")
        time.sleep(0.5)


def wait_for_database(driver: Driver, timeout: float) -> None:
    """Bolt opens before the ``neo4j`` database finishes starting; probe until it answers."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            driver.execute_query("RETURN 1", database_=DATABASE)
            return
        except AuthError:
            # a wrong password never becomes right by waiting
            raise
        except (Neo4jError, DriverError) as exc:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"neo4j database not ready after {timeout:g}s: {exc}") from exc
            time.sleep(0.5)


def bootstrap_schema(driver: Driver) -> list[str]:
    """Idempotent: constraints, full-text and range indexes, and the Counter singleton.

    A full-text index whose definition is stale is dropped, created again and waited for;
    returns the names recreated that way.
    """
    for statement in SCHEMA:
        driver.execute_query(statement, database_=DATABASE)
    definitions = fulltext_definitions(driver)
    recreated = _stale(definitions)
    for name in recreated:
        driver.execute_query(f"DROP INDEX {name}", database_=DATABASE)
    driver.execute_query(FULLTEXT_CREATE, database_=DATABASE)
    if recreated or FULLTEXT_INDEX not in definitions:
        # a new index populates in the background; searches must see every record on return
        driver.execute_query(
            "CALL db.awaitIndex($name, $timeout)",
            name=FULLTEXT_INDEX,
            timeout=INDEX_WAIT_SECONDS,
            database_=DATABASE,
        )
    driver.execute_query(COUNTER_INIT, name=COUNTER_NAME, start=COUNTER_START, database_=DATABASE)
    return recreated


def next_id(tx: ManagedTransaction) -> int:
    """Run inside a write transaction: the node lock on Counter serializes callers."""
    return tx.run(NEXT_ID, name=COUNTER_NAME).single(strict=True)["value"]


def allocate_id(driver: Driver) -> int:
    """The next record id, allocated in its own write transaction."""
    with driver.session(database=DATABASE) as session:
        return session.execute_write(next_id)


def counter_value(driver: Driver) -> int | None:
    """The Counter's current value; None when the node does not exist."""
    records, _, _ = driver.execute_query(
        "MATCH (c:Counter {name: $name}) RETURN c.value AS value",
        name=COUNTER_NAME,
        database_=DATABASE,
    )
    return records[0]["value"] if records else None


def constraint_names(driver: Driver) -> set[str]:
    """Names of every constraint in the database."""
    records, _, _ = driver.execute_query(
        "SHOW CONSTRAINTS YIELD name RETURN name", database_=DATABASE
    )
    return {r["name"] for r in records}


def index_names(driver: Driver) -> set[str]:
    """Names of every index in the database."""
    records, _, _ = driver.execute_query("SHOW INDEXES YIELD name RETURN name", database_=DATABASE)
    return {r["name"] for r in records}


def fulltext_definitions(driver: Driver) -> dict[str, tuple[list[str], list[str], str | None]]:
    """``{name: (labels, properties, analyzer)}`` of every full-text index."""
    records, _, _ = driver.execute_query(
        "SHOW FULLTEXT INDEXES YIELD name, labelsOrTypes, properties, options "
        "RETURN name, labelsOrTypes, properties, options",
        database_=DATABASE,
    )
    return {
        r["name"]: (
            list(r["labelsOrTypes"] or []),
            list(r["properties"] or []),
            ((r["options"] or {}).get("indexConfig") or {}).get("fulltext.analyzer"),
        )
        for r in records
    }


def stale_indexes(driver: Driver) -> list[str]:
    """Names of kb's full-text indexes that exist with another definition (labels, properties
    in order, analyzer); a missing one is not stale, it is absent."""
    return _stale(fulltext_definitions(driver))


def _stale(definitions: dict[str, tuple[list[str], list[str], str | None]]) -> list[str]:
    found = definitions.get(FULLTEXT_INDEX)
    wanted = ([FULLTEXT_LABEL], list(FULLTEXT_PROPERTIES), FULLTEXT_ANALYZER)
    return [FULLTEXT_INDEX] if found is not None and found != wanted else []


def schema_present(driver: Driver) -> bool:
    """True when the required constraints and the full-text index all exist."""
    return (
        constraint_names(driver) >= REQUIRED_CONSTRAINTS and index_names(driver) >= REQUIRED_INDEXES
    )
