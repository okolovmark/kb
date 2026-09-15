import datetime as dt
import subprocess
from pathlib import Path

import pytest

from kb.config import load
from kb.paths import UNIT_NAME, Paths
from kb.runner import Call, RecordingRunner, format_process_error
from kb.service import RestoreError, backup, restore, unit_exec_package

TODAY = dt.date(2026, 9, 11)
PKG = Path("/nix/store/fake-neo4j-1.0.0")
ADMIN = str(PKG / "bin" / "neo4j-admin")
STOP = ("systemctl", "--user", "stop", UNIT_NAME)
START = ("systemctl", "--user", "start", UNIT_NAME)
LOAD = (ADMIN, "database", "load", "neo4j", "--from-stdin", "--overwrite-destination=true")


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    return Paths.from_config(load(path=tmp_path / "none", home=tmp_path), tmp_path)


class WaitRecorder:
    """Fake readiness waiter: counts calls and notes which runner call preceded each."""

    def __init__(self, runner: RecordingRunner) -> None:
        self.runner = runner
        self.after: list[tuple[str, ...]] = []

    def __call__(self) -> None:
        self.after.append(self.runner.calls[-1].argv)


def waited_once_after_start(waiter: WaitRecorder) -> bool:
    return waiter.after == [START]


def to_path(call: Call) -> Path:
    flag = next(a for a in call.argv if a.startswith("--to-path="))
    return Path(flag.removeprefix("--to-path="))


def fake_admin(*, fail_dump: bool = False, fail_loading: set[Path] | None = None):
    """Fake neo4j-admin: a dump writes neo4j.dump into --to-path; loads of *fail_loading* fail."""

    def handler(call: Call) -> subprocess.CompletedProcess[str]:
        rc = 0
        if "dump" in call.argv:
            if fail_dump:
                rc = 1
            else:
                (to_path(call) / "neo4j.dump").write_bytes(b"dump")
        if "load" in call.argv and call.stdin_file in (fail_loading or set()):
            rc = 1
        return subprocess.CompletedProcess(list(call.argv), rc, "", "")

    return handler


def assert_dump_call(call: Call, paths: Paths) -> None:
    assert call.argv[:4] == (ADMIN, "database", "dump", "neo4j")
    assert len(call.argv) == 5
    tmp = to_path(call)
    assert tmp.parent == paths.backup_dir and tmp.name.startswith(".dump-")
    assert not tmp.exists(), "temp dump dir must be removed"
    assert call.env == {"NEO4J_CONF": str(paths.conf_dir)}
    assert call.capture is True


def names(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir())


def test_backup_stops_dumps_starts_waits_and_dates_the_file(paths: Paths) -> None:
    runner = RecordingRunner(fake_admin())
    waiter = WaitRecorder(runner)
    target = backup(runner, PKG, paths, TODAY, wait=waiter)

    assert target == paths.backup_dir / "kb-2026-09-11.dump"
    assert target.read_bytes() == b"dump"
    assert len(runner.calls) == 3
    assert (runner.calls[0].argv, runner.calls[2].argv) == (STOP, START)
    assert (runner.calls[0].capture, runner.calls[2].capture) == (False, False)
    assert_dump_call(runner.calls[1], paths)
    assert waited_once_after_start(waiter)
    assert names(paths.backup_dir) == ["kb-2026-09-11.dump"]


def test_backup_leaves_a_user_neo4j_dump_untouched(paths: Paths) -> None:
    paths.backup_dir.mkdir(parents=True)
    user_file = paths.backup_dir / "neo4j.dump"
    user_file.write_bytes(b"mine")
    backup(RecordingRunner(fake_admin()), PKG, paths, TODAY, wait=lambda: None)
    assert user_file.read_bytes() == b"mine"
    assert names(paths.backup_dir) == ["kb-2026-09-11.dump", "neo4j.dump"]


def test_second_backup_on_the_same_day_gets_a_suffix(paths: Paths) -> None:
    runner = RecordingRunner(fake_admin())
    assert backup(runner, PKG, paths, TODAY, wait=lambda: None).name == "kb-2026-09-11.dump"
    assert backup(runner, PKG, paths, TODAY, wait=lambda: None).name == "kb-2026-09-11-2.dump"
    assert backup(runner, PKG, paths, TODAY, wait=lambda: None).name == "kb-2026-09-11-3.dump"


def test_backup_restarts_and_waits_when_the_dump_fails(paths: Paths) -> None:
    runner = RecordingRunner(fake_admin(fail_dump=True))
    waiter = WaitRecorder(runner)
    with pytest.raises(subprocess.CalledProcessError):
        backup(runner, PKG, paths, TODAY, wait=waiter)
    assert len(runner.calls) == 3
    assert (runner.calls[0].argv, runner.calls[2].argv) == (STOP, START)
    assert_dump_call(runner.calls[1], paths)
    assert waited_once_after_start(waiter)
    assert names(paths.backup_dir) == []


def test_failed_neo4j_admin_error_carries_the_output_tail(paths: Paths) -> None:
    def noisy_failure(call: Call) -> subprocess.CompletedProcess[str]:
        if "dump" in call.argv:
            progress = "".join(f"Files: {i}/40, data: {i * 2.5:.1f}%\n" for i in range(1, 41))
            return subprocess.CompletedProcess(list(call.argv), 1, progress, "ERROR: disk full\n")
        return subprocess.CompletedProcess(list(call.argv), 0, "", "")

    with pytest.raises(subprocess.CalledProcessError) as info:
        backup(RecordingRunner(noisy_failure), PKG, paths, TODAY, wait=lambda: None)
    text = format_process_error(info.value)
    lines = text.splitlines()
    assert lines[0].startswith(f"{ADMIN} database dump neo4j --to-path=")
    assert lines[0].endswith(" failed with exit code 1:")
    assert lines[-1] == "ERROR: disk full"
    assert len(lines) == 21
    assert "Files: 21/40" not in text and "Files: 22/40" in text


def test_restore_takes_a_safety_dump_then_loads(paths: Paths, tmp_path: Path) -> None:
    dump = tmp_path / "kb-2026-09-01.dump"
    dump.write_bytes(b"x")
    runner = RecordingRunner(fake_admin())
    waiter = WaitRecorder(runner)
    reported: list[str] = []
    safety = restore(runner, PKG, paths, dump, today=TODAY, wait=waiter, report=reported.append)

    assert waited_once_after_start(waiter)
    assert safety == paths.backup_dir / "pre-restore-2026-09-11.dump"
    assert safety.read_bytes() == b"dump"
    assert reported == [f"pre-restore dump: {safety}"]
    assert len(runner.calls) == 4
    assert (runner.calls[0].argv, runner.calls[2].argv, runner.calls[3].argv) == (STOP, LOAD, START)
    assert_dump_call(runner.calls[1], paths)
    assert runner.calls[2].stdin_file == dump
    assert runner.calls[2].env == {"NEO4J_CONF": str(paths.conf_dir)}


def test_restore_of_a_neo4j_dump_in_backup_dir_keeps_the_file(paths: Paths) -> None:
    paths.backup_dir.mkdir(parents=True)
    user_file = paths.backup_dir / "neo4j.dump"
    user_file.write_bytes(b"mine")
    runner = RecordingRunner(fake_admin())
    safety = restore(runner, PKG, paths, user_file, today=TODAY, wait=lambda: None)
    assert user_file.read_bytes() == b"mine"
    assert runner.calls[2].stdin_file == user_file
    assert safety.read_bytes() == b"dump"
    assert names(paths.backup_dir) == ["neo4j.dump", "pre-restore-2026-09-11.dump"]


def test_restore_reloads_the_safety_dump_when_the_load_fails(paths: Paths, tmp_path: Path) -> None:
    junk = tmp_path / "junk.dump"
    junk.write_bytes(b"not a dump")
    runner = RecordingRunner(fake_admin(fail_loading={junk}))
    waiter = WaitRecorder(runner)
    with pytest.raises(subprocess.CalledProcessError):
        restore(runner, PKG, paths, junk, today=TODAY, wait=waiter)

    safety = paths.backup_dir / "pre-restore-2026-09-11.dump"
    assert safety.exists()
    assert waited_once_after_start(waiter)
    assert [c.argv for c in runner.calls[2:]] == [LOAD, LOAD, START]
    assert all(c.capture for c in runner.calls[1:4])
    assert runner.calls[2].stdin_file == junk
    assert runner.calls[3].stdin_file == safety


def test_restore_reports_a_failed_rollback(paths: Paths, tmp_path: Path) -> None:
    junk = tmp_path / "junk.dump"
    junk.write_bytes(b"not a dump")
    safety = paths.backup_dir / "pre-restore-2026-09-11.dump"
    runner = RecordingRunner(fake_admin(fail_loading={junk, safety}))
    with pytest.raises(RestoreError) as info:
        restore(runner, PKG, paths, junk, today=TODAY, wait=lambda: None)

    message = str(info.value)
    assert f"load of {junk} failed" in message
    assert f"pre-restore dump {safety} could not be loaded back" in message
    assert f"kb restore {safety}" in message
    assert isinstance(info.value.__cause__, subprocess.CalledProcessError)
    assert [c.argv for c in runner.calls[2:]] == [LOAD, LOAD, START]
    assert safety.exists()


def test_restore_does_not_load_when_the_safety_dump_fails(paths: Paths, tmp_path: Path) -> None:
    dump = tmp_path / "kb-2026-09-01.dump"
    dump.write_bytes(b"x")
    runner = RecordingRunner(fake_admin(fail_dump=True))
    waiter = WaitRecorder(runner)
    with pytest.raises(subprocess.CalledProcessError):
        restore(runner, PKG, paths, dump, today=TODAY, wait=waiter)
    assert len(runner.calls) == 3
    assert (runner.calls[0].argv, runner.calls[2].argv) == (STOP, START)
    assert_dump_call(runner.calls[1], paths)
    assert waited_once_after_start(waiter)
    assert not any("load" in c.argv for c in runner.calls)


def test_restore_refuses_a_missing_file(paths: Paths, tmp_path: Path) -> None:
    runner = RecordingRunner()
    with pytest.raises(FileNotFoundError):
        restore(runner, PKG, paths, tmp_path / "nope.dump", today=TODAY, wait=lambda: None)
    assert runner.calls == []


def test_unit_exec_package(tmp_path: Path) -> None:
    unit = tmp_path / "neo4j-kb.service"
    assert unit_exec_package(unit) is None
    unit.write_text("[Service]\nExecStart=/nix/store/abc-neo4j-2.0.0/bin/neo4j console\n")
    assert unit_exec_package(unit) == Path("/nix/store/abc-neo4j-2.0.0")
    unit.write_text("[Service]\nType=simple\n")
    assert unit_exec_package(unit) is None
