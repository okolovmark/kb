"""kb-setup: idempotent installer for the neo4j-kb user service, config, auth and schema."""

import os
import secrets
import shutil
import subprocess
from pathlib import Path

import click
from neo4j.exceptions import DriverError, Neo4jError

from kb.config import Config, ConfigError, load, render_default
from kb.db import BOLT_HOST, bootstrap_schema, connect, counter_value, read_auth
from kb.paths import LOG_CONFIGS, UNIT_NAME, Paths, neo4j_package, neo4j_version
from kb.runner import Runner, SubprocessRunner, format_process_error
from kb.service import neo4j_admin, private_mkdir, systemctl, unit_state, wait_until_ready

CONF_HEADER = (
    "# Rendered by kb-setup from config.toml; edits here are overwritten on the next run.\n"
)
RUNNING_STATES = frozenset({"active", "activating"})


def package_conf_dir(pkg: Path) -> Path:
    """``<pkg>/share/neo4j/conf``."""
    return pkg / "share" / "neo4j" / "conf"


def package_default_conf(pkg: Path) -> str:
    """The package's ``neo4j.conf`` text."""
    return (package_conf_dir(pkg) / "neo4j.conf").read_text()


def conf_settings(cfg: Config, paths: Paths, *, auth_enabled: bool = True) -> dict[str, str]:
    """The neo4j.conf keys kb owns: listen addresses, writable directories, memory, auth."""
    neo = cfg.neo4j
    return {
        "server.default_listen_address": BOLT_HOST,
        "server.bolt.listen_address": f"{BOLT_HOST}:{neo.bolt_port}",
        "server.http.listen_address": f"{BOLT_HOST}:{neo.http_port}",
        "server.https.enabled": "false",
        "server.directories.data": str(paths.data_dir),
        "server.directories.logs": str(paths.logs_dir),
        "server.directories.run": str(paths.run_dir),
        "server.directories.import": str(paths.import_dir),
        "server.directories.plugins": str(paths.plugins_dir),
        "server.directories.transaction.logs.root": str(paths.tx_logs_dir),
        "server.memory.heap.initial_size": neo.heap_max,
        "server.memory.heap.max_size": neo.heap_max,
        "server.memory.pagecache.size": neo.pagecache,
        # Neo4j preallocates 258 MB per log: half a gigabyte of empty files beside a 2.5 MB graph
        "db.tx_log.preallocate": "true" if neo.tx_log_preallocate else "false",
        "dbms.security.auth_enabled": "true" if auth_enabled else "false",
        "dbms.usage_report.enabled": "false",
        # LAN UDP broadcast for Neo4j's fleet manager; pointless for a 127.0.0.1-only DB
        "server.fleet_discovery.enabled": "false",
    }


def render_conf(settings: dict[str, str], base_conf: str) -> str:
    """Package settings minus the keys in *settings*, then kb's keys, under a do-not-edit header."""
    # Package neo4j.conf as the version-matched baseline, our keys override; why the
    # writable directories move out of the store: docs/adr/0001.
    inherited = [
        line
        for line in base_conf.splitlines()
        if _is_setting(line) and line.split("=", 1)[0].strip() not in settings
    ]
    ours = [f"{key}={value}" for key, value in settings.items()]
    return CONF_HEADER + "\n".join(inherited) + "\n\n" + "\n".join(ours) + "\n"


def _is_setting(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped) and not stripped.startswith("#") and "=" in stripped


def render_unit(pkg: Path, paths: Paths) -> str:
    """The neo4j-kb.service text for *pkg* and *paths*."""
    return f"""\
[Unit]
Description=Neo4j for kb

[Service]
Type=simple
# Only the conf dir is redirected; NEO4J_HOME stays the store path (docs/adr/0001)
Environment=NEO4J_CONF={paths.conf_dir}
ExecStart={pkg}/bin/neo4j console
Restart=on-failure
RestartSec=5
# a stop during boot ends the bootloader with 143 (SIGTERM); not a failure
SuccessExitStatus=143
# Neo4j warns below 40000 open files
LimitNOFILE=60000
StandardOutput=journal

[Install]
WantedBy=default.target
"""


def write_if_changed(path: Path, text: str) -> bool:
    """Write *text* to *path* unless it already holds it; True when written."""
    if path.exists() and path.read_text() == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return True


def copy_log_configs(pkg: Path, paths: Paths) -> bool:
    """Package server-logs.xml / user-logs.xml into our conf dir; True when any changed."""
    changed = False
    for name in LOG_CONFIGS:
        text = (package_conf_dir(pkg) / name).read_text()
        changed = write_if_changed(paths.conf_dir / name, text) or changed
    return changed


def ensure_auth(paths: Paths) -> tuple[str, bool]:
    """Existing password, or a new one written 0600; returns (password, generated)."""
    existing = read_auth(paths.auth_file)
    if existing:
        return existing[1], False
    # hex: no leading '-' that neo4j-admin would parse as an option
    password = secrets.token_hex(24)
    paths.auth_file.parent.mkdir(parents=True, exist_ok=True)
    # 0600 from the first byte, and O_EXCL so a concurrent run cannot clobber it
    fd = os.open(paths.auth_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(f"neo4j:{password}\n")
    return password, True


def data_is_fresh(paths: Paths) -> bool:
    """set-initial-password only works before the first server start creates databases/."""
    return not (paths.data_dir / "databases").exists()


def make_dirs(paths: Paths) -> None:
    """Create the data root and every neo4j directory; new ones get 0700."""
    for directory in (paths.root, paths.neo4j_root, *paths.neo4j_dirs):
        private_mkdir(directory)


def register_gc_root(runner: Runner, paths: Paths, pkg: Path, nix_store: str | None) -> None:
    """Indirect GC root so a profile upgrade + garbage collection cannot delete the unit's neo4j."""
    if nix_store is None:
        click.echo(
            "warning: nix-store not on PATH; no GC root for the neo4j package, "
            "a nix-collect-garbage after a profile upgrade can remove the running server",
            err=True,
        )
        return
    # nix-store prints the root path on stdout; keep it out of the summary
    runner.run(
        (nix_store, "--add-root", str(paths.pkg_root), "--indirect", "-r", str(pkg)), capture=True
    )


def install(
    cfg: Config,
    paths: Paths,
    pkg: Path,
    *,
    conf: str,
    unit: str,
    runner: Runner,
    nix_store: str | None,
) -> None:
    """Write files, set the initial password when fresh, enable the unit, wait, bootstrap schema."""
    make_dirs(paths)
    conf_changed = write_if_changed(paths.conf_file, conf)
    conf_changed = copy_log_configs(pkg, paths) or conf_changed
    if not paths.config_file.exists():
        write_if_changed(paths.config_file, render_default())

    password, generated = ensure_auth(paths)
    fresh = data_is_fresh(paths)
    if fresh:
        neo4j_admin(runner, pkg, paths, "dbms", "set-initial-password", "--", password)
    elif generated:
        click.echo(
            "warning: the data dir is already initialised, the new neo4j-auth password does "
            "not match the database; reset it in Neo4j Browser or remove the data dir",
            err=True,
        )

    was_running = unit_state(runner) in RUNNING_STATES
    unit_changed = write_if_changed(paths.unit_file, unit)
    register_gc_root(runner, paths, pkg, nix_store)
    systemctl(runner, "daemon-reload")
    systemctl(runner, "enable", "--now", UNIT_NAME)
    if was_running and (conf_changed or unit_changed):
        systemctl(runner, "restart", UNIT_NAME)

    wait_until_ready(cfg, paths.auth_file)
    with connect(cfg, auth_file=paths.auth_file) as driver:
        bootstrap_schema(driver)
        counter = counter_value(driver)

    click.echo(summary(cfg, paths, pkg, unit_state(runner), counter, password_set=fresh))


def summary(
    cfg: Config, paths: Paths, pkg: Path, state: str, counter: int | None, *, password_set: bool
) -> str:
    """The lines kb-setup prints at the end."""
    neo = cfg.neo4j
    return "\n".join(
        [
            f"neo4j:    {neo4j_version(pkg)} ({pkg})",
            f"unit:     {paths.unit_file} [{state}]",
            f"conf:     {paths.conf_file}",
            f"data:     {paths.data_dir}",
            f"config:   {paths.config_file}",
            f"auth:     {paths.auth_file}" + (" (initial password set)" if password_set else ""),
            f"bolt:     {BOLT_HOST}:{neo.bolt_port}   http: {BOLT_HOST}:{neo.http_port}",
            f"counter:  {counter}",
        ]
    )


@click.command(help="Install the neo4j-kb user service, config, auth and schema; safe to re-run.")
@click.option(
    "--dry-run", is_flag=True, help="Print the rendered unit and neo4j.conf, touch nothing."
)
@click.option(
    "--home",
    type=click.Path(path_type=Path),
    default=None,
    help="With --dry-run: render every path under DIR instead of ~.",
)
def main(dry_run: bool, home: Path | None) -> None:
    """``kb-setup`` entry point; --dry-run prints the unit and conf and writes nothing."""
    if home is not None and not dry_run:
        raise click.UsageError("--home is only valid together with --dry-run")
    home = home or Path.home()
    try:
        cfg = load(home=home)
        pkg = neo4j_package()
    except (ConfigError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from exc
    paths = Paths.from_config(cfg, home)
    conf = render_conf(conf_settings(cfg, paths), package_default_conf(pkg))
    unit = render_unit(pkg, paths)
    if dry_run:
        click.echo(f"# {paths.unit_file}\n{unit}")
        click.echo(f"# {paths.conf_file}\n{conf}", nl=False)
        click.echo(f"# copied from {package_conf_dir(pkg)}: {', '.join(LOG_CONFIGS)}")
        return
    try:
        install(
            cfg,
            paths,
            pkg,
            conf=conf,
            unit=unit,
            runner=SubprocessRunner(),
            nix_store=shutil.which("nix-store"),
        )
    except TimeoutError as exc:
        raise click.ClickException(str(exc)) from exc
    except subprocess.CalledProcessError as exc:
        raise click.ClickException(format_process_error(exc)) from exc
    except (Neo4jError, DriverError) as exc:
        raise click.ClickException(f"neo4j: {exc}") from exc
    except FileNotFoundError as exc:
        raise click.ClickException(f"not found: {exc.filename or exc}") from exc
    except (ValueError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc
