import os
import re
import stat
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner
from neo4j.exceptions import Neo4jError, ServiceUnavailable

from kb.config import Config, load
from kb.db import connect, schema_present
from kb.paths import LOG_CONFIGS, UNIT_NAME, Paths
from kb.runner import RecordingRunner
from kb.setup import (
    conf_settings,
    ensure_auth,
    install,
    main,
    package_conf_dir,
    package_default_conf,
    render_conf,
    render_unit,
    write_if_changed,
)

DIRECTORY_KEYS = ("data", "logs", "run", "import", "plugins", "transaction.logs.root")


def test_dry_run_renders_unit_and_conf_without_touching_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KB_CONFIG", raising=False)
    pkg = os.environ["KB_NEO4J_PACKAGE"]
    result = CliRunner().invoke(main, ["--dry-run", "--home", str(tmp_path)])
    assert result.exit_code == 0, result.output
    out = result.output
    root = tmp_path / ".local" / "share" / "kb" / "neo4j"

    assert f"ExecStart={pkg}/bin/neo4j console" in out
    assert f"Environment=NEO4J_CONF={root / 'conf'}" in out
    assert "Description=Neo4j for kb" in out
    assert "Restart=on-failure" in out
    assert "SuccessExitStatus=143" in out
    assert "StandardOutput=journal\n" in out
    assert "After=network.target" not in out
    assert "WantedBy=default.target" in out

    for key in DIRECTORY_KEYS:
        prefix = f"server.directories.{key}="
        line = next(c for c in out.splitlines() if c.startswith(prefix))
        assert line.split("=", 1)[1].startswith(str(root)), line
    assert "server.bolt.listen_address=127.0.0.1:7687" in out
    assert "server.http.listen_address=127.0.0.1:7474" in out
    assert "dbms.security.auth_enabled=true" in out
    assert "server.memory.heap.max_size=512m" in out
    assert "server.memory.pagecache.size=128m" in out
    assert "server.directories.import=import" not in out
    assert (
        f"server.fleet_discovery.enabled=false\n# copied from {pkg}/share/neo4j/conf: "
        "server-logs.xml, user-logs.xml\n"
    ) in out

    assert list(tmp_path.iterdir()) == []


def test_home_requires_dry_run(tmp_path: Path) -> None:
    result = CliRunner().invoke(main, ["--home", str(tmp_path)])
    assert result.exit_code == 2
    assert "--home is only valid together with --dry-run" in result.output
    assert list(tmp_path.iterdir()) == []


def test_render_conf_inherits_package_defaults_minus_overrides(tmp_path: Path) -> None:
    cfg = load(path=tmp_path / "none", home=tmp_path)
    paths = Paths.from_config(cfg, tmp_path)
    base = (
        "# comment\n"
        "server.directories.import=import\n"
        "server.jvm.additional=-XX:+UseG1GC\n"
        "server.jvm.additional=-XX:-OmitStackTraceInFastThrow\n"
        "\n"
        "#dbms.security.auth_enabled=false\n"
    )
    text = render_conf(conf_settings(cfg, paths, auth_enabled=False), base)
    lines = text.splitlines()
    assert "server.jvm.additional=-XX:+UseG1GC" in lines
    assert "server.jvm.additional=-XX:-OmitStackTraceInFastThrow" in lines
    assert "server.directories.import=import" not in lines
    assert f"server.directories.import={paths.import_dir}" in lines
    assert "dbms.security.auth_enabled=false" in lines
    assert lines.count("dbms.security.auth_enabled=false") == 1
    assert "server.fleet_discovery.enabled=false" in lines


def test_tx_log_preallocate_follows_the_config(tmp_path: Path) -> None:
    cfg = load(path=tmp_path / "none", home=tmp_path)
    paths = Paths.from_config(cfg, tmp_path)
    assert conf_settings(cfg, paths)["db.tx_log.preallocate"] == "true"  # Neo4j's own default

    config_file = tmp_path / "config.toml"
    config_file.write_text("[neo4j]\ntx_log_preallocate = false\n")
    off = load(path=config_file, home=tmp_path)
    assert off.neo4j.tx_log_preallocate is False
    assert conf_settings(off, paths)["db.tx_log.preallocate"] == "false"
    assert "db.tx_log.preallocate=false" in render_conf(conf_settings(off, paths), "").splitlines()


def test_write_if_changed(tmp_path: Path) -> None:
    target = tmp_path / "sub" / "file.txt"
    assert write_if_changed(target, "one\n") is True
    assert write_if_changed(target, "one\n") is False
    assert write_if_changed(target, "two\n") is True
    assert target.read_text() == "two\n"


def test_ensure_auth_generates_once_with_a_safe_alphabet(tmp_path: Path) -> None:
    paths = Paths.from_config(load(path=tmp_path / "none", home=tmp_path), tmp_path)
    password, generated = ensure_auth(paths)
    assert generated is True
    assert re.fullmatch(r"[0-9a-f]{48}", password), password
    assert not password.startswith("-")
    assert stat.S_IMODE(paths.auth_file.stat().st_mode) == 0o600
    assert paths.auth_file.read_text() == f"neo4j:{password}\n"
    assert ensure_auth(paths) == (password, False)


# --- install() against the fixture DB, systemctl / neo4j-admin / nix-store faked ---------


@pytest.fixture
def isolated(neo4j_server, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A config under tmp_path carrying the fixture's ports; never the fixture's own paths."""
    monkeypatch.delenv("KB_NEO4J_AUTH", raising=False)
    monkeypatch.delenv("KB_CONFIG", raising=False)
    cfg_file = tmp_path / ".config" / "kb" / "config.toml"
    cfg_file.parent.mkdir(parents=True)
    cfg_file.write_text(
        f"[neo4j]\nbolt_port = {neo4j_server.bolt_port}\nhttp_port = {neo4j_server.http_port}\n"
    )
    cfg = load(path=cfg_file, home=tmp_path)
    paths = Paths.from_config(cfg, tmp_path)
    pkg = Path(os.environ["KB_NEO4J_PACKAGE"])
    return cfg, paths, pkg


def _render(cfg: Config, paths: Paths, pkg: Path) -> tuple[str, str]:
    return render_conf(conf_settings(cfg, paths), package_default_conf(pkg)), render_unit(
        pkg, paths
    )


def test_install_fresh(isolated, fake_systemctl, capsys) -> None:
    cfg, paths, pkg = isolated
    conf, unit = _render(cfg, paths, pkg)
    runner = RecordingRunner(fake_systemctl("inactive"))
    install(cfg, paths, pkg, conf=conf, unit=unit, runner=runner, nix_store="nix-store")

    password = paths.auth_file.read_text().strip().removeprefix("neo4j:")
    admin = str(pkg / "bin" / "neo4j-admin")
    assert [c.argv for c in runner.calls] == [
        (admin, "dbms", "set-initial-password", "--", password),
        ("systemctl", "--user", "is-active", UNIT_NAME),
        ("nix-store", "--add-root", str(paths.pkg_root), "--indirect", "-r", str(pkg)),
        ("systemctl", "--user", "daemon-reload"),
        ("systemctl", "--user", "enable", "--now", UNIT_NAME),
        ("systemctl", "--user", "is-active", UNIT_NAME),
    ]
    assert runner.calls[0].env == {"NEO4J_CONF": str(paths.conf_dir)}

    assert stat.S_IMODE(paths.auth_file.stat().st_mode) == 0o600
    for directory in (paths.root, paths.neo4j_root, paths.conf_dir, paths.data_dir):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700, directory
    assert paths.conf_file.read_text() == conf
    assert paths.unit_file.read_text() == unit
    assert paths.config_file.exists()
    for name in LOG_CONFIGS:
        assert (paths.conf_dir / name).read_text() == (package_conf_dir(pkg) / name).read_text()

    out = capsys.readouterr()
    assert "(initial password set)" in out.out
    assert re.search(r"^counter:\s+\d+$", out.out, re.MULTILINE), out.out
    assert out.err == ""
    with connect(cfg, auth_file=paths.auth_file) as drv:
        assert schema_present(drv)


def test_install_rerun_restarts_only_on_change(isolated, fake_systemctl, capsys) -> None:
    cfg, paths, pkg = isolated
    conf, unit = _render(cfg, paths, pkg)
    install(cfg, paths, pkg, conf=conf, unit=unit, runner=RecordingRunner(), nix_store="nix-store")
    (paths.data_dir / "databases").mkdir()  # the server has started once
    capsys.readouterr()

    unchanged = RecordingRunner(fake_systemctl("active"))
    install(cfg, paths, pkg, conf=conf, unit=unit, runner=unchanged, nix_store="nix-store")
    argv = [c.argv for c in unchanged.calls]
    assert not any("set-initial-password" in a for a in argv)
    assert ("systemctl", "--user", "restart", UNIT_NAME) not in argv
    assert capsys.readouterr().err == ""

    changed = RecordingRunner(fake_systemctl("active"))
    install(
        cfg, paths, pkg, conf=conf + "# edited\n", unit=unit, runner=changed, nix_store="nix-store"
    )
    argv = [c.argv for c in changed.calls]
    assert ("systemctl", "--user", "restart", UNIT_NAME) in argv
    assert argv.index(("systemctl", "--user", "restart", UNIT_NAME)) > argv.index(
        ("systemctl", "--user", "enable", "--now", UNIT_NAME)
    )

    activating = RecordingRunner(fake_systemctl("activating"))
    install(
        cfg,
        paths,
        pkg,
        conf=conf + "# edited twice\n",
        unit=unit,
        runner=activating,
        nix_store="nix-store",
    )
    assert ("systemctl", "--user", "restart", UNIT_NAME) in [c.argv for c in activating.calls]


def test_install_not_fresh_with_new_password_warns(isolated, fake_systemctl, capsys) -> None:
    cfg, paths, pkg = isolated
    conf, unit = _render(cfg, paths, pkg)
    (paths.data_dir / "databases").mkdir(parents=True)
    runner = RecordingRunner(fake_systemctl("active"))
    install(cfg, paths, pkg, conf=conf, unit=unit, runner=runner, nix_store="nix-store")
    assert not any("set-initial-password" in c.argv for c in runner.calls)
    out = capsys.readouterr()
    assert "warning: the data dir is already initialised" in out.err
    assert "(initial password set)" not in out.out


def test_install_without_nix_store_warns_and_skips_gc_root(isolated, fake_systemctl, capsys):
    cfg, paths, pkg = isolated
    conf, unit = _render(cfg, paths, pkg)
    runner = RecordingRunner(fake_systemctl("inactive"))
    install(cfg, paths, pkg, conf=conf, unit=unit, runner=runner, nix_store=None)
    assert not any(c.argv[0] == "nix-store" for c in runner.calls)
    assert "warning: nix-store not on PATH" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (ServiceUnavailable("db down"), "Error: neo4j: db down"),
        (Neo4jError("boom"), "Error: neo4j: boom"),
        (
            ValueError("/x/neo4j-auth: expected one line 'neo4j:<password>'"),
            "Error: /x/neo4j-auth: expected one line 'neo4j:<password>'",
        ),
        (
            FileNotFoundError(2, "No such file or directory", "systemctl"),
            "Error: not found: systemctl",
        ),
        (
            PermissionError(1, "Operation not permitted", "/mnt/drive/kb"),
            "Error: [Errno 1] Operation not permitted: '/mnt/drive/kb'",
        ),
        (
            TimeoutError(f"bolt not reachable after 30s; see: journalctl -u {UNIT_NAME}"),
            f"Error: bolt not reachable after 30s; see: journalctl -u {UNIT_NAME}",
        ),
        (
            subprocess.CalledProcessError(64, ["neo4j-admin", "dbms", "set-initial-password"]),
            "Error: neo4j-admin dbms set-initial-password failed with exit code 64",
        ),
        (
            subprocess.CalledProcessError(
                1, ["neo4j-admin", "database", "dump"], "Files: 1/2\nFiles: 2/2\n", "ERROR boom\n"
            ),
            "Error: neo4j-admin database dump failed with exit code 1:\n"
            "Files: 1/2\nFiles: 2/2\nERROR boom",
        ),
    ],
)
def test_main_maps_install_errors_to_click_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exc: Exception, expected: str
) -> None:
    monkeypatch.setenv("KB_CONFIG", str(tmp_path / "none.toml"))

    def boom(*args, **kwargs):
        raise exc

    monkeypatch.setattr("kb.setup.install", boom)
    result = CliRunner().invoke(main, [])
    assert result.exit_code == 1
    assert result.output.strip() == expected


def test_install_propagates_a_failed_neo4j_admin_call(isolated, fake_systemctl) -> None:
    cfg, paths, pkg = isolated
    conf, unit = _render(cfg, paths, pkg)

    def failing(call):
        rc = 1 if "set-initial-password" in call.argv else 0
        return subprocess.CompletedProcess(list(call.argv), rc, "", "")

    with pytest.raises(subprocess.CalledProcessError):
        install(
            cfg, paths, pkg, conf=conf, unit=unit, runner=RecordingRunner(failing), nix_store=None
        )
