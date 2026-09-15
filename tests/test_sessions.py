"""Session records: open/close/note/current/list, the current-session rule, the OPENED_IN and
TOUCHED edges every writing command adds, show's grouping and today's no-summary line."""

import json
from typing import Any

import pytest
from click.testing import CliRunner, Result

from kb.cli import main

MONDAY = "2026-09-14"  # KB_TODAY in kb_env


def kb(*args: str, code: int = 0, stdin: str | None = None) -> Result:
    result = CliRunner().invoke(main, list(args), input=stdin)
    assert result.exit_code == code, result.output
    return result


def kb_json(*args: str) -> Any:
    return json.loads(kb("--json", *args).stdout)


def add(*args: str) -> int:
    return kb_json("add", *args, "--scope", "proj")["id"]


def open_session(session_id: str, scope: str = "proj", source: str = "startup") -> Any:
    return kb_json("session", "open", "--scope", scope, "--id", session_id, "--source", source)


def links(ref: str) -> list[tuple[str, str, int]]:
    return sorted((lk["type"], lk["direction"], lk["id"]) for lk in kb_json("links", ref)["links"])


def session_of(record_id: int) -> str | None:
    """The slug of the session the record was opened in, None without one."""
    for lk in kb_json("links", str(record_id))["links"]:
        if lk["type"] == "OPENED_IN":
            return lk["slug"]
    return None


def last_event_session(ref: str) -> str | None:
    return kb_json("log", ref)["events"][0]["session"]


# --- open -------------------------------------------------------------------------------


@pytest.mark.usefixtures("kb_env")
def test_open_numbers_by_day_and_is_idempotent_on_the_id() -> None:
    first = kb("session", "open", "--scope", "proj", "--id", "abc", "--source", "startup")
    assert first.output.strip().endswith("] 2026-09-14-1")
    again = open_session("abc", source="compact")
    assert (again["state"], again["slug"], again["source"]) == ("open", "2026-09-14-1", "startup")
    assert open_session("def")["slug"] == "2026-09-14-2"
    # slugs are unique across scopes: the first personal session of the day is number 3
    assert open_session("ghi", scope="personal")["slug"] == "2026-09-14-3"
    assert open_session("jkl")["slug"] == "2026-09-14-4"
    rec = kb_json("show", "2026-09-14-1")
    assert (rec["kind"], rec["scope"], rec["title"]) == (
        "session",
        "proj",
        "session 2026-09-14 1 (startup)",
    )
    assert (rec["session_id"], rec["source"], rec["closed_at"], rec["no_summary"]) == (
        "abc",
        "startup",
        None,
        False,
    )
    assert rec["opened_at"] == rec["created_at"]
    assert [(e["kind"], e["note"], e["session"]) for e in rec["events"]] == [
        ("opened", "startup", "abc"),
        ("created", None, "abc"),
    ]
    assert kb("show", "2026-09-14-1").output.splitlines()[1] == (
        "kind: session  scope: proj  slug: 2026-09-14-1  tags: -  pinned: no  source: startup"
    )
    assert kb("show", "2026-09-14-1").output.splitlines()[2].startswith("session: abc  opened: ")
    assert kb("show", "2026-09-14-1").output.splitlines()[2].endswith("  open")


@pytest.mark.usefixtures("kb_env")
def test_open_needs_a_scope_and_add_refuses_the_kind() -> None:
    outside = kb("session", "open", "--id", "z", "--source", "startup", code=2)
    assert "outside every configured scope, pass --scope" in outside.output
    assert kb("session", "open", "--scope", "proj", "--id", "z", code=2).exit_code == 2
    bad = kb("session", "open", "--scope", "proj", "--id", "z", "--source", "migrated", code=2)
    assert "migrated" in bad.output
    refused = kb("add", "x", "--kind", "session", "--scope", "proj", code=2)
    assert "session" in refused.output


# --- close / note / current / list ------------------------------------------------------


def test_close_sets_state_and_no_summary(kb_env, tmp_path) -> None:
    open_session("abc")
    closed = kb("session", "close", "--id", "abc", "--reason", "exit")
    assert closed.output.strip().endswith("] 2026-09-14-1  · closed, no summary")
    rec = kb_json("show", "2026-09-14-1")
    assert rec["closed_at"] is not None and rec["reason"] == "exit" and rec["no_summary"] is True
    assert [e["kind"] for e in rec["events"]] == ["closed", "opened", "created"]
    assert rec["events"][0]["note"] == "exit"
    state_line = kb("show", "2026-09-14-1").output.splitlines()[2]
    assert state_line.startswith("session: abc  opened: ") and "  closed: " in state_line
    assert state_line.endswith("  reason: exit  no summary")

    again = kb("session", "close", "--id", "abc")
    assert again.output.strip().endswith("] 2026-09-14-1  already closed")
    assert kb_json("show", "2026-09-14-1")["closed_at"] == rec["closed_at"]

    body = tmp_path / "body.md"
    body.write_text("**Intent:** build phase 3\n\n### Done\n- sessions\n")
    later = kb(
        "session", "close", "--id", "abc", "--body-file", str(body), "--title", "Build phase 3"
    )
    assert later.output.strip().endswith("] 2026-09-14-1  · updated")
    rec = kb_json("show", "2026-09-14-1")
    assert (rec["no_summary"], rec["title"]) == (False, "Build phase 3")
    assert rec["body"] == body.read_text()
    assert [e["kind"] for e in rec["events"]] == ["edited", "closed", "opened", "created"]
    assert rec["events"][0]["payload"]["fields"] == ["body", "title"]
    # the same body again changes nothing
    assert kb("session", "close", "--id", "abc", "--body-file", str(body)).output.endswith(
        "  already closed\n"
    )
    # a resume re-opens the record: closed_at goes, the source follows, an opened event
    reopened = open_session("abc", source="resume")
    assert (reopened["state"], reopened["closed_at"], reopened["source"]) == (
        "reopened",
        None,
        "resume",
    )
    assert [e["kind"] for e in kb_json("log", "2026-09-14-1")["events"]][:2] == [
        "opened",
        "edited",
    ]
    assert (
        kb("session", "close", "--id", "abc", "--reason", "exit")
        .output.strip()
        .endswith("  · closed")
    )  # the body written earlier stays: no "no summary"
    assert kb_json("show", "2026-09-14-1")["no_summary"] is False

    open_session("xyz")
    direct = kb("session", "close", "--id", "xyz", "--body", "-", stdin="summary from stdin\n")
    assert direct.output.strip().endswith("] 2026-09-14-2  · closed")
    xyz = kb_json("show", "2026-09-14-2")
    assert (xyz["no_summary"], xyz["body"], xyz["reason"]) == (False, "summary from stdin\n", None)
    assert kb("session", "close", "--id", "nope", code=1).output.strip() == "Error: no session nope"
    long_title = "x" * 121
    assert (
        "longer than 120"
        in kb("session", "close", "--id", "xyz", "--title", long_title, code=1).output
    )


def test_note_current_and_list(kb_env, monkeypatch, tmp_path) -> None:
    assert kb("session", "current", code=1).output.strip() == "Error: no open session"
    assert kb("session", "note", "x", code=1).output.strip() == "Error: no open session"
    assert kb("session", "list", "--scope", "proj").output == ""
    open_session("abc")
    current = kb("session", "current").output.strip()
    assert current.startswith("[") and "] 2026-09-14-1 (open since " in current
    assert kb_json("session", "current")["session_id"] == "abc"

    kb("session", "note", "worked on the parser")  # the current session, no --id
    rec = kb_json("show", "2026-09-14-1")
    assert rec["body"] == f"**{MONDAY}:** worked on the parser"
    assert [e["kind"] for e in rec["events"]] == ["edited", "opened", "created"]
    assert rec["events"][0]["payload"] == {
        "append": True,
        "before": {"body": ""},
        "fields": ["body"],
    }
    note = tmp_path / "note.md"
    note.write_text("from a file\n")
    kb("session", "note", "--body-file", str(note))
    assert kb_json("show", "2026-09-14-1")["body"].endswith(f"\n\n**{MONDAY}:** from a file")
    assert "one of them" in kb("session", "note", code=2).output
    assert "one of them" in kb("session", "note", "t", "--body-file", str(note), code=2).output

    listing = kb("session", "list", "--scope", "proj").output.splitlines()
    assert len(listing) == 1
    assert "] 2026-09-14-1  session 2026-09-14 1 (startup)  · opened " in listing[0]
    assert listing[0].endswith("  open  · 0 created / 0 touched")
    kb("session", "close", "--id", "abc")
    assert kb("session", "list", "--scope", "proj", "--open").output == ""
    assert "closed" in kb("session", "list", "--scope", "proj").output
    assert kb("session", "current", code=1).output.strip() == "Error: no open session"
    # a note written after the close clears "no summary"
    open_session("bare")
    kb("session", "close", "--id", "bare")
    assert kb_json("show", "2026-09-14-2")["no_summary"] is True
    kb("session", "note", "--id", "bare", "written later")
    assert kb_json("show", "2026-09-14-2")["no_summary"] is False
    as_json = kb_json("session", "list", "--scope", "proj", "--limit", "1")
    assert [s["slug"] for s in as_json["sessions"]] == ["2026-09-14-2"]
    assert (as_json["sessions"][0]["opened_in"], as_json["sessions"][0]["touched"]) == (0, 0)
    # KB_SESSION pointing at a closed session: current names it as closed
    monkeypatch.setenv("KB_SESSION", "abc")
    assert "] 2026-09-14-1 (closed " in kb("session", "current").output
    monkeypatch.setenv("KB_SESSION", "ghost")
    assert kb("session", "current", code=1).output.strip() == "Error: no open session"


# --- the current-session rule and the edges ---------------------------------------------


def test_writes_link_records_to_the_env_session(kb_env, monkeypatch) -> None:
    # KB_SESSION set but no such record: events carry the id, no edge, no error
    monkeypatch.setenv("KB_SESSION", "ghost")
    orphan = add("Orphan")
    assert links(str(orphan)) == []
    assert last_event_session(str(orphan)) == "ghost"
    monkeypatch.delenv("KB_SESSION")
    lonely = add("Lonely")  # no session anywhere: no edge either
    assert links(str(lonely)) == []
    assert last_event_session(str(lonely)) is None

    sess = open_session("s1")["id"]
    monkeypatch.setenv("KB_SESSION", "s1")
    task = add("Created here")
    assert links(str(task)) == [("OPENED_IN", "->", sess)]
    kb("done", str(orphan))
    kb("append", str(lonely), "more")
    assert links(str(orphan)) == [("TOUCHED", "<-", sess)]
    assert last_event_session(str(orphan)) == "s1"
    # an edit of the record created here adds TOUCHED beside OPENED_IN; a second one nothing
    kb("edit", str(task), "--title", "Renamed")
    kb("edit", str(task), "--title", "Renamed again")
    assert links(str(task)) == [("OPENED_IN", "->", sess), ("TOUCHED", "<-", sess)]
    # a no-op write (done twice, unchanged edit) adds nothing
    kb("done", str(orphan))
    kb("edit", str(lonely), "--title", "Lonely")
    assert (
        kb("session", "list", "--scope", "proj")
        .output.strip()
        .endswith("  open  · 1 created / 3 touched")
    )
    shown = kb("show", "2026-09-14-1").output
    assert f"\ncreated here:\n  [{task}] Renamed again\n" in shown
    assert (
        f"\ntouched here:\n  [{orphan}] Orphan\n  [{lonely}] Lonely\n  [{task}] Renamed again"
        in shown
    )
    assert "\nlinks:\n" not in shown
    # the session touching itself is not an edge
    kb("append", "2026-09-14-1", "a note through append")
    kb("edit", "2026-09-14-1", "--title", "Renamed session")
    assert all(lk["id"] != sess for lk in kb_json("links", "2026-09-14-1")["links"])
    # a plain record shows its session as an ordinary link line
    assert f"OPENED_IN -> [{sess}] Renamed session  (2026-09-14-1)" in kb("links", str(task)).output


def test_every_writing_command_touches(kb_env, monkeypatch) -> None:
    sess = open_session("s1")["id"]
    monkeypatch.setenv("KB_SESSION", "s1")
    window = add("Laundry", "--window", "weekend")
    hard = add("Taxes", "--due", "2026-09-30")
    old = add("Old", "--kind", "howto")
    new = add("New", "--kind", "howto")
    dup = add("Dup")
    keep = add("Keep")
    note = add("A note", "--kind", "note")
    monkeypatch.setenv("KB_TODAY", "2026-09-21")  # the weekend window ended untouched
    kb("skip", str(window))
    kb("snooze", str(hard), "2026-09-25")
    kb("archive", str(note), "--reason", "done with it")
    kb("supersede", str(old), str(new))
    kb("merge", str(dup), str(keep))
    kb("link", str(keep), "blocks", str(hard))
    kb("unlink", str(keep), "blocks", str(hard))
    kb("promote", str(hard), "KIO-1")
    for rid in (window, hard, old, new, dup, keep, note):
        assert ("TOUCHED", "<-", sess) in links(str(rid)), rid
    # merge moved dup's OPENED_IN onto keep, which already had one: 7 records, 6 created-here
    assert session_of(dup) is None and session_of(keep) == "2026-09-14-1"
    assert (
        kb("session", "list", "--scope", "proj")
        .output.strip()
        .endswith("  open  · 6 created / 7 touched")
    )
    # no-ops touch nothing: a second skip of the same window, unlink of a missing edge
    kb("session", "open", "--scope", "proj", "--id", "s2", "--source", "startup")
    monkeypatch.setenv("KB_SESSION", "s2")
    assert "already closed" in kb("skip", str(window)).output
    assert "no such link" in kb("unlink", str(keep), "blocks", str(hard)).output
    assert "already archived" in kb("archive", str(note), "--reason", "again").output
    assert (
        kb("session", "list", "--scope", "proj", "--open")
        .output.splitlines()[0]
        .endswith("  open  · 0 created / 0 touched")
    )


def test_latest_open_session_in_the_view_is_the_default(kb_env, monkeypatch) -> None:
    open_session("s1")
    open_session("s2")
    open_session("p1", scope="personal")  # opened last, but personal is outside the proj view
    assert session_of(add("In proj")) == "2026-09-14-2"  # the latest open one in proj+global
    assert last_event_session(str(add("Stamped"))) == "s2"
    assert session_of(kb_json("add", "Mine", "--scope", "personal")["id"]) == "2026-09-14-3"
    # global is inside every view: opened last, it becomes the current session of a proj write
    open_session("g1", scope="global")
    assert session_of(add("Global is newer")) == "2026-09-14-4"
    kb("session", "close", "--id", "g1")
    assert session_of(add("After the close")) == "2026-09-14-2"  # a closed session is never picked
    kb("session", "close", "--id", "s2")
    assert session_of(add("Down to the first")) == "2026-09-14-1"
    kb("session", "close", "--id", "s1")
    assert session_of(kb_json("add", "Mine too", "--scope", "personal")["id"]) == "2026-09-14-3"
    assert session_of(add("Nobody")) is None  # only p1 is left, and personal is out of the view
    assert last_event_session(str(add("Unstamped"))) is None
    # an open session older than 24 hours is not the current one
    open_session("old")
    kb(
        "query",
        "MATCH (s:Session {session_id: 'old'}) SET s.opened_at = s.opened_at - duration('P2D')",
    )
    assert session_of(add("Stale")) is None
    assert kb("session", "current", "--scope", "proj", code=1).output.strip() == (
        "Error: no open session"
    )
    # KB_SESSION wins over the fallback: p1 is out of the view and not the newest open one
    open_session("fresh")
    monkeypatch.setenv("KB_SESSION", "p1")
    assert session_of(add("Env wins")) == "2026-09-14-3"
    # KB_SESSION naming a session that was never opened: no edge, no error
    monkeypatch.setenv("KB_SESSION", "never-opened")
    assert session_of(add("No such session")) is None


# --- today ------------------------------------------------------------------------------


@pytest.mark.usefixtures("kb_env")
def test_today_lists_sessions_without_summary() -> None:
    open_session("a")
    kb("session", "close", "--id", "a", "--reason", "exit")
    open_session("b")
    kb("session", "close", "--id", "b", "--body", "written")
    open_session("p", scope="personal")
    kb("session", "close", "--id", "p")
    open_session("c")  # still open: not listed
    out = kb("today", "--scope", "proj").output.splitlines()
    assert out == [
        "kb today 2026-09-14 (proj)",
        "1 session without summary: 2026-09-14-1 — write them: "
        "kb session close --id a --body-file <f>",
    ]
    kb("session", "close", "--id", "c")
    assert kb("today", "--scope", "proj").output.splitlines()[-1] == (
        "2 sessions without summary: 2026-09-14-1, 2026-09-14-4 — write them: "
        "kb session close --id <session_id> --body-file <f>"
    )
    assert (
        "3 sessions without summary: 2026-09-14-1, 2026-09-14-3, 2026-09-14-4" in kb("today").output
    )
    as_json = kb_json("today", "--scope", "proj")
    assert [(s["slug"], s["session_id"]) for s in as_json["no_summary"]] == [
        ("2026-09-14-1", "a"),
        ("2026-09-14-4", "c"),
    ]
    assert "without summary" not in kb("next", "--scope", "proj").output
    # older than 7 days: no longer listed; a note written later clears the flag
    kb(
        "query",
        "MATCH (s:Session {session_id: 'a'}) SET s.closed_at = s.closed_at - duration('P8D')",
    )
    assert "2026-09-14-1" not in kb("today", "--scope", "proj").output
    kb("session", "note", "--id", "c", "did things")
    assert kb("today", "--scope", "proj").output.strip() == "kb today 2026-09-14 (proj)"
    assert kb_json("today", "--scope", "proj")["no_summary"] == []


# --- QC round: the flag, the view, archiving, the cap ------------------------------------


def test_no_summary_follows_the_body_however_it_is_written(kb_env, tmp_path) -> None:
    open_session("a")
    kb("session", "close", "--id", "a", "--reason", "exit")
    assert kb_json("show", "2026-09-14-1")["no_summary"] is True

    kb("edit", "2026-09-14-1", "--body", "written by hand")
    assert kb_json("show", "2026-09-14-1")["no_summary"] is False
    kb("edit", "2026-09-14-1", "--body", "")
    assert kb_json("show", "2026-09-14-1")["no_summary"] is True
    kb("append", "2026-09-14-1", "a paragraph")
    assert kb_json("show", "2026-09-14-1")["no_summary"] is False
    # a title-only edit leaves the flag alone
    kb("edit", "2026-09-14-1", "--body", "")
    kb("edit", "2026-09-14-1", "--title", "Renamed")
    assert kb_json("show", "2026-09-14-1")["no_summary"] is True
    # and the empty note is refused rather than dating an empty paragraph
    assert "the note is empty" in kb("session", "note", "--id", "a", "   ", code=2).output
    empty = tmp_path / "empty.md"
    empty.write_text("\n\n")
    assert (
        "the note is empty"
        in kb("session", "note", "--id", "a", "--body-file", str(empty), code=2).output
    )
    assert kb_json("show", "2026-09-14-1")["no_summary"] is True


def test_an_id_outside_the_view_is_refused(kb_env) -> None:
    open_session("mine", scope="personal")
    refused = kb("session", "close", "--id", "mine", "--scope", "proj", code=1)
    assert refused.output.strip() == (
        "Error: not visible in this scope: [265] is in scope personal; rerun with --scope personal"
    )
    assert "session 2026-09-14" not in refused.output  # no title leaks
    assert kb("session", "note", "--id", "mine", "--scope", "proj", "x", code=1).exit_code == 1
    kb("session", "close", "--id", "mine", "--scope", "personal")  # the right view works
    assert kb_json("show", "2026-09-14-1")["closed_at"] is not None


def test_archiving_a_session_hides_it_everywhere(kb_env) -> None:
    open_session("a")
    kb("session", "close", "--id", "a", "--reason", "exit")
    open_session("b")
    assert len(kb_json("session", "list", "--scope", "proj")["sessions"]) == 2
    assert kb_json("session", "current", "--scope", "proj")["session_id"] == "b"
    assert "1 session without summary" in kb("today", "--scope", "proj").output

    kb("archive", "2026-09-14-1", "--reason", "noise")
    kb("archive", "2026-09-14-2", "--reason", "noise")
    assert kb_json("session", "list", "--scope", "proj")["sessions"] == []
    assert kb("session", "current", "--scope", "proj", code=1).output.strip() == (
        "Error: no open session"
    )
    assert "without summary" not in kb("today", "--scope", "proj").output
    assert session_of(add("After the archive")) is None


def test_the_no_summary_line_names_at_most_three(kb_env) -> None:
    for name in "abcde":
        open_session(name)
        kb("session", "close", "--id", name)
    line = kb("today", "--scope", "proj").output.splitlines()[-1]
    assert line == (
        "5 sessions without summary: 2026-09-14-1, 2026-09-14-2, 2026-09-14-3 … and 2 more "
        "— write them: kb session close --id <session_id> --body-file <f>"
    )
    assert len(kb_json("today", "--scope", "proj")["no_summary"]) == 5  # json keeps them all


def test_add_link_touches_the_target(kb_env, monkeypatch) -> None:
    sess = open_session("s1")["id"]
    monkeypatch.setenv("KB_SESSION", "s1")
    target = add("A note", "--kind", "note")
    made = add("Blocked by the note", "--link", f"related:{target}")
    assert ("TOUCHED", "<-", sess) in links(str(target))
    assert ("OPENED_IN", "->", sess) in links(str(made))
    # both were created here; the link is what additionally touched the target
    assert (
        kb("session", "list", "--scope", "proj")
        .output.strip()
        .endswith("  open  · 2 created / 1 touched")
    )


def test_the_closed_event_carries_the_session_that_closed_it(kb_env, monkeypatch) -> None:
    open_session("a")
    kb("session", "close", "--id", "a", "--reason", "hook")
    assert last_event_session("2026-09-14-1") == "a"  # the hook: the session closed itself
    # the body written later, from a terminal with nothing open: no session on the event
    kb("session", "close", "--id", "a", "--body", "written later")
    assert last_event_session("2026-09-14-1") is None
    # closed from another running session, resolved through the fallback rule, not from the env
    open_session("b")
    kb("session", "close", "--id", "b")
    assert last_event_session("2026-09-14-2") == "b"
    open_session("c")
    kb("session", "close", "--id", "a", "--body", "written from c")
    assert last_event_session("2026-09-14-1") == "c"
    # a KB_SESSION naming no record still says which process wrote the event (see stamp_of)
    monkeypatch.setenv("KB_SESSION", "gone")
    kb("session", "close", "--id", "c", "--reason", "exit")
    assert last_event_session("2026-09-14-3") == "gone"


def test_reopening_updates_the_placeholder_title_only(kb_env) -> None:
    """The placeholder names the source; a title Claude has written is the session's own."""
    kb("session", "open", "--scope", "proj", "--id", "a", "--source", "startup")
    assert kb_json("show", "2026-09-14-1")["title"] == "session 2026-09-14 1 (startup)"
    kb("session", "close", "--id", "a", "--reason", "clear")
    again = open_session("a", source="compact")
    assert (again["state"], again["source"]) == ("reopened", "compact")
    assert again["title"] == "session 2026-09-14 1 (compact)"

    kb("session", "close", "--id", "a", "--title", "Ship the parser")
    kb("session", "open", "--scope", "proj", "--id", "a", "--source", "resume")
    written = kb_json("show", "2026-09-14-1")
    assert (written["title"], written["source"]) == ("Ship the parser", "resume")


def test_a_session_addresses_its_own_id_from_any_scope(kb_env, monkeypatch) -> None:
    """The cwd must not be able to strand a session that a hook opened somewhere else."""
    open_session("mine", scope="personal")
    monkeypatch.setenv("KB_SESSION", "mine")  # this process's own session
    # resumed from a project cwd: open, note and close all reach it, and current sees it
    reopened = kb("session", "open", "--scope", "proj", "--id", "mine", "--source", "resume")
    assert reopened.output.strip().endswith("] 2026-09-14-1")
    assert kb_json("session", "current", "--scope", "proj")["session_id"] == "mine"
    kb("session", "note", "--id", "mine", "--scope", "proj", "worked from the project")
    kb("session", "close", "--id", "mine", "--scope", "proj", "--reason", "exit")
    closed = kb_json("show", "2026-09-14-1")
    assert closed["closed_at"] is not None and closed["no_summary"] is False
    assert closed["scope"] == "personal"  # it never moved scope


def test_another_sessions_id_is_refused_in_a_project_cwd(kb_env, monkeypatch) -> None:
    open_session("theirs", scope="personal")
    monkeypatch.setenv("KB_SESSION", "ours")
    kb("session", "open", "--scope", "proj", "--id", "ours", "--source", "startup")
    hidden = "not visible in this scope: [265] is in scope personal; rerun with --scope personal"
    for args in (
        ("session", "open", "--scope", "proj", "--id", "theirs", "--source", "resume"),
        ("session", "close", "--scope", "proj", "--id", "theirs"),
        ("session", "note", "--scope", "proj", "--id", "theirs", "x"),
    ):
        result = kb(*args, code=1)
        assert result.output.strip() == f"Error: {hidden}", args
    # current never reaches it either: it is neither ours nor in the view
    assert kb_json("session", "current", "--scope", "proj")["session_id"] == "ours"
    monkeypatch.delenv("KB_SESSION")
    kb("session", "close", "--scope", "proj", "--id", "ours")
    assert kb("session", "current", "--scope", "proj", code=1).output.strip() == (
        "Error: no open session"
    )
    # and the refused session is still openable and closable from its own scope
    kb("session", "close", "--scope", "personal", "--id", "theirs", "--reason", "exit")
    assert kb_json("show", "2026-09-14-1")["closed_at"] is not None
