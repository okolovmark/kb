"""One throwaway Neo4j per test run, started from $KB_NEO4J_PACKAGE on ephemeral ports."""

import os
import shutil
import socket
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest
from neo4j import Driver, GraphDatabase

from kb.config import Config, load
from kb.db import (
    COUNTER_NAME,
    COUNTER_START,
    bolt_open,
    bolt_uri,
    bootstrap_schema,
    wait_for_database,
)
from kb.paths import Paths, neo4j_package
from kb.runner import Call, Completed
from kb.setup import conf_settings, package_default_conf, render_conf

START_TIMEOUT = 120


@dataclass(frozen=True)
class Neo4jServer:
    bolt_port: int
    http_port: int
    home: Path
    config_file: Path
    cfg: Config
    paths: Paths


def free_ports(count: int) -> list[int]:
    # Racy by nature: the ports are free when closed here and taken by Neo4j a moment
    # later; another process could grab one in between. Acceptable for a test fixture.
    sockets = [socket.socket() for _ in range(count)]
    try:
        for sock in sockets:
            sock.bind(("127.0.0.1", 0))
        return [sock.getsockname()[1] for sock in sockets]
    finally:
        for sock in sockets:
            sock.close()


@pytest.fixture(scope="session")
def neo4j_server(tmp_path_factory: pytest.TempPathFactory):
    pkg = neo4j_package()
    home = tmp_path_factory.mktemp("home")
    bolt_port, http_port = free_ports(2)
    config_file = home / ".config" / "kb" / "config.toml"
    config_file.parent.mkdir(parents=True)
    config_file.write_text(
        f"[neo4j]\nbolt_port = {bolt_port}\nhttp_port = {http_port}\n"
        'heap_max = "256m"\npagecache = "64m"\n'
    )
    cfg = load(path=config_file, home=home)
    paths = Paths.from_config(cfg, home)
    for directory in paths.neo4j_dirs:
        directory.mkdir(parents=True)
    settings = conf_settings(cfg, paths, auth_enabled=False)
    # Neo4j preallocates 256 MiB per transaction log by default: 512 MB per test run
    settings["db.tx_log.preallocate"] = "false"
    paths.conf_file.write_text(render_conf(settings, package_default_conf(pkg)))

    log = home / "neo4j-console.log"
    with log.open("wb") as fh:
        proc = subprocess.Popen(
            [str(pkg / "bin" / "neo4j"), "console"],
            env={**os.environ, "NEO4J_CONF": str(paths.conf_dir)},
            stdout=fh,
            stderr=subprocess.STDOUT,
        )
    try:
        _wait_for_start(proc, bolt_port, log)
        yield Neo4jServer(bolt_port, http_port, home, config_file, cfg, paths)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        shutil.rmtree(home, ignore_errors=True)


def _wait_for_start(proc: subprocess.Popen[bytes], port: int, log: Path) -> None:
    deadline = time.monotonic() + START_TIMEOUT
    while not bolt_open("127.0.0.1", port):
        if proc.poll() is not None:
            pytest.fail(f"neo4j exited with {proc.returncode}:\n{_tail(log)}")
        if time.monotonic() > deadline:
            proc.kill()
            pytest.fail(f"neo4j did not open bolt within {START_TIMEOUT}s:\n{_tail(log)}")
        time.sleep(0.5)


def _tail(log: Path) -> str:
    return log.read_text(errors="replace")[-4000:]


@pytest.fixture(scope="session")
def driver(neo4j_server: Neo4jServer) -> Driver:
    drv = GraphDatabase.driver(bolt_uri(neo4j_server.bolt_port), auth=None)
    wait_for_database(drv, START_TIMEOUT)
    yield drv
    drv.close()


@pytest.fixture
def cli_env(neo4j_server: Neo4jServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the CLI at the fixture DB: its config file, and no auth file (auth is off)."""
    monkeypatch.setenv("KB_CONFIG", str(neo4j_server.config_file))
    monkeypatch.setenv("KB_NEO4J_AUTH", str(tmp_path / "no-such-auth-file"))


SystemctlFaker = Callable[[str], Callable[[Call], Completed]]


@pytest.fixture
def fake_systemctl() -> SystemctlFaker:
    """``fake_systemctl(state)`` -> RecordingRunner handler: rc 0, ``is-active`` prints state."""

    def factory(state: str) -> Callable[[Call], Completed]:
        def handler(call: Call) -> Completed:
            out = state + "\n" if "is-active" in call.argv else ""
            return subprocess.CompletedProcess(list(call.argv), 0, out, "")

        return handler

    return factory


@dataclass(frozen=True)
class KbEnv:
    """A per-test CLI environment: its config file, the project path for scope `proj`, driver."""

    config_file: Path
    proj: Path
    home: Path
    driver: Driver


@pytest.fixture
def kb_env(
    neo4j_server: Neo4jServer, driver: Driver, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> KbEnv:
    """Schema in place, no Record/Event/Meta nodes, Counter at 264, a config with scope `proj`,
    today 2026-09-14."""
    bootstrap_schema(driver)
    driver.execute_query(
        "MATCH (n) WHERE n:Record OR n:Event OR n:Meta DETACH DELETE n", database_="neo4j"
    )
    driver.execute_query(
        "MATCH (c:Counter {name: $name}) SET c.value = $start",
        name=COUNTER_NAME,
        start=COUNTER_START,
        database_="neo4j",
    )
    proj = tmp_path / "proj"
    proj.mkdir()
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        f"[neo4j]\nbolt_port = {neo4j_server.bolt_port}\nhttp_port = {neo4j_server.http_port}\n"
        f'[scopes]\nproj = "{proj}"\n'
    )
    monkeypatch.setenv("KB_CONFIG", str(config_file))
    monkeypatch.setenv("KB_NEO4J_AUTH", str(tmp_path / "no-such-auth-file"))
    monkeypatch.setenv("KB_TODAY", "2026-09-14")  # a plain Monday
    monkeypatch.delenv("KB_SESSION", raising=False)
    # The suite normally runs from an agent's own shell, which carries this one.
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.chdir(tmp_path)  # outside every scope path
    return KbEnv(config_file, proj, tmp_path, driver)
