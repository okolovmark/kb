"""Pin levels: kb pin, kb unpin, kb add --critical, and the order and marker of kb index."""

import json
from typing import Any

import pytest
from click.testing import CliRunner, Result

from conftest import with_area
from kb import consolidate
from kb.cli import main
from kb.records import Record, pin_rank

HEADER = (
    "# kb index 2026-09-14 (proj) — generated, do not edit; write with kb add/append, "
    "read with kb show <id|slug>, search with kb search"
)


def kb(*args: str, code: int = 0) -> Result:
    result = CliRunner().invoke(main, with_area(list(args)))
    assert result.exit_code == code, result.output
    return result


def kb_json(*args: str) -> Any:
    return json.loads(kb("--json", *args).stdout)


def add(title: str, *args: str, scope: str = "proj") -> dict[str, Any]:
    return kb_json("add", title, "--scope", scope, *args)


def log(ref: str) -> list[dict[str, Any]]:
    return kb_json("log", ref)["events"]


def stored_level(kb_env, rid: int) -> Any:
    """The property as written, before the read turns a missing one into normal."""
    records, _, _ = kb_env.driver.execute_query(
        "MATCH (r:Record {id: $id}) RETURN r.pin_level AS level", id=rid, database_="neo4j"
    )
    return records[0]["level"]


# --- pin / unpin ------------------------------------------------------------------------


def test_pin_sets_the_level_and_writes_the_change(kb_env) -> None:
    note = add("Release checklist", "--kind", "note")
    rid = str(note["id"])
    assert (note["pinned"], note["pin_level"]) == (False, None)

    out = kb("pin", rid).output.strip()
    assert out == f"[{rid}] Release checklist  (release_checklist)  · pinned normal"
    rec = kb_json("show", rid)
    assert (rec["pinned"], rec["pin_level"]) == (True, "normal")
    assert stored_level(kb_env, note["id"]) == "normal"
    event = log(rid)[0]
    assert event["kind"] == "edited"
    assert event["payload"] == {
        "fields": ["pin_level", "pinned"],
        "before": {"pin_level": None, "pinned": False},
        "after": {"pin_level": "normal", "pinned": True},
    }

    promoted = kb("pin", "release_checklist", "--critical").output.strip()
    assert promoted.endswith("  · pinned critical (was normal)")
    assert log(rid)[0]["payload"] == {
        "fields": ["pin_level"],
        "before": {"pin_level": "normal"},
        "after": {"pin_level": "critical"},
    }
    assert "  pinned: critical  " in kb("show", rid).output

    demoted = kb_json("pin", rid, "--normal")
    assert (demoted["pin_level"], demoted["written"]) == ("normal", True)
    assert demoted["before"] == {"pinned": True, "pin_level": "critical"}
    assert kb("pin", rid, "--normal").output.strip().endswith("  already pinned normal")
    assert [e["kind"] for e in log(rid)] == ["edited", "edited", "edited", "created"]


@pytest.mark.usefixtures("kb_env")
def test_an_unchanged_pin_writes_nothing_and_says_already() -> None:
    rid = str(add("Never force-push", "--kind", "feedback")["id"])
    assert [e["kind"] for e in log(rid)] == ["created"]
    same = kb("pin", rid).output.strip()
    assert same == f"[{rid}] Never force-push  (never_force_push)  already pinned normal"
    kb("pin", rid, "--critical")
    again = kb_json("pin", rid, "--critical")
    assert (again["written"], again["pin_level"]) == (False, "critical")
    assert kb("pin", rid, "--critical").output.strip().endswith("  already pinned critical")
    assert [e["kind"] for e in log(rid)] == ["edited", "created"]


def test_unpin_removes_the_level_with_an_event(kb_env) -> None:
    rec = add("Answer in the reader's language", "--kind", "feedback", "--critical")
    rid = str(rec["id"])
    out = kb("unpin", rid).output.strip()
    assert out.endswith("  · unpinned (was critical)")
    shown = kb_json("show", rid)
    assert (shown["pinned"], shown["pin_level"]) == (False, None)
    assert stored_level(kb_env, rec["id"]) is None
    assert "  pinned: no  " in kb("show", rid).output
    assert log(rid)[0]["payload"] == {
        "fields": ["pin_level", "pinned"],
        "before": {"pin_level": "critical", "pinned": True},
        "after": {"pin_level": None, "pinned": False},
    }
    assert kb("unpin", rid).output.strip().endswith("  already unpinned")
    assert kb_json("unpin", rid)["written"] is False
    assert [e["kind"] for e in log(rid)] == ["edited", "created"]


def test_pin_refuses_a_task_and_respects_the_view(kb_env, monkeypatch) -> None:
    task = str(add("Ship the release")["id"])
    refused = kb("pin", task, code=1).output.strip()
    assert refused == f"Error: [{task}] is a task; pin is for non-task kinds"
    assert kb("unpin", task).output.strip().endswith("  already unpinned")
    assert [e["kind"] for e in log(task)] == ["created"]

    private = str(add("Dentist checklist", "--kind", "note", scope="personal")["id"])
    monkeypatch.chdir(kb_env.proj)
    message = (
        f"Error: not visible in this scope: [{private}] is in scope personal; "
        "rerun with --scope personal"
    )
    for args in (["pin", private], ["pin", private, "--critical"], ["unpin", private]):
        result = kb(*args, code=1)
        assert result.output.strip() == message, args
        assert "Dentist" not in result.output
    assert kb_json("pin", private, "--scope", "personal")["pin_level"] == "normal"


def test_a_record_pinned_before_the_levels_counts_as_normal(kb_env) -> None:
    rec = add("Read the code before the fix", "--kind", "feedback")
    rid = str(rec["id"])
    kb_env.driver.execute_query(
        "MATCH (r:Record {id: $id}) REMOVE r.pin_level", id=rec["id"], database_="neo4j"
    )
    assert stored_level(kb_env, rec["id"]) is None
    assert kb_json("show", rid)["pin_level"] == "normal"
    assert kb_json("index", "--scope", "proj")["pinned"][0]["pin_level"] == "normal"
    assert kb("pin", rid).output.strip().endswith("  already pinned normal")
    assert [e["kind"] for e in log(rid)] == ["created"]
    assert kb("pin", rid, "--critical").output.strip().endswith("  · pinned critical (was normal)")


# --- add --critical ---------------------------------------------------------------------


@pytest.mark.usefixtures("kb_env")
def test_add_critical_implies_the_pin() -> None:
    fb = add("Never post credentials", "--kind", "feedback", "--critical")
    assert (fb["pinned"], fb["pin_level"]) == (True, "critical")
    note = add("Keep the ledger balanced", "--kind", "note", "--critical", "--pin")
    assert (note["pinned"], note["pin_level"]) == (True, "critical")
    assert add("Rotate the backups", "--kind", "feedback")["pin_level"] == "normal"
    assert add("Port map of the test boxes", "--kind", "note")["pin_level"] is None
    assert add("Where the dumps live", "--kind", "note", "--pin")["pin_level"] == "normal"
    # the refusals are in test_commands.test_add_rejects_bad_flag_combinations


# --- index ------------------------------------------------------------------------------


@pytest.mark.usefixtures("kb_env")
def test_index_prints_critical_rules_first_with_a_marker() -> None:
    first = add("Write a test for every fix", "--kind", "feedback")
    second = add("Estimate from the calibration table", "--kind", "feedback", scope="global")
    late = add("Reply in the user's language", "--kind", "feedback", "--summary", "Every reply")
    note = add("Deploy notes", "--kind", "note")
    kb("pin", str(late["id"]), "--critical")
    kb("pin", str(first["id"]), "--critical")
    out = kb("index", "--scope", "proj").output.splitlines()
    assert out == [
        HEADER,
        "",
        "## Pinned",
        f"- [{first['id']}] CRITICAL write_a_test_for_every_fix — Write a test for every fix",
        f"- [{late['id']}] CRITICAL reply_in_the_user_s_language — Every reply",
        f"- [{second['id']}] estimate_from_the_calibration_table — "
        "Estimate from the calibration table",
        "",
        "## Recent",
        f"- [{note['id']}] deploy_notes — Deploy notes",
    ]
    # a session hook takes the Pinned rules as the `- [` lines under `## Pinned`
    pinned = out[out.index("## Pinned") + 1 : out.index("## Recent") - 1]
    assert all(line.startswith("- [") for line in pinned)
    # a short budget keeps the critical rules and drops the normal ones
    cut = kb("index", "--scope", "proj", "--budget", "5").output.splitlines()
    assert cut == out[:5]
    kb("pin", str(first["id"]), "--normal")
    again = kb("index", "--scope", "proj").output.splitlines()
    assert again[3] == f"- [{late['id']}] CRITICAL reply_in_the_user_s_language — Every reply"
    assert again[4] == f"- [{first['id']}] write_a_test_for_every_fix — Write a test for every fix"


@pytest.mark.usefixtures("kb_env")
def test_index_json_carries_the_pin_level() -> None:
    assert kb_json("index", "--scope", "proj") == {
        "date": "2026-09-14",
        "scope": "proj",
        "pinned": [],
        "recent": [],
    }
    rule = add("Check the scope first", "--kind", "feedback")
    top = add("Answer in one line", "--kind", "feedback", "--critical", "--summary", "Short")
    note = add("Port map", "--kind", "note")
    data = kb_json("index", "--scope", "proj")
    assert [(e["id"], e["pin_level"]) for e in data["pinned"]] == [
        (top["id"], "critical"),
        (rule["id"], "normal"),
    ]
    assert data["pinned"][0] == {
        "id": top["id"],
        "kind": "feedback",
        "scope": "proj",
        "slug": "answer_in_one_line",
        "title": "Answer in one line",
        "summary": "Short",
        "pin_level": "critical",
    }
    assert [(e["id"], e["pin_level"]) for e in data["recent"]] == [(note["id"], None)]
    per_command = json.loads(kb("index", "--scope", "proj", "--json").stdout)
    assert per_command == data
    # the budget cuts the JSON where it cuts the text
    cut = kb_json("index", "--scope", "proj", "--budget", "4")
    assert [e["id"] for e in cut["pinned"]] == [top["id"]] and cut["recent"] == []


def test_index_json_reports_an_unreachable_store(tmp_path, monkeypatch) -> None:
    config = tmp_path / "closed.toml"
    config.write_text("[neo4j]\nbolt_port = 1\n")
    monkeypatch.setenv("KB_CONFIG", str(config))
    monkeypatch.setenv("KB_NEO4J_AUTH", str(tmp_path / "none"))
    data = json.loads(kb("--json", "index").stdout)
    assert data["pinned"] == [] and data["recent"] == []
    assert data["error"].startswith("neo4j-kb is not reachable on bolt://127.0.0.1:1")


# --- ranks ------------------------------------------------------------------------------


def test_pin_rank_and_the_record_consolidate_keeps() -> None:
    critical = Record(1, "note", "proj", "a", body="short", pinned=True, pin_level="critical")
    normal = Record(2, "note", "proj", "b", body="a longer body", pinned=True, pin_level="normal")
    loose = Record(3, "note", "proj", "c", body="the longest body of the three")
    assert [pin_rank(r) for r in (critical, normal, loose)] == [0, 1, 2]
    assert consolidate._keep([loose, normal, critical]) is critical
    assert consolidate._keep([loose, normal]) is normal
