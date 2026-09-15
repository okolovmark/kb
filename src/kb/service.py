"""systemd unit control and neo4j-admin backup/restore, both through the Runner seam."""

import contextlib
import datetime as dt
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

from kb.config import Config
from kb.db import BOLT_HOST, connect, wait_for_bolt, wait_for_database
from kb.paths import UNIT_NAME, Paths
from kb.runner import Completed, Runner, format_process_error

PRIVATE_DIR = 0o700
JOURNAL_HINT = f"see: journalctl --user -u {UNIT_NAME} -n 100"

Waiter = Callable[[], None]


class RestoreError(Exception):
    """A failed load whose rollback to the pre-restore dump failed as well."""


def systemctl(runner: Runner, *args: str, check: bool = True, capture: bool = False) -> Completed:
    """``systemctl --user <args>`` through the runner."""
    return runner.run(("systemctl", "--user", *args), check=check, capture=capture)


def unit_state(runner: Runner) -> str:
    """``systemctl is-active`` output (active, inactive, failed, ...); ``unknown`` when empty."""
    result = systemctl(runner, "is-active", UNIT_NAME, check=False, capture=True)
    return (result.stdout or "").strip() or "unknown"


def unit_exec_package(unit_file: Path) -> Path | None:
    """The neo4j store path the installed unit runs (``ExecStart=<pkg>/bin/neo4j console``)."""
    if not unit_file.exists():
        return None
    for line in unit_file.read_text().splitlines():
        if line.startswith("ExecStart="):
            return Path(line.removeprefix("ExecStart=").split()[0]).parent.parent
    return None


def wait_until_ready(cfg: Config, auth_file: Path) -> None:
    """Block until bolt accepts and the neo4j database answers.

    systemctl start returns when the JVM is forked, seconds before Neo4j listens.
    """
    timeout = cfg.neo4j.start_timeout
    try:
        wait_for_bolt(BOLT_HOST, cfg.neo4j.bolt_port, timeout)
        with connect(cfg, auth_file=auth_file) as driver:
            wait_for_database(driver, timeout)
    except TimeoutError as exc:
        raise TimeoutError(f"{exc}; {JOURNAL_HINT}") from exc


def start_and_wait(runner: Runner, wait: Waiter) -> None:
    """``systemctl start`` followed by the readiness waiter."""
    systemctl(runner, "start", UNIT_NAME)
    wait()


def journalctl(runner: Runner, lines: int, follow: bool) -> Completed:
    """``journalctl --user -u <unit> -n <lines>``, ``--follow`` on request; rc unchecked."""
    argv = ["journalctl", "--user", "-u", UNIT_NAME, "-n", str(lines), "--no-pager"]
    if follow:
        argv.append("--follow")
    return runner.run(argv, check=False)


def neo4j_admin(
    runner: Runner, pkg: Path, paths: Paths, *args: str, stdin_file: Path | None = None
) -> Completed:
    """Run ``neo4j-admin <args>`` from *pkg* against kb's conf dir, output captured."""
    # NEO4J_CONF makes neo4j-admin read our neo4j.conf, hence our data dir. Output is
    # captured: hundreds of progress lines on success, the tail on failure via
    # format_process_error.
    return runner.run(
        (str(pkg / "bin" / "neo4j-admin"), *args),
        env={"NEO4J_CONF": str(paths.conf_dir)},
        stdin_file=stdin_file,
        capture=True,
    )


def private_mkdir(path: Path) -> None:
    """mkdir -p; a newly created leaf gets 0700 (personal notes live under it)."""
    if path.is_dir():
        return
    path.mkdir(parents=True, exist_ok=True)
    # FUSE/exFAT mounts (a synced Drive folder) refuse chmod; the directory still works
    with contextlib.suppress(OSError):
        path.chmod(PRIVATE_DIR)


def backup_target(backup_dir: Path, today: dt.date, prefix: str = "kb") -> Path:
    """``<dir>/<prefix>-<date>.dump``, or ``-2``, ``-3``, ... when that name is taken."""
    base = f"{prefix}-{today.isoformat()}"
    target = backup_dir / f"{base}.dump"
    n = 2
    while target.exists():
        target = backup_dir / f"{base}-{n}.dump"
        n += 1
    return target


def dump(runner: Runner, pkg: Path, paths: Paths, target: Path) -> Path:
    """``neo4j-admin database dump`` into a fresh temp dir, moved to target; service stopped."""
    # neo4j-admin always writes <database>.dump; dumping straight into backup.dir would
    # clobber a user's neo4j.dump lying there (the file a hand-run dump produces)
    tmp = Path(tempfile.mkdtemp(dir=target.parent, prefix=".dump-"))
    try:
        neo4j_admin(runner, pkg, paths, "database", "dump", "neo4j", f"--to-path={tmp}")
        (tmp / "neo4j.dump").replace(target)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return target


def load(runner: Runner, pkg: Path, paths: Paths, dump_file: Path) -> None:
    """``neo4j-admin database load`` from any file name via stdin (service stopped)."""
    neo4j_admin(
        runner,
        pkg,
        paths,
        "database",
        "load",
        "neo4j",
        "--from-stdin",
        "--overwrite-destination=true",
        stdin_file=dump_file,
    )


def backup(runner: Runner, pkg: Path, paths: Paths, today: dt.date, *, wait: Waiter) -> Path:
    """stop -> dump -> start and wait for readiness; started again even when the dump fails."""
    private_mkdir(paths.backup_dir)
    target = backup_target(paths.backup_dir, today)
    systemctl(runner, "stop", UNIT_NAME)
    try:
        dump(runner, pkg, paths, target)
    finally:
        start_and_wait(runner, wait)
    return target


def restore(
    runner: Runner,
    pkg: Path,
    paths: Paths,
    dump_file: Path,
    *,
    today: dt.date,
    wait: Waiter,
    report: Callable[[str], None] = lambda _: None,
) -> Path:
    """stop -> safety dump -> load -> start and wait; a failed load reloads the safety dump.

    Returns the safety dump path (``<backup.dir>/pre-restore-<date>[-n].dump``).
    """
    if not dump_file.is_file():
        raise FileNotFoundError(dump_file)
    private_mkdir(paths.backup_dir)
    safety = backup_target(paths.backup_dir, today, prefix="pre-restore")
    systemctl(runner, "stop", UNIT_NAME)
    try:
        dump(runner, pkg, paths, safety)
        report(f"pre-restore dump: {safety}")
        try:
            load(runner, pkg, paths, dump_file)
        except subprocess.CalledProcessError:
            # load --overwrite-destination empties the store before it reads the archive
            try:
                load(runner, pkg, paths, safety)
            except subprocess.CalledProcessError as rollback:
                raise RestoreError(
                    f"load of {dump_file} failed and the pre-restore dump {safety} could not "
                    f"be loaded back; the database is empty, load {safety} by hand: "
                    f"kb restore {safety}\n{format_process_error(rollback)}"
                ) from rollback
            raise
    finally:
        start_and_wait(runner, wait)
    return safety
