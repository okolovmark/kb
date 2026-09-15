"""Every phase 1 command against the fixture DB."""

import json
import logging
import time
from typing import Any

import pytest
from click.testing import CliRunner, Result

from kb.cli import main

MONDAY = "2026-09-14"  # KB_TODAY in kb_env


def kb(*args: str, code: int = 0, obj: Any = None, stdin: str | None = None) -> Result:
    result = CliRunner().invoke(main, list(args), obj=obj, input=stdin)
    assert result.exit_code == code, result.output
    return result


def kb_json(*args: str) -> Any:
    return json.loads(kb("--json", *args).stdout)


def add(*args: str) -> int:
    return kb_json("add", *args, "--scope", "proj")["id"]


def events(ref: str) -> list[str]:
    return [e["kind"] for e in kb_json("log", ref)["events"]]


# --- add --------------------------------------------------------------------------------


@pytest.mark.usefixtures("kb_env")
def test_add_task_defaults_to_age_anchored_today() -> None:
    result = kb("add", "Fix the widget", "--scope", "proj")
    line = result.output.strip()
    assert line.startswith("[") and line.endswith("] Fix the widget")
    rec = kb_json("show", line.split("]")[0][1:])
    assert rec["kind"] == "task"
    assert rec["scope"] == "proj"
    assert rec["slug"] is None
    assert rec["sched"] == "age"
    assert rec["anchor"] == MONDAY
    assert rec["days"] == "work"
    assert rec["pinned"] is False
    assert rec["source"] == "manual"
    assert rec["urgency"] == {
        "level": 0,
        "name": "silent",
        "reason": f"0 working days since {MONDAY}",
    }
    assert [e["kind"] for e in rec["events"]] == ["created"]
    assert rec["id"] >= 265


@pytest.mark.usefixtures("kb_env")
def test_add_scope_from_cwd_and_refusal_outside(kb_env, monkeypatch) -> None:
    outside = kb("add", "nowhere", code=2)
    assert "outside every configured scope, pass --scope" in outside.output
    monkeypatch.chdir(kb_env.proj)
    rec = kb_json("add", "inside the project")
    assert rec["scope"] == "proj"
    assert kb_json("add", "personal thing", "--scope", "personal")["days"] == "off"
    assert kb_json("add", "global thing", "--scope", "global")["days"] == "any"
    assert kb_json("add", "explicit", "--scope", "global", "--days", "work")["days"] == "work"
    bad = kb("add", "x", "--scope", "nope", code=2)
    assert "unknown scope 'nope'; one of global, personal, proj" in bad.output


@pytest.mark.usefixtures("kb_env")
def test_add_non_task_kinds_get_unique_slugs_and_pins() -> None:
    first = kb("add", "Deploy Checklist: prod!", "--kind", "howto", "--scope", "proj")
    assert first.output.strip().endswith("] Deploy Checklist: prod!  (deploy_checklist_prod)")
    second = kb_json("add", "Deploy checklist prod", "--kind", "howto", "--scope", "proj")
    assert second["slug"] == "deploy_checklist_prod_2"
    third = kb_json("add", "Deploy checklist prod", "-k", "howto", "--scope", "proj")
    assert third["slug"] == "deploy_checklist_prod_3"
    fb = kb_json("add", "Never post creds", "--kind", "feedback", "--scope", "global")
    assert fb["pinned"] is True
    assert fb["sched"] is None
    unpinned = kb_json(
        "add", "Note", "--kind", "note", "--no-pin", "--slug", "My Slug!", "--scope", "proj"
    )
    assert unpinned["pinned"] is False
    assert unpinned["slug"] == "my_slug"
    assert kb_json("show", "my_slug")["id"] == unpinned["id"]
    assert (
        kb_json("add", "Pinned note", "--kind", "note", "--pin", "--scope", "proj")["pinned"]
        is True
    )
    assert kb("add", "t", "--slug", "x", "--scope", "proj", code=2).output.count("non-task") == 1
    digits = kb("add", "2026", "--kind", "note", "--scope", "proj", code=1)
    assert (
        digits.output.strip() == "Error: slug '2026' is all digits; digits mean an id, pass --slug"
    )
    assert (
        kb_json("add", "2026", "--kind", "note", "--slug", "year_2026", "--scope", "proj")["slug"]
        == "year_2026"
    )
    tagged = kb_json("add", "Tagged", "--tag", "a", "--tag", "a", "--tag", "b", "--scope", "proj")
    assert tagged["tags"] == ["a", "b"]


@pytest.mark.usefixtures("kb_env")
def test_add_schedule_flags() -> None:
    hard = kb_json("add", "Taxes", "--due", "2026-09-15", "--scope", "personal")
    assert (hard["sched"], hard["due"], hard["anchor"]) == ("hard", "2026-09-15", None)
    soft = kb_json(
        "add", "Plan", "--plan", "2026-09-30", "--repeat", "interval:30d", "--scope", "proj"
    )
    assert (soft["sched"], soft["due"], soft["repeat"]) == ("soft", "2026-09-30", "interval:30")
    weekend = kb_json("add", "Laundry", "--window", "weekend", "--scope", "personal")
    assert weekend["window_rrule"] == "FREQ=WEEKLY;BYDAY=SA,SU"
    assert weekend["anchor"] == MONDAY
    assert kb_json("add", "L2", "--window", "SAT,SUN", "--scope", "personal")["window_rrule"] == (
        "FREQ=WEEKLY;BYDAY=SA,SU"
    )
    raw = kb_json("add", "L3", "--window", "RRULE:FREQ=WEEKLY;BYDAY=WE", "--scope", "personal")
    assert raw["window_rrule"] == "FREQ=WEEKLY;BYDAY=WE"
    once = kb_json("add", "Tell me once", "--once", "--scope", "proj")
    assert once["sched"] == "once"
    aged = kb_json("add", "Old", "--anchor", "2026-08-20", "--scope", "proj")
    assert aged["anchor"] == "2026-08-20"
    tight = kb_json(
        "add", "Tight", "--due", "2026-09-30", "--escalation", '{"loud": 5}', "--scope", "proj"
    )
    assert tight["escalation"] == '{"loud": 5}'
    linked = kb_json(
        "add",
        "Odoo one",
        "--source",
        "odoo",
        "--external-id",
        "KIO-1",
        "--journal-ref",
        "journal/2026-09-10",
        "--scope",
        "proj",
    )
    assert (linked["source"], linked["external_id"], linked["journal_ref"]) == (
        "odoo",
        "KIO-1",
        "journal/2026-09-10",
    )


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--due", "2026-09-15", "--plan", "2026-09-16"], "--due and --plan are exclusive"),
        (["--due", "2026-09-15", "--once"], "--due and --once are exclusive"),
        (["--window", "weekend", "--once"], "--window and --once are exclusive"),
        (["--repeat", "interval:30d"], "--repeat requires --due or --plan"),
        (["--window", "weekend", "--repeat", "interval:7d"], "--repeat requires --due or --plan"),
        (["--due", "15.09.2026"], "expected YYYY-MM-DD, got '15.09.2026'"),
        (["--window", "FREQ=NEVER"], "invalid rrule 'FREQ=NEVER'"),
        (
            ["--due", "2026-09-15", "--repeat", "weekly"],
            "repeat must be calendar:<rrule> or interval:<N>d",
        ),
        (
            ["--due", "2026-09-15", "--repeat", "interval:0d"],
            "interval must be a positive day count",
        ),
        (["--escalation", "[1]"], "escalation must be a JSON object"),
        (["--escalation", '{"x": 1}'], "escalation: unknown key 'x'"),
        (["--escalation", '{"loud": "5"}'], "escalation: loud must be an integer"),
        (["--anchor", "2026-08-01", "--due", "2026-09-15"], "--anchor applies to age tasks"),
        (
            ["--kind", "note", "--due", "2026-09-15"],
            "schedule flags apply to tasks only, not to a note",
        ),
        (["--kind", "note", "--days", "work"], "schedule flags apply to tasks only"),
        (["--link", "knows:1"], "unknown relationship type 'knows'"),
        (["--link", "related"], "expected REL:REF, got 'related'"),
        (["--link", "related:999999"], "no record 999999"),
        (["--pin"], "--pin/--no-pin apply to non-task kinds"),
        (["--no-pin"], "--pin/--no-pin apply to non-task kinds"),
        (["--window", "DTSTART:20260101\nRRULE:FREQ=WEEKLY;BYDAY=SA"], "a single RRULE body"),
        (["--due", "2026-09-15", "--repeat", "calendar:DTSTART:20260101"], "a single RRULE body"),
    ],
)
@pytest.mark.usefixtures("kb_env")
def test_add_rejects_bad_flag_combinations(args: list[str], message: str) -> None:
    result = CliRunner().invoke(main, ["add", "t", "--scope", "proj", *args])
    assert result.exit_code in (1, 2), result.output
    assert message in result.output
    assert "Traceback" not in result.output


@pytest.mark.usefixtures("kb_env")
def test_add_with_body_and_links() -> None:
    target = add("Target")
    rec = kb_json(
        "add",
        "Source",
        "--scope",
        "proj",
        "--body",
        "-",
        "--link",
        f"related:{target}",
        "--tag",
        "a",
        "--tag",
        "b",
    )
    assert rec["tags"] == ["a", "b"]
    assert rec["body"] == ""  # empty stdin under CliRunner
    piped = CliRunner().invoke(
        main, ["add", "Piped", "--scope", "proj", "--body", "-"], input="line one\nline two\n"
    )
    assert piped.exit_code == 0, piped.output
    links = kb("links", str(rec["id"])).output.strip()
    assert links == f"RELATED -> [{target}] Target"
    back = kb("links", str(target)).output.strip()
    assert back == f"RELATED <- [{rec['id']}] Source"
    assert events(str(rec["id"])) == ["linked", "created"]


# --- append / edit ----------------------------------------------------------------------


@pytest.mark.usefixtures("kb_env")
def test_append_and_edit(tmp_path) -> None:
    rid = str(add("Note-ish"))
    kb("append", rid, "first thought")
    kb("append", rid, "second thought")
    rec = kb_json("show", rid)
    assert rec["body"] == f"**{MONDAY}:** first thought\n\n**{MONDAY}:** second thought"
    assert events(rid) == ["edited", "edited", "created"]

    unchanged = kb("edit", rid, "--title", "Note-ish")
    assert unchanged.output.strip().endswith("unchanged")
    assert events(rid) == ["edited", "edited", "created"]

    body_file = tmp_path / "body.md"
    body_file.write_text("# replaced\n")
    kb("edit", rid, "--title", "Renamed", "--body-file", str(body_file))
    rec = kb_json("show", rid)
    assert (rec["title"], rec["body"]) == ("Renamed", "# replaced\n")
    edited = kb_json("log", rid)["events"][0]
    assert edited["payload"] == {
        "fields": ["body", "title"],
        "before": {
            "title": "Note-ish",
            "body": f"**{MONDAY}:** first thought\n\n**{MONDAY}:** second thought",
        },
    }
    appended = kb_json("log", rid)["events"][-2]  # the first append: body was empty before
    assert appended["payload"] == {"fields": ["body"], "append": True, "before": {"body": ""}}
    assert (
        kb("edit", rid, "--body", "x", "--body-file", str(body_file), code=2).output.count(
            "exclusive"
        )
        == 1
    )
    assert kb("edit", rid, "--title", "x" * 121, code=1).output.strip() == (
        "Error: title longer than 120 characters (121)"
    )


@pytest.mark.usefixtures("kb_env")
def test_edit_opens_the_editor_when_no_flag_is_given(monkeypatch) -> None:
    rid = str(add("Editable"))
    monkeypatch.setattr("click.edit", lambda text, **_kw: text + "\nmore\n")
    kb("edit", rid)
    assert kb_json("show", rid)["body"] == "\nmore"
    monkeypatch.setattr("click.edit", lambda _text, **_kw: None)
    assert kb("edit", rid).output.strip().endswith("unchanged")


# --- show / search / graph / log --------------------------------------------------------


@pytest.mark.usefixtures("kb_env")
def test_show_text_layout() -> None:
    rid = str(add("Taxes", "--due", "2026-09-16", "--tag", "money", "--body", "pay them"))
    other = str(add("Other"))
    kb("link", rid, "blocks", other)
    out = kb("show", rid).output.splitlines()
    assert out[0] == f"[{rid}] Taxes"
    assert out[1] == "kind: task  scope: proj  slug: -  tags: money  pinned: no  source: manual"
    assert out[2] == "schedule: hard due 2026-09-16 days=work"
    assert out[3] == "urgency: L3 loud — T-2"
    assert out[4].startswith("created: 2026-") and "updated: 2026-" in out[4]
    assert out[5:7] == ["", "pay them"]
    assert out[7:9] == ["", "links:"]
    assert out[9] == f"  BLOCKS -> [{other}] Other"
    assert out[10:12] == ["", "events:"]
    assert out[12].endswith("Z  linked  " + json.dumps({"target": int(other), "type": "BLOCKS"}))
    assert out[13].endswith("Z  created")
    missing = kb("show", "999999", code=1)
    assert missing.output.strip() == "Error: no record 999999"
    assert kb("show", "no_such_slug", code=1).output.strip() == "Error: no record no_such_slug"


@pytest.mark.usefixtures("kb_env")
def test_search_scopes_kinds_and_archived() -> None:
    a = add("Neo4j indexes explained", "--kind", "howto", "--body", "range index on kind")
    b = add("Unrelated task about cats")
    c = kb_json("add", "Personal neo4j note", "--kind", "note", "--scope", "personal")["id"]
    hits = kb("search", "neo4j").output.splitlines()
    assert {line.split("]")[0][1:] for line in hits} == {str(a), str(c)}
    assert hits[0].split("  ")[-1].replace(".", "").isdigit()  # trailing score
    assert any(
        line.startswith(f"[{a}] howto neo4j_indexes_explained Neo4j indexes explained")
        for line in hits
    )
    assert kb("search", "neo4j", "--scope", "proj").output.count("\n") == 1
    assert kb("search", "neo4j", "--kind", "note").output.startswith(f"[{c}] note")
    assert kb("search", "cats").output.startswith(f"[{b}] task - Unrelated")
    kb("archive", str(a), "--reason", "outdated")
    assert str(a) not in kb("search", "neo4j").output
    assert str(a) in kb("search", "neo4j", "--all").output
    as_json = kb_json("search", "index", "--all")
    assert as_json["hits"][0]["id"] == a
    assert kb("search", "neo4j", "--scope", "nope", code=2).output.count("unknown scope") == 1


@pytest.mark.usefixtures("kb_env")
def test_search_follows_the_cwd_scope(kb_env, monkeypatch) -> None:
    mine = add("Neo4j in the project", "--kind", "howto")
    glob = kb_json("add", "Neo4j everywhere", "--kind", "note", "--scope", "global")["id"]
    personal = kb_json("add", "Neo4j at home", "--kind", "note", "--scope", "personal")["id"]
    monkeypatch.chdir(kb_env.proj)
    ids = {int(line.split("]")[0][1:]) for line in kb("search", "neo4j").output.splitlines()}
    assert ids == {mine, glob}
    assert personal not in ids
    with_flag = {
        int(line.split("]")[0][1:])
        for line in kb("search", "neo4j", "--scope", "personal").output.splitlines()
    }
    assert with_flag == {personal, glob}


@pytest.mark.usefixtures("kb_env")
def test_links_graph_log_and_vocabulary() -> None:
    a, b, c, d = (add(f"n{i}") for i in range(4))
    kb("link", str(a), "RELATED", str(b))
    again = kb("link", str(a), "related", str(b))
    assert again.output.strip().endswith("(already linked)")
    kb("link", str(b), "PART_OF", str(c))
    kb("link", str(d), "BLOCKS", str(c))
    bad = kb("link", str(a), "KNOWS", str(b), code=1)
    assert bad.output.strip() == (
        "Error: unknown relationship type 'KNOWS'; one of RELATED, SUPERSEDES, BLOCKS, "
        "OPENED_IN, TOUCHED, PART_OF"
    )
    assert kb("link", str(a), "RELATED", str(a), code=1).output.strip() == (
        "Error: a record cannot link to itself"
    )
    graph = kb("graph", str(a), "--depth", "2").output.splitlines()
    assert graph == [
        "hop 1:",
        f"  RELATED -> [{b}] n1",
        "hop 2:",
        f"  PART_OF -> [{c}] n2",
    ]
    deep = kb_json("graph", str(a), "--depth", "3")
    assert [(n["hop"], n["type"], n["direction"], n["id"]) for n in deep["nodes"]] == [
        (1, "RELATED", "->", b),
        (2, "PART_OF", "->", c),
        (3, "BLOCKS", "<-", d),
    ]
    assert kb("links", str(c)).output.splitlines() == [
        f"BLOCKS <- [{d}] n3",
        f"PART_OF <- [{b}] n1",
    ]
    assert events(str(a)) == ["linked", "created"]
    removed = kb("unlink", str(a), "related", str(b))
    assert removed.output.strip().endswith("removed")
    assert kb("unlink", str(a), "related", str(b)).output.strip().endswith("(no such link)")
    assert kb("links", str(a)).output == ""
    log = kb("log", str(a), "--limit", "1").output.splitlines()
    assert len(log) == 1 and "linked" in log[0] and '"removed": true' in log[0]


# --- done / skip / snooze ---------------------------------------------------------------


@pytest.mark.usefixtures("kb_env")
def test_done_plain_recurring_and_once() -> None:
    plain = str(add("Plain"))
    first = kb("done", plain, "finished it")
    assert first.output.strip() == f"[{plain}] Plain  · done"
    rec = kb_json("show", plain)
    assert rec["done_at"] is not None
    assert rec["events"][0]["note"] == "finished it"
    second = kb("done", plain)
    assert second.output.strip() == f"[{plain}] Plain  already done"
    assert events(plain) == ["done", "created"]
    assert plain not in kb("today", "--scope", "proj").output

    interval = str(add("Water plants", "--plan", "2026-09-14", "--repeat", "interval:10d"))
    result = kb("done", interval)
    assert result.output.strip() == f"[{interval}] Water plants  · next 2026-09-24"
    rec = kb_json("show", interval)
    assert (rec["due"], rec["done_at"]) == ("2026-09-24", None)

    taxes = str(
        add("Taxes", "--due", "2026-09-15", "--repeat", "calendar:FREQ=MONTHLY;BYMONTHDAY=15")
    )
    assert kb("done", taxes).output.strip().endswith("· next 2026-10-15")
    overdue = str(
        add("Overdue", "--due", "2026-09-01", "--repeat", "calendar:FREQ=MONTHLY;BYMONTHDAY=1")
    )
    assert kb("done", overdue).output.strip().endswith("· next 2026-10-01")

    once = str(add("Once", "--once"))
    kb("done", once)
    assert kb_json("show", once)["done_at"] is not None

    note = kb_json("add", "A note", "--kind", "note", "--scope", "proj")["id"]
    assert (
        kb("done", str(note), code=1).output.strip()
        == f"Error: [{note}] is a note; done applies to tasks"
    )


@pytest.mark.usefixtures("kb_env")
def test_window_skip_done_and_lazy_backlog(monkeypatch) -> None:
    # created on Monday 2026-09-14 with a Tuesday window: 09-15, 09-22, 09-29 ...
    tue = str(add("Tuesday chore", "--window", "FREQ=WEEKLY;BYDAY=TU", "--days", "any"))
    plain = str(add("Plain"))
    assert kb("skip", plain, code=1).output.strip() == (
        f"Error: [{plain}] is not a window task; skip applies to window tasks only"
    )
    assert (
        kb("skip", tue, code=1).output.strip() == f"Error: [{tue}] has no window before 2026-09-14"
    )

    monkeypatch.setenv("KB_TODAY", "2026-09-15")
    assert f"[{tue}] L2 normal  Tuesday chore  · window TU" in kb("today", "--scope", "proj").output
    skipped = kb("skip", tue)
    assert (
        skipped.output.strip()
        == f"[{tue}] Tuesday chore  · skipped window ending 2026-09-15, skips 1"
    )
    assert kb("skip", tue).output.strip().endswith("window already closed")
    assert tue not in kb("today", "--scope", "proj").output
    assert kb_json("show", tue)["urgency"]["reason"] == "skipped this window"

    # the next window (09-22) passes untouched; the standup on Thursday writes the skip lazily
    monkeypatch.setenv("KB_TODAY", "2026-09-24")
    kb("today", "--scope", "proj")
    rec = kb_json("show", tue)
    assert (rec["skips"], rec["backlog"]) == (2, True)
    skips = [e for e in kb_json("log", tue)["events"] if e["kind"] == "skip"]
    assert [e["payload"]["window"] for e in skips] == ["2026-09-22", "2026-09-15"]
    kb("today", "--scope", "proj")  # idempotent: no third skip
    assert len([e for e in kb_json("log", tue)["events"] if e["kind"] == "skip"]) == 2
    monkeypatch.setenv("KB_TODAY", "2026-09-29")
    assert tue not in kb("today", "--scope", "proj").output
    assert kb_json("show", tue)["urgency"]["reason"] == "backlog after 2 skips"

    # done clears the backlog, names the window it closed and keeps the task open
    done = kb("done", tue, "finally")
    assert done.output.strip() == f"[{tue}] Tuesday chore  · next 2026-10-06"
    rec = kb_json("show", tue)
    assert (rec["skips"], rec["backlog"], rec["done_at"]) == (0, False, None)
    assert rec["events"][0]["payload"] == {"window": "2026-09-29"}
    assert rec["urgency"]["reason"] == "done this window"
    monkeypatch.setenv("KB_TODAY", "2026-10-06")
    assert f"[{tue}] L2 normal" in kb("today", "--scope", "proj").output


@pytest.mark.usefixtures("kb_env")
def test_snooze_rules(monkeypatch) -> None:
    hard = str(add("Deadline", "--due", "2026-09-16"))
    refused = kb("snooze", hard, "2026-09-16", code=1)
    assert refused.output.strip() == "Error: cannot snooze past the deadline 2026-09-16"
    assert kb("snooze", hard, "2026-09-20", code=1).output.strip() == (
        "Error: cannot snooze past the deadline 2026-09-16"
    )
    ok = kb("snooze", hard)  # default tomorrow = 2026-09-15 < due
    assert ok.output.strip() == f"[{hard}] Deadline  · snoozed until 2026-09-15"
    assert kb_json("show", hard)["urgency"]["reason"] == "snoozed until 2026-09-15"
    assert hard not in kb("today", "--scope", "proj").output
    monkeypatch.setenv("KB_TODAY", "2026-09-15")
    assert f"[{hard}] L3 loud  Deadline  · T-1" in kb("today", "--scope", "proj").output
    monkeypatch.setenv("KB_TODAY", "2026-09-14")
    assert kb("snooze", hard, "2026-09-14", code=1).output.strip() == (
        "Error: snooze date must be after 2026-09-14"
    )
    assert kb("snooze", hard, "tomorrow-ish", code=2).output.count("expected YYYY-MM-DD") == 1

    aged = str(add("Aged", "--anchor", "2026-08-20"))
    assert f"[{aged}] L3 loud" in kb("today", "--scope", "proj").output
    kb("snooze", aged, "2026-09-21")
    assert kb_json("show", aged)["snoozed_until"] == "2026-09-21"
    assert aged not in kb("next", "--scope", "proj").output
    assert events(aged) == ["snooze", "created"]
    note = kb_json("add", "n", "--kind", "note", "--scope", "proj")["id"]
    assert "snooze applies to tasks" in kb("snooze", str(note), code=1).output


# --- today / next / why -----------------------------------------------------------------


@pytest.mark.usefixtures("kb_env")
def test_today_orders_filters_and_counts(kb_env, monkeypatch) -> None:
    scream = add("Overdue", "--due", "2026-09-10")
    loud = add("Soon", "--due", "2026-09-16")
    normal_soft = add("Planned", "--plan", "2026-09-20")
    whisper = add("Far", "--due", "2026-09-28")
    silent = add("Fresh")
    old = add("Old", "--anchor", "2026-08-20")
    glob = kb_json("add", "Global chore", "--scope", "global", "--anchor", "2026-08-20")["id"]
    personal = kb_json("add", "Personal one", "--scope", "personal", "--due", "2026-09-14")["id"]
    done = add("Finished", "--due", "2026-09-14")
    kb("done", str(done))

    result = kb("today", "--scope", "proj")
    assert result.output.splitlines() == [
        "kb today 2026-09-14 (proj)",
        f"[{scream}] L4 scream  Overdue  · T+4",
        f"[{old}] L3 loud  Old  · 15 working days since 2026-08-20",
        f"[{glob}] L3 loud  Global chore  · 15 working days since 2026-08-20",
        f"[{loud}] L3 loud  Soon  · T-2",
        f"[{normal_soft}] L2 normal  Planned  · T-6",
        "+1 quiet (whisper), kb next",
    ]
    assert str(personal) not in result.output
    assert str(silent) not in result.output

    nxt = kb("next", "--scope", "proj").output.splitlines()
    assert nxt[0] == "kb next 2026-09-14 (proj)"
    assert nxt[-1] == f"[{whisper}] L1 whisper  Far  · T-14"
    assert "quiet" not in nxt[-1]

    # personal shows with --scope personal, and from outside every scope path (no filter)
    assert (
        f"[{personal}] L4 scream  Personal one  · T-0" in kb("today", "--scope", "personal").output
    )
    everything = kb("today").output.splitlines()
    assert everything[0] == "kb today 2026-09-14 (all)"
    assert any(str(personal) in line for line in everything)
    assert any(str(scream) in line for line in everything)
    monkeypatch.chdir(kb_env.proj)
    inside = kb("today").output
    assert inside.startswith("kb today 2026-09-14 (proj)")
    assert str(personal) not in inside
    only_global = kb("today", "--scope", "global").output.splitlines()
    assert only_global == [
        "kb today 2026-09-14 (global)",
        f"[{glob}] L3 loud  Global chore  · 15 working days since 2026-08-20",
    ]

    as_json = kb_json("today", "--scope", "proj")
    assert as_json["date"] == MONDAY and as_json["scope"] == "proj" and as_json["odoo"] is None
    assert [t["id"] for t in as_json["tasks"]] == [scream, old, glob, loud, normal_soft]
    assert as_json["quiet"] == 1
    assert as_json["tasks"][0]["name"] == "scream"


@pytest.mark.usefixtures("kb_env")
def test_today_record_shown_writes_events_and_closes_once_tasks(monkeypatch) -> None:
    once = add("Tell me once", "--once")
    loud = add("Soon", "--due", "2026-09-16")
    quiet = add("Far", "--due", "2026-09-28")
    plain = kb("today", "--scope", "proj")
    assert f"[{once}] L2 normal  Tell me once  · once" in plain.output
    assert events(str(once)) == ["created"]

    monkeypatch.setenv("KB_SESSION", "sess-1")
    shown = kb("today", "--scope", "proj", "--record-shown")
    assert f"[{once}] L2 normal  Tell me once  · once" in shown.output
    assert events(str(once)) == ["done", "shown", "created"]
    once_rec = kb_json("show", str(once))
    assert once_rec["done_at"] is not None
    assert once_rec["events"][0]["note"] == "shown"
    assert once_rec["events"][0]["session"] == "sess-1"
    assert events(str(loud)) == ["shown", "created"]
    assert events(str(quiet)) == ["created"]  # whisper is not printed, so not shown
    after = kb("today", "--scope", "proj")
    assert str(once) not in after.output
    kb("next", "--scope", "proj")
    assert events(str(quiet)) == ["created"]


@pytest.mark.usefixtures("kb_env")
def test_today_never_fails(kb_env, tmp_path, monkeypatch) -> None:
    broken = tmp_path / "broken.toml"
    broken.write_text("[neo4j]\nbolt_port = 1\n")
    monkeypatch.setenv("KB_CONFIG", str(broken))
    result = kb("today")
    assert result.stdout == ""
    assert result.stderr.startswith("kb today: ")
    invalid = tmp_path / "invalid.toml"
    invalid.write_text("[nowhere]\nx = 1\n")
    monkeypatch.setenv("KB_CONFIG", str(invalid))
    assert kb("today").stderr.strip() == "kb today: unknown key: nowhere"
    assert kb("next").stderr.strip() == "kb next: unknown key: nowhere"


@pytest.mark.usefixtures("kb_env")
def test_why_spells_out_the_computation() -> None:
    rid = str(add("Taxes", "--due", "2026-09-16", "--escalation", '{"loud": 3}'))
    lines = kb("why", rid).output.splitlines()
    assert lines[0] == f"[{rid}] Taxes"
    assert lines[1] == "schedule: hard due 2026-09-16 days=work"
    assert lines[2] == "today: 2026-09-14 (Monday, working day)"
    assert lines[3] == (
        "thresholds: whisper 14 / normal 7 / loud 3 calendar days before due (task override (loud))"
    )
    assert lines[4] == "due: 2026-09-16, T-2"
    assert lines[5] == "snooze: none"
    assert lines[6] == "before day fit: L3 loud — T-2"
    assert lines[7] == "day fit: days=work on a Monday, working day: fits"
    assert lines[8] == "result: L3 loud — T-2"
    as_json = kb_json("why", rid)
    assert (as_json["level"], as_json["name"]) == (3, "loud")
    note = kb_json("add", "n", "--kind", "note", "--scope", "proj")["id"]
    assert "why applies to tasks" in kb("why", str(note), code=1).output


# --- archive / supersede / merge / promote ----------------------------------------------


@pytest.mark.usefixtures("kb_env")
def test_archive_supersede_merge_promote() -> None:
    old = add("Old howto", "--kind", "howto", "--body", "obsolete recipe")
    new = add("New howto", "--kind", "howto", "--body", "fresh recipe")
    kb("supersede", str(old), str(new))
    assert kb("links", str(new)).output.strip() == f"SUPERSEDES -> [{old}] Old howto  (old_howto)"
    assert kb_json("show", str(old))["archived_at"] is not None
    assert events(str(old)) == ["superseded", "created"]
    assert events(str(new)) == ["superseded", "created"]
    assert str(old) not in kb("search", "recipe").output
    assert str(old) in kb("search", "recipe", "--all").output

    dup = add("Dup task", "--due", "2026-09-30")
    keep = add("Keep task")
    third = add("Third")
    kb("link", str(dup), "blocks", str(third))
    kb("link", str(third), "part_of", str(dup))
    kb("link", str(dup), "related", str(keep))  # an edge between the two is dropped, not moved
    kb("append", str(dup), "history")
    merged = kb("merge", str(dup), str(keep))
    assert (
        merged.output.strip()
        == f"[{dup}] Dup task  merged into  [{keep}] Keep task  (2 edges moved)"
    )
    assert kb("links", str(keep)).output.splitlines() == [
        f"BLOCKS -> [{third}] Third",
        f"PART_OF <- [{third}] Third",
    ]
    assert kb("links", str(dup)).output == ""
    assert events(str(keep)) == ["merged", "edited", "linked", "linked", "created"]
    assert events(str(dup)) == ["archived", "created"]  # DUP keeps its own created event
    dup_rec = kb_json("show", str(dup))
    assert dup_rec["archived_at"] is not None
    assert dup_rec["events"][0]["note"] == f"merged into [{keep}]"
    assert str(dup) not in kb("today", "--scope", "proj").output
    assert (
        kb("merge", str(keep), str(keep), code=1).output.strip()
        == "Error: DUP and KEEP are the same record"
    )

    archived = kb("archive", str(third), "--reason", "not needed")
    assert archived.output.strip() == f"[{third}] Third  · archived"
    assert kb_json("log", str(third))["events"][0]["note"] == "not needed"
    again = kb("archive", str(third), "--reason", "twice")
    assert again.output.strip() == f"[{third}] Third  already archived"
    assert events(str(third)) == ["archived", "linked", "created"]  # no second archived
    assert kb("archive", str(third), code=2).output.count("--reason") >= 1

    promoted = kb("promote", str(keep), "KIO-1700")
    assert promoted.output.strip() == f"[{keep}] Keep task  · odoo KIO-1700"
    rec = kb_json("show", str(keep))
    assert (rec["source"], rec["external_id"]) == ("odoo", "KIO-1700")
    assert rec["events"][0]["kind"] == "promoted" and rec["events"][0]["note"] == "KIO-1700"
    note = kb("promote", str(new), "KIO-1701", code=1)
    assert note.output.strip() == (
        f"Error: [{new}] is a howto; promote is for tasks, notes/howtos link with kb link"
    )
    assert kb_json("show", str(new))["source"] == "manual"


# --- index ------------------------------------------------------------------------------


HEADER = (
    "# kb index 2026-09-14 (proj) — generated, do not edit; write with kb add/append, "
    "read with kb show <id|slug>, search with kb search"
)


@pytest.mark.usefixtures("kb_env")
def test_index_empty_pinned_recent_and_budget() -> None:
    assert kb("index", "--scope", "proj").output.splitlines() == [HEADER]
    add("A task that never shows")
    assert kb("index", "--scope", "proj").output.splitlines() == [HEADER]
    fb = kb_json("add", "Verify facts", "--kind", "feedback", "--scope", "global")
    howto = kb_json("add", "Deploy recipe", "--kind", "howto", "--scope", "proj")
    note = kb_json("add", "Some note", "--kind", "note", "--scope", "proj")
    personal = kb_json("add", "Private", "--kind", "note", "--scope", "personal")
    other = kb_json("add", "Elsewhere", "--kind", "note", "--scope", "global")
    kb("append", str(howto["id"]), "touched later")  # most recently updated comes first
    out = kb("index", "--scope", "proj").output.splitlines()
    assert out == [
        HEADER,
        "",
        "## Pinned",
        f"- [{fb['id']}] verify_facts — Verify facts",
        "",
        "## Recent",
        f"- [{howto['id']}] deploy_recipe — Deploy recipe",
        f"- [{other['id']}] elsewhere — Elsewhere",
        f"- [{note['id']}] some_note — Some note",
    ]
    assert str(personal["id"]) not in "\n".join(out)
    cut = kb("index", "--scope", "proj", "--budget", "7").output.splitlines()
    assert len(cut) == 7 and cut[-1] == f"- [{howto['id']}] deploy_recipe — Deploy recipe"
    kb("archive", str(note["id"]), "--reason", "gone")
    assert str(note["id"]) not in kb("index", "--scope", "proj").output
    everything = kb("index").output
    assert everything.startswith("# kb index 2026-09-14 (all)")
    assert str(personal["id"]) in everything


def test_help_of_every_new_command() -> None:
    runner = CliRunner()
    for name in (
        "add",
        "append",
        "edit",
        "show",
        "search",
        "links",
        "graph",
        "log",
        "done",
        "skip",
        "snooze",
        "today",
        "next",
        "why",
        "archive",
        "supersede",
        "merge",
        "link",
        "unlink",
        "promote",
        "sync",
        "index",
        "migrate",
    ):
        result = runner.invoke(main, [name, "--help"])
        assert result.exit_code == 0, (name, result.output)
    assert runner.invoke(main, ["sync", "odoo", "--help"]).exit_code == 0
    assert runner.invoke(main, ["migrate", "state", "--help"]).exit_code == 0
    assert runner.invoke(main, ["migrate", "nodes", "--help"]).exit_code == 0


# --- QC round 1 -------------------------------------------------------------------------


@pytest.mark.usefixtures("kb_env")
def test_done_twice_on_the_same_day_advances_once() -> None:
    water = str(add("Water plants", "--plan", "2026-09-14", "--repeat", "interval:10d"))
    first = kb("done", water)
    assert first.output.strip().endswith("· next 2026-09-24")
    second = kb("done", water, "again")
    assert second.output.strip() == f"[{water}] Water plants  already done"
    rec = kb_json("show", water)
    assert rec["due"] == "2026-09-24"
    assert events(water) == ["done", "created"]


@pytest.mark.usefixtures("kb_env")
def test_exhausted_recurrence_closes_the_task() -> None:
    last = str(
        add(
            "Last time",
            "--due",
            "2026-09-15",
            "--repeat",
            "calendar:FREQ=MONTHLY;BYMONTHDAY=15;COUNT=1",
        )
    )
    result = kb("done", last)
    assert result.output.strip() == f"[{last}] Last time  · done"
    rec = kb_json("show", last)
    assert rec["done_at"] is not None
    assert rec["events"][0]["note"] == "recurrence exhausted"
    noted = str(
        add(
            "Noted",
            "--due",
            "2026-09-15",
            "--repeat",
            "calendar:FREQ=MONTHLY;BYMONTHDAY=15;COUNT=1",
        )
    )
    kb("done", noted, "paid")
    assert kb_json("show", noted)["events"][0]["note"] == "paid (recurrence exhausted)"


@pytest.mark.usefixtures("kb_env")
def test_snoozed_window_task_collects_no_skips(monkeypatch) -> None:
    laundry = str(add("Laundry", "--window", "weekend", "--days", "any"))
    kb("snooze", laundry, "2026-09-28")
    for saturday in ("2026-09-19", "2026-09-26"):
        monkeypatch.setenv("KB_TODAY", saturday)
        assert laundry not in kb("today", "--scope", "proj").output
    rec = kb_json("show", laundry)
    assert (rec["skips"], rec["backlog"]) == (0, False)
    monkeypatch.setenv("KB_TODAY", "2026-09-28")  # the snooze ends; last weekend was inside it
    kb("today", "--scope", "proj")
    rec = kb_json("show", laundry)
    assert (rec["skips"], rec["backlog"]) == (0, False)
    assert [e["kind"] for e in rec["events"]] == ["snooze", "created"]
    monkeypatch.setenv("KB_TODAY", "2026-10-03")  # first Saturday after the snooze
    assert (
        f"[{laundry}] L2 normal  Laundry  · window SA,SU" in kb("today", "--scope", "proj").output
    )


@pytest.mark.usefixtures("kb_env")
def test_edit_resets_skips_and_backlog(monkeypatch) -> None:
    tue = str(add("Tuesday chore", "--window", "FREQ=WEEKLY;BYDAY=TU", "--days", "any"))
    monkeypatch.setenv("KB_TODAY", "2026-09-24")
    kb("today", "--scope", "proj")
    assert kb_json("show", tue)["skips"] == 1
    kb("edit", tue, "--title", "Tuesday chore, renamed")
    rec = kb_json("show", tue)
    assert (rec["skips"], rec["backlog"], rec["title"]) == (0, False, "Tuesday chore, renamed")
    assert rec["events"][0]["payload"]["before"] == {"title": "Tuesday chore"}


@pytest.mark.usefixtures("kb_env")
def test_invalid_rrule_on_one_task_does_not_kill_today() -> None:
    broken = str(add("Broken window", "--window", "weekend", "--days", "any"))
    loud = add("Soon", "--due", "2026-09-16")
    kb(
        "query",
        "MATCH (t:Record {id: $id}) SET t.window_rrule = 'FREQ=BOGUS'",
        "-p",
        f"id={broken}",
    )
    out = kb("today", "--scope", "proj").output
    assert f"[{loud}] L3 loud  Soon  · T-2" in out
    assert broken not in out
    shown = kb("show", broken).output
    assert "urgency: L0 silent — invalid rrule: " in shown
    assert any(line.startswith("rrule: invalid") for line in kb("why", broken).output.splitlines())
    assert kb("skip", broken, code=1).output.startswith(f"Error: [{broken}] has an invalid rrule")


@pytest.mark.usefixtures("kb_env")
def test_once_is_shown_on_a_weekend_and_can_be_snoozed(monkeypatch) -> None:
    once = str(add("Tell me once", "--once"))  # days=work by scope
    monkeypatch.setenv("KB_TODAY", "2026-09-19")  # Saturday
    assert f"[{once}] L2 normal  Tell me once  · once" in kb("today", "--scope", "proj").output
    kb("snooze", once, "2026-09-21")
    assert once not in kb("today", "--scope", "proj").output
    monkeypatch.setenv("KB_TODAY", "2026-09-21")
    assert once in kb("today", "--scope", "proj").output


@pytest.mark.usefixtures("kb_env")
def test_index_budget_covers_pinned_lines_too() -> None:
    for n in range(3):
        kb_json("add", f"Rule {n}", "--kind", "feedback", "--scope", "global")
    kb_json("add", "A note", "--kind", "note", "--scope", "proj")
    full = kb("index", "--scope", "proj").output.splitlines()
    assert len(full) == 9  # header, blank, ## Pinned, 3 rules, blank, ## Recent, 1 note
    cut = kb("index", "--scope", "proj", "--budget", "4").output.splitlines()
    assert len(cut) == 4
    assert cut[2] == "## Pinned" and cut[3].endswith("— Rule 0")
    assert kb("index", "--budget", "0", code=2).output.count("0 is not in the range") == 1


def test_commands_fail_fast_when_bolt_is_closed(tmp_path, monkeypatch) -> None:
    config = tmp_path / "closed.toml"
    config.write_text("[neo4j]\nbolt_port = 1\n")
    monkeypatch.setenv("KB_CONFIG", str(config))
    monkeypatch.setenv("KB_NEO4J_AUTH", str(tmp_path / "none"))
    expected = "neo4j-kb is not reachable on bolt://127.0.0.1:1: kb status / kb service start"
    started = time.monotonic()
    result = kb("show", "1", code=1)
    assert time.monotonic() - started < 5
    assert result.output.strip() == f"Error: {expected}"
    assert kb("today").stderr.strip() == f"kb today: {expected}"
    assert kb("next").stderr.strip() == f"kb next: {expected}"
    assert json.loads(kb("--json", "today").stdout) == {"error": expected, "tasks": []}
    index = kb("index")
    assert index.stdout.startswith("# kb index ") and index.stdout.count("\n") == 1
    assert index.stderr.strip() == f"kb index: {expected}"
    assert kb("query", "RETURN 1", code=1).output.strip() == f"Error: {expected}"


@pytest.mark.usefixtures("kb_env")
def test_queries_on_unwritten_properties_raise_no_driver_notifications(caplog) -> None:
    with caplog.at_level(logging.DEBUG, logger="neo4j.notifications"):
        result = kb("today", "--scope", "proj")  # archived_at, done_at: no node has them yet
        assert result.stderr == ""
        rid = str(add("Lonely"))
        assert kb("links", rid).stderr == ""  # no RELATED/BLOCKS/... edge exists yet
        assert kb("search", "lonely").stderr == ""
    assert [r for r in caplog.records if r.name == "neo4j.notifications"] == []


# --- summary and index kinds (phase 2) --------------------------------------------------


@pytest.mark.usefixtures("kb_env")
def test_summary_on_add_edit_show_and_index(monkeypatch) -> None:
    note = kb_json(
        "add",
        "Deploy recipe",
        "--kind",
        "howto",
        "--scope",
        "proj",
        "--summary",
        "  how a merged PR  reaches prod ",
    )
    assert note["summary"] == "how a merged PR reaches prod"
    rid = str(note["id"])
    out = kb("show", rid).output.splitlines()
    assert out[0] == f"[{rid}] Deploy recipe"
    assert out[1] == "how a merged PR reaches prod"
    assert out[2].startswith("kind: howto")
    assert kb("index", "--scope", "proj").output.splitlines()[-1] == (
        f"- [{rid}] deploy_recipe — how a merged PR reaches prod"
    )
    monkeypatch.setattr("click.edit", lambda *_a, **_kw: pytest.fail("editor opened"))
    kb("edit", rid, "--summary", "the deploy skill, step by step")
    rec = kb_json("show", rid)
    assert rec["summary"] == "the deploy skill, step by step"
    assert rec["events"][0]["payload"] == {
        "fields": ["summary"],
        "before": {"summary": "how a merged PR reaches prod"},
    }
    same = kb("edit", rid, "--summary", "the deploy skill, step by step")
    assert same.output.strip().endswith("unchanged")
    kb("edit", rid, "--summary", "")  # an empty summary clears it
    assert kb_json("show", rid)["summary"] is None
    assert kb("show", rid).output.splitlines()[1].startswith("kind: howto")
    assert kb("index", "--scope", "proj").output.splitlines()[-1] == (
        f"- [{rid}] deploy_recipe — Deploy recipe"
    )
    assert kb_json("add", "No summary", "--kind", "note", "--scope", "proj")["summary"] is None
    long = kb_json("add", "Long", "--kind", "note", "--scope", "proj", "--summary", "word " * 50)
    lines = kb("index", "--scope", "proj").output.splitlines()
    assert lines[3] == f"- [{long['id']}] long — {('word ' * 32).rstrip()}…"  # newest recent, cut
    assert kb_json("show", str(long["id"]))["summary"] == ("word " * 50).strip()  # stored whole


@pytest.mark.usefixtures("kb_env")
def test_index_kinds() -> None:
    task = add("A task")
    note = kb_json("add", "A note", "--kind", "note", "--scope", "proj")
    decision = kb_json("add", "A decision", "--kind", "decision", "--scope", "proj")
    default = kb("index", "--scope", "proj").output
    assert f"[{note['id']}]" in default and f"[{decision['id']}]" in default
    assert f"[{task}]" not in default
    only_tasks = kb("index", "--scope", "proj", "--kinds", "task").output.splitlines()
    assert only_tasks[-1] == f"- [{task}] - — A task"
    assert f"[{note['id']}]" not in "\n".join(only_tasks)
    two = kb("index", "--scope", "proj", "--kinds", "note, decision").output
    assert f"[{note['id']}]" in two and f"[{decision['id']}]" in two
    assert "unknown kind 'bogus'" in kb("index", "--kinds", "note,bogus", code=2).output
    assert "at least one kind" in kb("index", "--kinds", ",", code=2).output


# --- scope view (QC round 2) ------------------------------------------------------------


def test_view_scope_hides_records_of_other_scopes(kb_env, monkeypatch) -> None:
    glob = kb_json("add", "Style rules", "--kind", "feedback", "--scope", "global")
    private = kb_json("add", "CV repo", "--kind", "note", "--scope", "personal")
    note = kb_json("add", "Project note", "--kind", "note", "--scope", "proj")
    task = kb_json("add", "Private task", "--scope", "personal")
    gid, pid = str(glob["id"]), str(private["id"])
    kb("link", gid, "related", pid)  # outside every scope path: everything is in view
    kb("link", gid, "related", str(note["id"]))
    refused = (
        f"Error: not visible in this scope: [{pid}] is in scope personal; "
        "rerun with --scope personal"
    )

    monkeypatch.chdir(kb_env.proj)
    out = kb("links", gid).output
    assert "CV repo" not in out and f"RELATED -> [{note['id']}] Project note" in out
    assert out.splitlines()[-1] == "1 linked records in other scopes (hidden)"
    assert kb_json("links", gid)["hidden_links"] == 1
    graph = kb("graph", gid).output
    assert (
        "CV repo" not in graph
        and graph.splitlines()[-1] == "1 linked records in other scopes (hidden)"
    )
    shown = kb("show", gid).output
    assert "CV repo" not in shown and "  1 linked records in other scopes (hidden)" in shown
    assert kb_json("show", gid)["hidden_links"] == 1
    for args in (
        ["show", pid],
        ["show", "cv_repo"],
        ["links", pid],
        ["graph", pid],
        ["log", pid],
        ["append", pid, "x"],
        ["edit", pid, "--title", "x"],
        ["archive", pid, "--reason", "x"],
        ["link", gid, "related", pid],
        ["unlink", gid, "related", pid],
        ["merge", pid, gid],
        ["supersede", pid, gid],
        ["promote", pid, "KIO-1"],
    ):
        result = kb(*args, code=1)
        assert result.output.strip() == refused, args
        assert "CV repo" not in result.output
    tid = str(task["id"])
    for args in (["done", tid], ["snooze", tid], ["skip", tid], ["why", tid]):
        assert (
            kb(*args, code=1)
            .output.strip()
            .startswith(f"Error: not visible in this scope: [{tid}] is in scope personal")
        ), args
    assert (
        kb("add", "linked", "--scope", "proj", "--link", f"related:{pid}", code=1).output.strip()
        == refused
    )
    # nothing above wrote anything (the log itself needs the personal view from here)
    assert [e["kind"] for e in kb_json("log", pid, "--scope", "personal")["events"]] == ["created"]

    # --scope personal opens the view: personal + global, the project note is the hidden one
    assert kb("show", pid, "--scope", "personal").output.splitlines()[0] == f"[{pid}] CV repo"
    with_scope = kb("links", gid, "--scope", "personal").output
    assert f"RELATED -> [{pid}] CV repo" in with_scope and "Project note" not in with_scope
    assert with_scope.splitlines()[-1] == "1 linked records in other scopes (hidden)"
    kb("edit", pid, "--title", "CV repo (git)", "--scope", "personal")
    assert kb("done", tid, "--scope", "personal").exit_code == 0
    # a project record from another project's cwd names that project
    assert kb("show", str(note["id"]), "--scope", "global", code=1).output.strip() == (
        f"Error: not visible in this scope: [{note['id']}] is in scope proj; "
        "rerun with --scope proj"
    )
    assert kb("show", "--help").output.count("--scope") == 1
