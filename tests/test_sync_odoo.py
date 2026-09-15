import dataclasses
import json
import os
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from kb import sync_odoo
from kb.cli import main
from kb.config import load

UID = 7


@dataclass
class FakeOdoo:
    """A JSON-RPC server answering common.login and project.task search_read."""

    tasks: list[dict[str, Any]]
    password: str = "secret"
    calls: list[dict[str, Any]] = field(default_factory=list)
    url: str = ""
    users: dict[str, int] = field(default_factory=lambda: {"dev": 42})


def fake_server(state: FakeOdoo) -> Iterator[FakeOdoo]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers["Content-Length"])
            request = json.loads(self.rfile.read(length))
            params = request["params"]
            state.calls.append(params)
            result: Any
            error: dict[str, Any] | None = None
            if self.path != "/jsonrpc":
                self.send_error(404)
                return
            if params["service"] == "common" and params["method"] == "login":
                _db, user, pwd = params["args"]
                result = UID if (user, pwd) == ("mark", state.password) else False
            elif params["service"] == "object" and params["method"] == "execute_kw":
                _db, uid, pwd, model, method, args, kwargs = params["args"]
                if pwd != state.password or uid != UID:
                    error = {"message": "Odoo Server Error", "data": {"message": "Access Denied"}}
                    result = None
                elif model == "res.users":
                    assert method == "search_read" and kwargs == {"fields": ["id"], "limit": 1}
                    login = args[0][0][2]
                    result = [{"id": state.users[login]}] if login in state.users else []
                else:
                    assert (model, method) == ("project.task", "search_read")
                    result = [{k: t.get(k, False) for k in kwargs["fields"]} for t in state.tasks]
            else:
                error = {"message": "unknown method"}
                result = None
            body = {"jsonrpc": "2.0", "id": request["id"]}
            body["error" if error else "result"] = error or result
            payload = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()


TASKS = [
    {
        "id": 101,
        "key": "KIO-1700",
        "name": "First task",
        "stage_id": [3, "In Progress"],
        "date_assign": "2026-08-31 20:00:00",  # 04:00 on 2026-09-01 in Manila
        "date_deadline": "2026-09-20",
        "create_date": "2026-08-30 10:00:00",
    },
    {
        "id": 102,
        "key": "KIO-1701",
        "name": "Second task",
        "stage_id": [2, "To Do"],
        "date_assign": False,
        "date_deadline": False,
        "create_date": "2026-09-05 10:00:00",
    },
    {
        "id": 103,
        "key": False,
        "name": "Keyless task",
        "stage_id": [2, "To Do"],
        "date_assign": "2026-09-08 01:00:00",
        "date_deadline": False,
        "create_date": "2026-09-08 01:00:00",
    },
]


@pytest.fixture
def odoo() -> Iterator[FakeOdoo]:
    yield from fake_server(FakeOdoo(tasks=[dict(t) for t in TASKS]))


def configure(
    kb_env,
    url: str,
    *,
    enabled: bool = True,
    password: str | None = "secret",
    assignee: str = "",
) -> Path:
    auth = kb_env.home / "odoo-auth"
    if password is not None:
        auth.write_text(password + "\n")
        auth.chmod(0o600)
    base = kb_env.config_file.read_text().split("[sources.odoo]")[0]
    kb_env.config_file.write_text(
        base
        + "[sources.odoo]\n"
        + f"enabled = {'true' if enabled else 'false'}\n"
        + f'url = "{url}"\ndatabase = "prod"\nusername = "mark"\nassignee_login = "{assignee}"\n'
        + f'password_file = "{auth}"\nproject_id = 5\nscope = "proj"\ntimeout = 3\n'
    )
    return auth


def meta(kb_env) -> dict[str, Any] | None:
    rows, _, _ = kb_env.driver.execute_query(
        "MATCH (m:Meta {name: 'odoo_sync'}) RETURN m", database_="neo4j"
    )
    return dict(rows[0]["m"]) if rows else None


def odoo_tasks(kb_env) -> dict[str, dict[str, Any]]:
    rows, _, _ = kb_env.driver.execute_query(
        "MATCH (t:Record:Task {source: 'odoo'}) "
        "OPTIONAL MATCH (e:Event)-[:ON]->(t) WITH t, e ORDER BY e.at "
        "RETURN t, collect(e.kind) AS kinds, collect(e.note) AS notes",
        database_="neo4j",
    )
    return {
        r["t"]["external_id"]: {**dict(r["t"]), "kinds": r["kinds"], "notes": r["notes"]}
        for r in rows
    }


def assert_first_run(odoo: FakeOdoo, first) -> None:
    assert first.exit_code == 0, first.output
    assert first.output.strip() == "odoo: 3 open tasks, 3 new, 0 updated, 0 closed"
    login, search = odoo.calls
    assert login["args"] == ["prod", "mark", "secret"]
    assert search["args"][3:5] == ["project.task", "search_read"]
    assert search["args"][5] == [
        [["user_ids", "in", [UID]], ["is_closed", "=", False], ["project_id", "=", 5]]
    ]
    assert search["args"][6] == {"fields": sync_odoo.TASK_FIELDS}


def test_first_run_creates_then_second_run_closes_and_updates(kb_env, odoo: FakeOdoo) -> None:
    configure(kb_env, odoo.url)
    runner = CliRunner()
    assert_first_run(odoo, runner.invoke(main, ["sync", "odoo"]))
    tasks = odoo_tasks(kb_env)
    assert sorted(tasks) == ["103", "KIO-1700", "KIO-1701"]
    first_task = tasks["KIO-1700"]
    assert first_task["title"] == "KIO-1700 First task"
    assert first_task["scope"] == "proj"
    assert first_task["sched"] == "soft"
    assert str(first_task["due"]) == "2026-09-20"
    assert str(first_task["anchor"]) == "2026-09-01"  # UTC 20:00 the day before, Manila date
    assert first_task["days"] == "work"
    assert first_task["body"] == f"{odoo.url}/web#id=101&model=project.task&view_type=form"
    assert first_task["stage"] == "In Progress"
    assert first_task["synced_at"] is not None
    assert first_task["kinds"] == ["created"]
    second_task = tasks["KIO-1701"]
    assert second_task["sched"] == "age"
    assert "due" not in second_task
    assert str(second_task["anchor"]) == "2026-09-05"  # create_date fallback
    assert tasks["103"]["title"] == "103 Keyless task"
    state = meta(kb_env)
    assert state["ok"] is True and state["count"] == 3 and state.get("error") is None

    # KIO-1701 closed in Odoo, KIO-1700's deadline moved, the keyless one unchanged
    odoo.tasks = [dict(TASKS[0], date_deadline="2026-09-25"), dict(TASKS[2])]
    second = runner.invoke(main, ["sync", "odoo"])
    assert second.exit_code == 0, second.output
    assert second.output.strip() == "odoo: 2 open tasks, 0 new, 1 updated, 1 closed"
    tasks = odoo_tasks(kb_env)
    assert len(tasks) == 3  # no duplicates
    assert str(tasks["KIO-1700"]["due"]) == "2026-09-25"
    assert tasks["KIO-1700"]["kinds"] == ["created", "synced"]
    shown = runner.invoke(main, ["show", str(tasks["KIO-1700"]["id"])]).output.splitlines()
    assert shown[1].endswith("source: odoo  external: KIO-1700  stage: In Progress")
    assert tasks["KIO-1701"]["kinds"] == ["created", "done"]
    assert tasks["KIO-1701"]["notes"] == ["closed in Odoo"]  # collect() drops nulls
    assert tasks["KIO-1701"]["done_at"] is not None
    assert tasks["103"]["kinds"] == ["created"]

    # a third run changes nothing: no new events, no reopen
    third = runner.invoke(main, ["sync", "odoo"])
    assert third.output.strip() == "odoo: 2 open tasks, 0 new, 0 updated, 0 closed"
    assert odoo_tasks(kb_env)["KIO-1700"]["kinds"] == ["created", "synced"]

    today = runner.invoke(main, ["today", "--scope", "proj"])
    assert today.exit_code == 0
    assert today.output.splitlines() == [
        "kb today 2026-09-14 (proj)",
        "+1 quiet (whisper), kb next",  # KIO-1700 is T-11: whisper; 103 is 4 working days old
    ]
    nxt = runner.invoke(main, ["next", "--scope", "proj"])
    assert f"[{tasks['KIO-1700']['id']}] L1 whisper  KIO-1700 First task  · T-11" in nxt.output
    assert "KIO-1701" not in nxt.output


def test_server_down_sets_meta_ok_false_and_today_says_so(kb_env) -> None:
    configure(kb_env, "http://127.0.0.1:1")  # nothing listens on port 1
    runner = CliRunner()
    result = runner.invoke(main, ["sync", "odoo"])
    assert result.exit_code == 0, result.output
    assert result.stdout == ""
    assert result.stderr.startswith("kb sync odoo: http://127.0.0.1:1:")
    state = meta(kb_env)
    assert state["ok"] is False
    assert "127.0.0.1:1" in state["error"]
    today = runner.invoke(main, ["today", "--scope", "proj"])
    assert today.exit_code == 0
    lines = today.output.splitlines()
    assert lines[0] == "kb today 2026-09-14 (proj)"
    assert lines[1].startswith("Odoo: sync failed, data as of ")
    assert "127.0.0.1:1" in lines[1]


def test_bad_password_and_missing_auth_file(kb_env, odoo: FakeOdoo) -> None:
    configure(kb_env, odoo.url, password="wrong")
    result = CliRunner().invoke(main, ["sync", "odoo"])
    assert result.exit_code == 0
    assert result.stderr.strip() == "kb sync odoo: login as mark on prod failed"
    assert meta(kb_env)["ok"] is False
    assert len(odoo.calls) == 1

    auth = configure(kb_env, odoo.url, password=None)
    auth.unlink(missing_ok=True)
    result = CliRunner().invoke(main, ["sync", "odoo"])
    assert result.exit_code == 0
    assert result.stderr.strip() == f"kb sync odoo: password file {auth} not found"
    assert len(odoo.calls) == 1  # the missing file made no network call


def test_disabled_makes_no_network_call_and_prints_nothing(kb_env, odoo: FakeOdoo) -> None:
    configure(kb_env, odoo.url, enabled=False)
    result = CliRunner().invoke(main, ["sync", "odoo"])
    assert result.exit_code == 0
    assert result.output == ""
    assert odoo.calls == []
    assert meta(kb_env) is None


def test_rpc_fault_is_a_sync_error(odoo: FakeOdoo, tmp_path: Path) -> None:
    defaults = load(path=tmp_path / "none.toml", home=tmp_path).sources.odoo
    cfg = dataclasses.replace(defaults, url=odoo.url, database="prod", username="mark")
    with pytest.raises(sync_odoo.SyncError, match="login as mark on prod failed"):
        sync_odoo.fetch_tasks(cfg, "nope")
    rows = sync_odoo.fetch_tasks(cfg, "secret")
    assert [r["id"] for r in rows] == [101, 102, 103]
    with pytest.raises(sync_odoo.SyncError, match="must start with http"):
        sync_odoo.rpc("ftp://x", "common", "login", [], 1)
    with pytest.raises(sync_odoo.SyncError, match="url is not set"):
        sync_odoo.fetch_tasks(dataclasses.replace(cfg, url=""), "secret")


def test_sync_never_overwrites_local_title_and_body(kb_env, odoo: FakeOdoo) -> None:
    configure(kb_env, odoo.url)
    runner = CliRunner()
    runner.invoke(main, ["sync", "odoo"])
    tasks = odoo_tasks(kb_env)
    first = tasks["KIO-1700"]["id"]
    runner.invoke(main, ["append", str(first), "my note"])
    runner.invoke(main, ["edit", str(first), "--title", "My own title"])
    odoo.tasks = [
        dict(TASKS[0], name="Renamed in Odoo", stage_id=[4, "Done-ish"]),
        dict(TASKS[1], name="Second renamed"),
        dict(TASKS[2]),
    ]
    result = runner.invoke(main, ["sync", "odoo"])
    assert result.output.strip() == "odoo: 3 open tasks, 0 new, 2 updated, 0 closed"
    tasks = odoo_tasks(kb_env)
    kept = tasks["KIO-1700"]
    assert kept["title"] == "My own title"
    assert kept["body"].endswith("**2026-09-14:** my note")
    assert kept["stage"] == "Done-ish"
    synced = json.loads(runner.invoke(main, ["--json", "log", str(first)]).stdout)["events"][0]
    assert synced["kind"] == "synced" and synced["payload"] == {"changed": ["stage"]}
    second = tasks["KIO-1701"]  # created by the sync, never edited: the title follows Odoo
    assert second["title"] == "KIO-1701 Second renamed"
    assert second["origin"] == "odoo-sync"
    assert second["kinds"] == ["created", "synced"]
    assert len(tasks) == 3


def test_promoted_record_keeps_its_text_and_is_never_closed(kb_env, odoo: FakeOdoo) -> None:
    configure(kb_env, odoo.url)
    runner = CliRunner()
    mine = json.loads(
        runner.invoke(
            main, ["--json", "add", "Ship the widget", "--scope", "proj", "--body", "plan"]
        ).stdout
    )["id"]
    runner.invoke(main, ["promote", str(mine), "KIO-1700"])
    outside = json.loads(
        runner.invoke(main, ["--json", "add", "Other ticket", "--scope", "proj"]).stdout
    )["id"]
    runner.invoke(main, ["promote", str(outside), "KIO-9999"])  # not in the result set
    result = runner.invoke(main, ["sync", "odoo"])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [
        "odoo: 3 open tasks, 2 new, 1 updated, 0 closed",
        f"1 untracked odoo records (not in the result set): {outside}",
    ]
    tasks = odoo_tasks(kb_env)
    assert len(tasks) == 4
    promoted = tasks["KIO-1700"]
    assert promoted["id"] == mine
    assert promoted["title"] == "Ship the widget"
    assert promoted["body"] == "plan"
    assert promoted["sched"] == "soft" and str(promoted["due"]) == "2026-09-20"
    assert promoted["stage"] == "In Progress"
    assert promoted["kinds"] == ["created", "promoted", "synced"]
    other = tasks["KIO-9999"]
    assert "done_at" not in other and "synced_at" not in other
    again = runner.invoke(main, ["sync", "odoo"])
    assert again.output.splitlines() == ["odoo: 3 open tasks, 0 new, 0 updated, 0 closed"]
    assert meta(kb_env)["untracked"] == 1  # unchanged since the last run: not repeated


def test_migrated_items_are_matched_by_odoo_id_not_closed(kb_env, odoo: FakeOdoo) -> None:
    configure(kb_env, odoo.url)
    runner = CliRunner()
    fixture = Path(__file__).parent / "fixtures" / "state_sample.md"
    migrated = runner.invoke(main, ["migrate", "state", str(fixture), "--scope", "proj"])
    assert migrated.exit_code == 0, migrated.output
    odoo.tasks = [
        {
            "id": 33172,
            "key": "KIO-1001",
            "name": "distributor rows outside the convention",
            "stage_id": [2, "To Do"],
            "date_assign": "2026-09-09 02:00:00",
            "date_deadline": False,
            "create_date": "2026-09-09 02:00:00",
        }
    ]
    result = runner.invoke(main, ["sync", "odoo"])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[0] == "odoo: 1 open tasks, 0 new, 1 updated, 0 closed"
    tasks = odoo_tasks(kb_env)
    assert sorted(tasks) == ["KIO-1001"]  # normalised from 33172, no duplicate
    item = tasks["KIO-1001"]
    assert item["id"] == 256
    assert "done_at" not in item
    assert item["title"].startswith("KIO-1001 the 2,906 parts")  # migrated: the title is kept
    assert "origin" not in item
    assert item["body"].startswith("KIO-1001 the 2,906 parts")  # the migrated text stays
    assert item["kinds"] == ["created", "synced"]
    payload = json.loads(runner.invoke(main, ["--json", "log", "256"]).stdout)["events"][0][
        "payload"
    ]
    assert payload == {"changed": ["external_id", "stage"]}
    odoo.tasks[0]["name"] = "renamed again in Odoo"
    runner.invoke(main, ["sync", "odoo"])
    assert odoo_tasks(kb_env)["KIO-1001"]["title"].startswith("KIO-1001 the 2,906 parts")
    # the verifier's second step: a migrated item promoted to a key outside the result set
    assert runner.invoke(main, ["promote", "251", "KIO-9999"]).exit_code == 0
    second = runner.invoke(main, ["sync", "odoo"])
    assert second.output.splitlines() == [
        "odoo: 1 open tasks, 0 new, 0 updated, 0 closed",
        "1 untracked odoo records (not in the result set): 251",
    ]
    again = odoo_tasks(kb_env)
    assert again["KIO-9999"]["kinds"] == ["created", "promoted"]
    assert "done_at" not in again["KIO-9999"]


def test_two_records_on_one_odoo_id_are_both_kept_in_sync(kb_env, odoo: FakeOdoo) -> None:
    """The live state.md shares three Odoo ids between two items each: external_id is not unique."""
    configure(kb_env, odoo.url)
    runner = CliRunner()
    ids = [
        json.loads(
            runner.invoke(
                main,
                [
                    "--json",
                    "add",
                    title,
                    "--scope",
                    "proj",
                    "--source",
                    "odoo",
                    "--external-id",
                    "33052",
                ],
            ).stdout
        )["id"]
        for title in ("first angle on the ticket", "second angle on the ticket")
    ]
    odoo.tasks = [
        dict(TASKS[0], id=33052, key="KIO-1500", name="Shared ticket", date_deadline="2026-10-01")
    ]
    result = runner.invoke(main, ["sync", "odoo"])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "odoo: 1 open tasks, 0 new, 2 updated, 0 closed"
    rows, _, _ = kb_env.driver.execute_query(
        "MATCH (t:Record:Task {source: 'odoo'}) RETURN t ORDER BY t.id", database_="neo4j"
    )
    tasks = [dict(r["t"]) for r in rows]
    assert [t["id"] for t in tasks] == ids  # no third record
    for t, title in zip(
        tasks, ("first angle on the ticket", "second angle on the ticket"), strict=True
    ):
        assert t["external_id"] == "KIO-1500"
        assert t["sched"] == "soft" and str(t["due"]) == "2026-10-01"
        assert t["stage"] == "In Progress"
        assert t["title"] == title  # added by hand, not by the sync: the title is yours
        assert t["synced_at"] is not None
    odoo.tasks = []
    gone = runner.invoke(main, ["sync", "odoo"])
    assert gone.output.strip() == "odoo: 0 open tasks, 0 new, 0 updated, 2 closed"
    closed = odoo_tasks(kb_env)  # keyed by external_id: both share KIO-1500, take the raw rows
    rows, _, _ = kb_env.driver.execute_query(
        "MATCH (t:Record:Task {source: 'odoo'}) WHERE t.done_at IS NOT NULL RETURN count(t) AS n",
        database_="neo4j",
    )
    assert rows[0]["n"] == 2
    assert len(closed) == 1


def test_long_names_are_truncated_and_bad_rows_skipped(kb_env, odoo: FakeOdoo) -> None:
    configure(kb_env, odoo.url)
    odoo.tasks = [
        dict(TASKS[0], name="x" * 129),
        dict(TASKS[1], id=104, key="KIO-1704", date_assign=False, create_date=False),
    ]
    result = CliRunner().invoke(main, ["sync", "odoo"])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == (
        "odoo: 2 open tasks, 1 new, 0 updated, 0 closed, "
        "skipped rows: 104 (no date_assign and no create_date)"
    )
    tasks = odoo_tasks(kb_env)
    assert list(tasks) == ["KIO-1700"]
    assert len(tasks["KIO-1700"]["title"]) == 120
    assert tasks["KIO-1700"]["title"].startswith("KIO-1700 xxx")
    assert meta(kb_env)["ok"] is True and meta(kb_env)["count"] == 2


def test_a_failure_inside_the_write_still_records_meta(kb_env, odoo: FakeOdoo, monkeypatch) -> None:
    configure(kb_env, odoo.url)
    runner = CliRunner()
    first = runner.invoke(main, ["sync", "odoo"])
    assert first.exit_code == 0, first.output
    assert meta(kb_env)["count"] == 3

    def boom(*_args, **_kwargs):
        raise ValueError("boom")

    # a nested context: undoing the shared monkeypatch would also drop kb_env's KB_CONFIG
    # and point the CLI at the live ~/.config/kb/config.toml
    with pytest.MonkeyPatch.context() as inner:
        inner.setattr(sync_odoo, "upsert", boom)
        result = runner.invoke(main, ["sync", "odoo"])
    assert result.exit_code == 0
    assert result.stderr.strip() == "kb sync odoo: boom"
    state = meta(kb_env)
    assert (state["ok"], state["error"], state["count"]) == (False, "boom", 3)
    assert sync_odoo.upsert is not boom
    assert os.environ["KB_CONFIG"] == str(kb_env.config_file)

    configure(kb_env, "http://127.0.0.1:1")  # a transport failure before any row: count kept
    down = runner.invoke(main, ["sync", "odoo"])
    assert down.exit_code == 0
    state = meta(kb_env)
    assert state["ok"] is False and state["count"] == 3
    assert os.environ["KB_CONFIG"] == str(kb_env.config_file)
    assert Path(os.environ["KB_CONFIG"]).is_relative_to(kb_env.home)


def test_password_file_must_be_private(kb_env, odoo: FakeOdoo) -> None:
    auth = configure(kb_env, odoo.url)
    auth.chmod(0o640)
    result = CliRunner().invoke(main, ["sync", "odoo"])
    assert result.exit_code == 0
    assert result.stderr.strip() == (
        f"kb sync odoo: password file {auth} must be mode 0600 or 0400, is 0640"
    )
    assert odoo.calls == []
    assert meta(kb_env)["ok"] is False
    auth.chmod(0o400)
    accepted = CliRunner().invoke(main, ["sync", "odoo"])
    assert accepted.exit_code == 0, accepted.output
    assert accepted.output.startswith("odoo: 3 open tasks")


def test_unchanged_rows_leave_updated_at_alone_and_local_done_is_reopened(
    kb_env, odoo: FakeOdoo
) -> None:
    configure(kb_env, odoo.url)
    runner = CliRunner()
    runner.invoke(main, ["sync", "odoo"])
    before = {k: (v["updated_at"], v["synced_at"]) for k, v in odoo_tasks(kb_env).items()}
    runner.invoke(main, ["sync", "odoo"])
    after = odoo_tasks(kb_env)
    for key, (updated, synced) in before.items():
        assert after[key]["updated_at"] == updated  # nothing changed: not touched
        assert after[key]["synced_at"] > synced  # but seen again
    # kb done on an Odoo task does not stick while Odoo keeps it open
    first = after["KIO-1700"]["id"]
    runner.invoke(main, ["done", str(first)])
    assert odoo_tasks(kb_env)["KIO-1700"].get("done_at") is not None
    runner.invoke(main, ["sync", "odoo"])
    reopened = odoo_tasks(kb_env)["KIO-1700"]
    assert "done_at" not in reopened
    assert reopened["kinds"][-1] == "synced"


def test_assignee_login_resolves_the_uid_the_tasks_are_filtered_on(kb_env, odoo: FakeOdoo) -> None:
    configure(kb_env, odoo.url, assignee="dev")
    runner = CliRunner()
    result = runner.invoke(main, ["sync", "odoo"])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "odoo: 3 open tasks, 3 new, 0 updated, 0 closed"
    login, users, search = odoo.calls
    assert login["method"] == "login"
    assert users["args"][3:6] == ["res.users", "search_read", [[["login", "=", "dev"]]]]
    assert users["args"][6] == {"fields": ["id"], "limit": 1}
    assert search["args"][5][0][0] == ["user_ids", "in", [42]]  # the resolved id, not the RPC uid

    configure(kb_env, odoo.url, assignee="nobody")
    missing = runner.invoke(main, ["sync", "odoo"])
    assert missing.exit_code == 0
    assert missing.stderr.strip() == "kb sync odoo: assignee 'nobody' not found in res.users"
    assert meta(kb_env)["ok"] is False
    assert odoo.calls[-1]["args"][3] == "res.users"  # no task query after the failed lookup


def test_untracked_list_is_capped_and_printed_only_when_it_changes(kb_env, odoo: FakeOdoo) -> None:
    configure(kb_env, odoo.url)
    runner = CliRunner()
    ids = []
    for n in range(10):
        rec = json.loads(
            runner.invoke(
                main,
                [
                    "--json",
                    "add",
                    f"old {n}",
                    "--scope",
                    "proj",
                    "--source",
                    "odoo",
                    "--external-id",
                    f"OLD-{n}",
                ],
            ).stdout
        )
        ids.append(rec["id"])
    first = runner.invoke(main, ["sync", "odoo"])
    assert first.output.splitlines()[1] == (
        "10 untracked odoo records (not in the result set): "
        + ", ".join(str(i) for i in ids[:8])
        + ", … and 2 more (kb show <id>)"
    )
    assert meta(kb_env)["untracked"] == 10
    second = runner.invoke(main, ["sync", "odoo"])
    assert second.output.splitlines() == ["odoo: 3 open tasks, 0 new, 0 updated, 0 closed"]
    runner.invoke(main, ["archive", str(ids[0]), "--reason", "gone"])
    third = runner.invoke(main, ["sync", "odoo"])
    assert third.output.splitlines()[1].startswith("9 untracked odoo records")
