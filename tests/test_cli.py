import json
import os
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner
from neo4j import Driver

from kb.cli import App, main, parse_params
from kb.db import bootstrap_schema
from kb.paths import UNIT_NAME, neo4j_version
from kb.runner import Call, RecordingRunner


@pytest.mark.usefixtures("cli_env")
def test_query_round_trip(driver: Driver) -> None:
    runner = CliRunner()
    created = runner.invoke(
        main,
        [
            "query",
            "CREATE (p:Probe {name: $name, n: $n, tags: $tags}) RETURN p",
            "-p",
            "name=cli-probe",
            "-p",
            "n=42",
            "-p",
            'tags=["a", "b"]',
        ],
    )
    assert created.exit_code == 0, created.output
    row = json.loads(created.stdout.strip())
    assert row["p"]["labels"] == ["Probe"]
    assert row["p"]["properties"] == {"name": "cli-probe", "n": 42, "tags": ["a", "b"]}
    assert "nodes created: 1" in created.stderr

    read = runner.invoke(
        main,
        [
            "query",
            "MATCH (p:Probe {name: $name}) RETURN p.n AS n, date('2026-09-11') AS d",
            "-p",
            "name=cli-probe",
        ],
    )
    assert read.exit_code == 0, read.output
    lines = [json.loads(line) for line in read.stdout.splitlines()]
    assert lines == [{"n": 42, "d": "2026-09-11"}]
    assert read.stderr == ""

    deleted = runner.invoke(main, ["query", "MATCH (p:Probe) DETACH DELETE p"])
    assert deleted.exit_code == 0, deleted.output
    assert deleted.stdout == ""
    assert "nodes deleted: 1" in deleted.stderr


@pytest.mark.usefixtures("cli_env")
def test_query_reports_cypher_errors(driver: Driver) -> None:
    result = CliRunner().invoke(main, ["query", "THIS IS NOT CYPHER"])
    assert result.exit_code == 1
    assert result.output.startswith("Error:")


def test_parse_params() -> None:
    assert parse_params(("a=1", "b=x", 'c={"k": [1]}', "d=", "e=true")) == {
        "a": 1,
        "b": "x",
        "c": {"k": [1]},
        "d": "",
        "e": True,
    }


def write_unit(home: Path, pkg: str) -> None:
    unit = home / ".config" / "systemd" / "user" / UNIT_NAME
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(f"[Service]\nExecStart={pkg}/bin/neo4j console\n")


@pytest.mark.usefixtures("cli_env")
def test_status_ok_against_running_db(driver: Driver, tmp_path: Path, fake_systemctl) -> None:
    bootstrap_schema(driver)
    write_unit(tmp_path, os.environ["KB_NEO4J_PACKAGE"])
    app = App(runner=RecordingRunner(fake_systemctl("active")), home=tmp_path)
    result = CliRunner().invoke(main, ["--json", "status"], obj=app)
    assert result.exit_code == 0, result.output
    info = json.loads(result.output)
    assert info["unit"] == "active"
    assert info["bolt_reachable"] is True
    assert info["schema_ok"] is True
    assert {"record_id", "record_slug"} <= set(info["constraints"])
    assert isinstance(info["counter"], int)
    assert info["package"] == info["unit_package"] == os.environ["KB_NEO4J_PACKAGE"]
    assert info["upgrade_pending"] is False
    assert info["kb"]


@pytest.mark.usefixtures("cli_env")
def test_status_flags_a_unit_running_an_older_package(tmp_path: Path, fake_systemctl) -> None:
    write_unit(tmp_path, "/nix/store/0000000000000000000000000000000-neo4j-2025.01.0")
    app = App(runner=RecordingRunner(fake_systemctl("active")), home=tmp_path)
    result = CliRunner().invoke(main, ["status"], obj=app)
    assert result.exit_code == 1
    current = neo4j_version(Path(os.environ["KB_NEO4J_PACKAGE"]))
    assert f"upgrade:      unit runs 2025.01.0, package is {current}: run kb-setup" in result.output

    as_json = CliRunner().invoke(main, ["--json", "status"], obj=app)
    info = json.loads(as_json.output)
    assert info["upgrade_pending"] is True
    assert info["unit_package"].endswith("-neo4j-2025.01.0")
    assert info["package"] == os.environ["KB_NEO4J_PACKAGE"]


@pytest.mark.usefixtures("cli_env")
def test_status_exits_1_when_the_schema_is_incomplete(
    driver: Driver, tmp_path: Path, fake_systemctl
) -> None:
    """A release that adds a constraint leaves the database short of it until kb-setup runs."""
    bootstrap_schema(driver)
    write_unit(tmp_path, os.environ["KB_NEO4J_PACKAGE"])
    app = App(runner=RecordingRunner(fake_systemctl("active")), home=tmp_path)
    assert CliRunner().invoke(main, ["status"], obj=app).exit_code == 0

    driver.execute_query("DROP CONSTRAINT session_id", database_="neo4j")
    try:
        result = CliRunner().invoke(main, ["status"], obj=app)
        assert result.exit_code == 1
        assert "schema:       incomplete, run kb-setup" in result.output
        info = json.loads(CliRunner().invoke(main, ["--json", "status"], obj=app).output)
        assert info["schema_ok"] is False  # the JSON shape is unchanged
        assert info["bolt_reachable"] is True  # what the hook reads, still true
        assert "session_id" not in info["constraints"]
    finally:
        bootstrap_schema(driver)
    assert CliRunner().invoke(main, ["status"], obj=app).exit_code == 0


def test_status_exits_1_when_unit_is_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_systemctl
) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text("[neo4j]\nbolt_port = 1\n")  # nothing listens on port 1
    monkeypatch.setenv("KB_CONFIG", str(cfg))
    app = App(runner=RecordingRunner(fake_systemctl("inactive")), home=tmp_path)
    result = CliRunner().invoke(main, ["status"], obj=app)
    assert result.exit_code == 1
    assert "unit:         inactive" in result.output
    assert "unreachable" in result.output


@pytest.mark.parametrize(("action", "waits"), [("start", 1), ("restart", 1), ("stop", 0)])
def test_service_actions_wait_for_readiness_after_a_start(
    tmp_path: Path, fake_systemctl, action: str, waits: int
) -> None:
    runner = RecordingRunner(fake_systemctl("active"))
    waited: list[tuple[str, ...]] = []
    app = App(runner=runner, home=tmp_path, waiter=lambda: waited.append(runner.calls[-1].argv))
    result = CliRunner().invoke(main, ["service", action], obj=app)
    assert result.exit_code == 0, result.output
    assert runner.calls[0].argv == ("systemctl", "--user", action, UNIT_NAME)
    assert waited == [("systemctl", "--user", action, UNIT_NAME)] * waits
    assert result.output.strip() == "active"


def test_service_start_reports_a_readiness_timeout(tmp_path: Path, fake_systemctl) -> None:
    def never_ready() -> None:
        raise TimeoutError("bolt on 127.0.0.1:1 not reachable after 30s; see: journalctl")

    app = App(
        runner=RecordingRunner(fake_systemctl("activating")), home=tmp_path, waiter=never_ready
    )
    result = CliRunner().invoke(main, ["service", "start"], obj=app)
    assert result.exit_code == 1
    assert (
        result.output.strip()
        == "Error: bolt on 127.0.0.1:1 not reachable after 30s; see: journalctl"
    )


def test_subprocess_failures_become_click_errors(tmp_path: Path, monkeypatch) -> None:
    def no_systemctl(call: Call) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError(2, "No such file or directory", call.argv[0])

    def failing(call: Call) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(list(call.argv), 3, "", "")

    def must_not_wait() -> None:
        raise AssertionError("waiter must not run when the service was never started")

    monkeypatch.setenv("KB_CONFIG", str(tmp_path / "none.toml"))
    missing = CliRunner().invoke(
        main,
        ["service", "stop"],
        obj=App(runner=RecordingRunner(no_systemctl), home=tmp_path, waiter=must_not_wait),
    )
    assert missing.exit_code == 1
    assert missing.output.strip() == "Error: not found: systemctl"

    failed = CliRunner().invoke(
        main,
        ["backup"],
        obj=App(runner=RecordingRunner(failing), home=tmp_path, waiter=must_not_wait),
    )
    assert failed.exit_code == 1
    assert (
        failed.output.strip() == f"Error: systemctl --user stop {UNIT_NAME} failed with exit code 3"
    )


def test_restore_prints_the_safety_dump_once(tmp_path: Path, monkeypatch, fake_systemctl) -> None:
    def admin_and_systemctl(call: Call) -> subprocess.CompletedProcess[str]:
        if "dump" in call.argv:
            flag = next(a for a in call.argv if a.startswith("--to-path="))
            (Path(flag.removeprefix("--to-path=")) / "neo4j.dump").write_bytes(b"dump")
        return fake_systemctl("active")(call)

    monkeypatch.setenv("KB_CONFIG", str(tmp_path / "none.toml"))
    dump = tmp_path / "kb-2026-09-01.dump"
    dump.write_bytes(b"x")
    app = App(runner=RecordingRunner(admin_and_systemctl), home=tmp_path, waiter=lambda: None)
    result = CliRunner().invoke(main, ["restore", str(dump)], obj=app)
    assert result.exit_code == 0, result.output
    safety_lines = [
        line for line in result.stderr.splitlines() if line.startswith("pre-restore dump: ")
    ]
    assert len(safety_lines) == 1
    assert result.stdout.splitlines() == [f"restored {dump}"]
    assert result.output.count("pre-restore dump:") == 1

    as_json = CliRunner().invoke(main, ["--json", "restore", str(dump)], obj=app)
    assert as_json.exit_code == 0, as_json.output
    assert set(json.loads(as_json.stdout)) == {"restored", "pre_restore_dump"}


def test_help_of_every_command() -> None:
    runner = CliRunner()
    for args in (
        ["--help"],
        ["status", "--help"],
        ["config", "--help"],
        ["service", "--help"],
        ["query", "--help"],
        ["backup", "--help"],
        ["restore", "--help"],
    ):
        result = runner.invoke(main, args)
        assert result.exit_code == 0, (args, result.output)
