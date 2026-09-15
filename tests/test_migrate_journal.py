"""kb migrate journal against tests/fixtures/journal: three real day files (2026-04-17 with a
standup and an untimed, intent-less block; 2026-06-09 with a ``# date`` header and ``**Nodes
created:**``; 2026-06-15 with a standup) and one synthetic file of edge cases (2026-05-01)."""

import datetime as dt
import json
from pathlib import Path

import pytest
from click.testing import CliRunner, Result

from kb import migrate_journal
from kb.cli import main
from kb.config import load

JOURNAL = Path(__file__).parent / "fixtures" / "journal"
UTC = dt.UTC
STATE = """## WIP
- [6] 2026-04-16: KIO-1001 review
- [7] 2026-04-16: KIO-1001 email notification
- [18] 2026-06-09: PR 766
- [19] 2026-06-09: notify a colleague
- [20] 2026-06-09: uncommitted infra
- [21] 2026-06-09: deploy skill untested
- [30] 2026-06-15: PR 1 my-status
- [40] 2026-05-01: forty
- [41] 2026-05-01: forty-one
- [42] 2026-05-01: forty-two
"""
# touched: 04-17 2+1, 05-01 5+0+1+0, 06-09 3+2+2, 06-15 2+2; opened_in: 40 41 42, 18, 20 21, 30;
# unresolved: ids 17 24 43 44 6706 and 14 node slugs nobody migrated here
SUMMARY = (
    "created 11 sessions across 4 days, linked 20 touched / 7 opened_in edges, "
    "19 unresolved targets, skipped 0 existing"
)


def kb(*args: str, code: int = 0) -> Result:
    result = CliRunner().invoke(main, list(args))
    assert result.exit_code == code, result.output
    return result


def kb_json(*args: str):
    return json.loads(kb("--json", *args).stdout)


def record_count(kb_env, kind: str = "session") -> int:
    rows, _, _ = kb_env.driver.execute_query(
        "MATCH (r:Record {kind: $kind}) RETURN count(r) AS n", kind=kind, database_="neo4j"
    )
    return rows[0]["n"]


@pytest.fixture
def cfg(tmp_path: Path):
    return load(path=tmp_path / "none.toml", home=tmp_path)  # defaults: Asia/Manila


@pytest.fixture
def seeded(kb_env, tmp_path: Path):
    """Ten tasks with the ids the fixtures mention, two of the nodes they link."""
    state = tmp_path / "state.md"
    state.write_text(STATE)
    kb("migrate", "state", str(state), "--scope", "proj")
    kb(
        "add",
        "Journal format",
        "--kind",
        "howto",
        "--slug",
        "conventions_journal_format",
        "--scope",
        "proj",
    )
    kb(
        "add",
        "Session close",
        "--kind",
        "feedback",
        "--slug",
        "feedback_session_close",
        "--scope",
        "proj",
    )
    return kb_env


# --- parsing ----------------------------------------------------------------------------


def test_fixtures_parse_to_the_expected_blocks(cfg) -> None:
    blocks, skipped = migrate_journal.load_dir(JOURNAL, cfg)
    assert skipped == []
    by = {b.slug: b for b in blocks}
    assert [b.slug for b in blocks] == [
        "2026-04-17-1",
        "2026-04-17-2",
        "2026-05-01-1",
        "2026-05-01-4",
        "2026-05-01-5",
        "2026-05-01-3",
        "2026-06-09-1",
        "2026-06-09-2",
        "2026-06-09-3",
        "2026-06-15-1",
        "2026-06-15-2",
    ]

    untimed = by["2026-04-17-1"]  # "## Session 1", no time, no Intent, no Tags
    assert (untimed.time, untimed.intent, untimed.tags) == (dt.time(), None, ())
    assert untimed.title == "session 2026-04-17 1"
    assert untimed.at == dt.datetime(2026, 4, 16, 16, 0, tzinfo=UTC)  # midnight Manila
    assert untimed.body.startswith("### Done\n- **[7] CLOSED** KIO-1001")
    assert untimed.body.endswith("none merged yet.")  # the closing --- is not part of it
    assert (untimed.ids, untimed.opened_ids) == ((7, 6), ())
    assert untimed.session_id == "journal:2026-04-17-1"
    closing = by["2026-04-17-2"]
    assert closing.time == dt.time(14, 6)
    assert closing.title == "Close WIP [6] per user confirmation"
    assert closing.tags == ("custom_recruitment", "admin")
    assert closing.ids == (6,)

    first = by["2026-06-09-1"]
    assert first.at == dt.datetime(2026, 6, 9, 6, 8, tzinfo=UTC)  # 14:08 Manila
    assert first.title == (
        'Get `demo_change_tracker` installing + green on Odoo 16, add a "changed by" field, '
        "ship a PR"
    )
    assert first.tags == (
        "demo_change_tracker",
        "bug",
        "feature",
        "testing",
        "ready_for_review",
    )
    assert (first.ids, first.opened_ids) == ((18,), (18,))
    assert first.targets == (
        "feedback_session_close",
        "feedback_scope_discipline",
        "conventions_journal_format",
        "reference_odoo16_no_expression_attributes",
        "reference_odoo17_to_16_api_porting",
        "reference_odoo_menu_custom_group_no_one",
        "project_addons_git_repo",
    )
    assert first.body.startswith("**Intent:** Get `demo_change_tracker`")
    third = by["2026-06-09-3"]
    assert (third.ids, third.opened_ids) == (
        (20, 21, 17),
        (20, 21),
    )  # "added [20] (...), [21] (...)"
    assert by["2026-06-09-2"].opened_ids == ()  # "closed [19]" opens nothing

    standup_day = by["2026-06-15-2"]
    assert standup_day.time == dt.time(10, 36)
    assert (standup_day.ids, standup_day.opened_ids) == ((24, 30), ())
    assert by["2026-06-15-1"].opened_ids == (30,)
    assert len(standup_day.targets) == 8
    assert all(not b.by_position for b in blocks if b.file != "2026-05-01.md")


def test_synthetic_edge_cases(cfg) -> None:
    blocks = migrate_journal.parse_day(JOURNAL / "2026-05-01.md", dt.date(2026, 5, 1), cfg)
    assert [(b.n, b.by_position, b.time) for b in blocks] == [
        (1, False, dt.time(9, 15)),
        (4, True, dt.time(10, 30)),  # a repeated "Session 1"
        (5, True, dt.time(11, 45)),  # "Session 11:45 — ...": a time, not a number
        (3, False, dt.time()),
    ]
    fenced = blocks[0]
    assert "## Session 9 — 23:59" in fenced.body  # the heading inside the fence did not split
    assert fenced.body.endswith("not blank after the rule, so the block continues: [44]")
    assert fenced.ids == (40, 41, 42, 43, 6, 6706, 44)  # [7] and [99] sit in code, [41] once
    assert fenced.opened_ids == (40, 41, 42, 43)
    assert fenced.targets == ("conventions_journal_format",)
    assert fenced.tags == ("tooling", "kb")
    cut = blocks[1]
    assert 100 < len(cut.title) <= 120 and cut.title.endswith("…")
    assert cut.title.startswith("A repeated number gets the next free one; this intent is")
    assert cut.ids == ()
    timed = blocks[2]
    assert (timed.title, timed.ids, timed.tags) == ("session 2026-05-01 5", (40,), ())
    wrapped = blocks[3]
    assert wrapped.title == "Wrapped intent line one continues on the second line"
    assert wrapped.tags == ("kb", "wrapped")
    # nothing outside a session block counts: the standup's [7], the stray heading's [43]
    assert 7 not in {i for b in blocks for i in b.ids}


def test_heading_shapes() -> None:
    parse = migrate_journal.parse_heading
    assert parse("## Session 1 — 15:45") == (1, dt.time(15, 45))
    assert parse("## Session 2 - 9:05") == (2, dt.time(9, 5))
    assert parse("## Session 3") == (3, dt.time())
    assert parse("## Session 1 — 2026-04-10") == (1, dt.time())
    assert parse("## Session 10:30 — OFFERS-PAGE pipeline") == (None, dt.time(10, 30))
    assert parse("## Session — quotation PDF polish") == (None, dt.time())
    assert parse("## Session close — 2026-08-10") == (None, dt.time())
    assert parse("## Session 2 (continued from 3) — 14:05") == (2, dt.time(14, 5))
    # two loose times: the last one, so the block sorts after the one before it in the day
    assert parse("## Session 5 — started 2026-08-19 10:20, closed 2026-08-19 17:55") == (
        5,
        dt.time(17, 55),
    )
    assert parse("## Session 6 — 02:00-08:00") == (6, dt.time(2, 0))  # a range: its first time
    # an en dash, spaced: the character is the input, so it stays literal here
    assert parse("## Session 7 — 02:00 – 08:00") == (7, dt.time(2, 0))  # noqa: RUF001
    # the parenthesised start belongs to another day: the time on the line itself wins
    assert parse("## Session 8 (started 2026-07-16 ~23:50, spans midnight) — 00:30") == (
        8,
        dt.time(0, 30),
    )
    assert parse("## Session 9 (10:00-11:00 elsewhere)") == (9, dt.time())
    assert parse("## Session 4 — 25:99 nonsense") == (4, dt.time())
    assert parse("## Session 4 — 25:99 then 08:15") == (4, dt.time(8, 15))
    assert parse("## Session 4 — 25:99-08:15") == (4, dt.time(8, 15))  # a bad range start
    assert migrate_journal.number_blocks([1, 1, None, 3, 2, None]) == [
        (1, False),
        (4, True),
        (5, True),
        (3, False),
        (2, False),
        (6, True),
    ]
    assert migrate_journal.number_blocks([None, None]) == [(1, True), (2, True)]


def test_split_blocks_rules() -> None:
    text = (
        "# 2026-01-01\n\nintro text is dropped\n\n## Session 1 — 10:00\n\nbody one\n---\n"
        "still body: a rule followed by text\n\n---\n\ndropped after the rule\n"
        "## Session 2 — 11:00\nbody two\n```\n## Session 3\n```\n## Other\nnot a session\n"
        "## Session 4\nlast\n---"
    )
    raws = migrate_journal.split_blocks(text)
    assert [r.heading for r in raws] == [
        "## Session 1 — 10:00",
        "## Session 2 — 11:00",
        "## Session 4",
    ]
    assert raws[0].lines == ["", "body one", "---", "still body: a rule followed by text", ""]
    assert raws[1].lines == ["body two", "```", "## Session 3", "```"]
    assert raws[2].lines == ["last"]  # a trailing --- at EOF closes the block


def test_load_dir_skips_other_files_and_refuses_bad_dates(cfg, tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("## Session 1\nnot a day file\n")
    (tmp_path / "notes.md").write_text("## Session 1\n")
    (tmp_path / "2026-02-03.md").write_text("## Session 1 — 08:00\n\nbody\n")
    blocks, skipped = migrate_journal.load_dir(tmp_path, cfg)
    assert skipped == ["README.md", "notes.md"]
    assert [b.slug for b in blocks] == ["2026-02-03-1"]
    (tmp_path / "2026-13-01.md").write_text("## Session 1\n")
    with pytest.raises(migrate_journal.records.RecordError, match=r"2026-13-01\.md: not a date"):
        migrate_journal.load_dir(tmp_path, cfg)


# --- the command ------------------------------------------------------------------------


def test_dry_run_prints_the_plan_and_writes_nothing(seeded) -> None:
    result = kb("migrate", "journal", str(JOURNAL), "--scope", "proj", "--dry-run")
    lines = result.output.splitlines()
    plan = {line.split()[0]: line for line in lines[:11]}
    assert plan["2026-04-17-1"].split()[1:6] == ["00:00", "2", "edges", "0", "unresolved"]
    assert plan["2026-04-17-1"].endswith("session 2026-04-17 1")
    assert plan["2026-05-01-1"].split()[1:6] == ["09:15", "8", "edges", "3", "unresolved"]
    assert plan["2026-05-01-4"].endswith("(numbered by position)")
    assert plan["2026-06-09-3"].split()[1:6] == ["17:43", "4", "edges", "4", "unresolved"]
    assert "## Numbered by position (2)" in result.output
    assert (
        "- 2026-05-01-5 ← '## Session 11:45 — a heading with a time but no number'" in result.output
    )
    assert "## Unresolved targets (19)" in result.output
    assert "- [17] no record ← 1 session: 2026-06-09-3" in result.output
    assert "- [6706] no record ← 1 session: 2026-05-01-1" in result.output
    assert (
        "- [[feedback_scope_discipline]] ← 2 sessions: 2026-06-09-1, 2026-06-15-2" in result.output
    )
    assert lines[-1] == (
        "would create 11 sessions across 4 days, link 20 touched / 7 opened_in edges, "
        "19 unresolved targets, skip 0 existing"
    )
    assert record_count(seeded) == 0
    as_json = kb_json("migrate", "journal", str(JOURNAL), "--scope", "proj", "--dry-run")
    assert len(as_json["plan"]) == 11 and as_json["days"] == 4
    assert as_json["unresolved_ids"]["43"] == ["2026-05-01-1"]
    assert [b["slug"] for b in as_json["by_position"]] == ["2026-05-01-4", "2026-05-01-5"]


def test_migrate_creates_sessions_events_and_edges(seeded, tmp_path: Path) -> None:
    report = tmp_path / "report.md"
    result = kb("migrate", "journal", str(JOURNAL), "--scope", "proj", "--report", str(report))
    assert result.stdout.strip() == SUMMARY
    text = report.read_text()
    assert text.startswith("# kb migrate journal\n\n## Created (11)\n")
    assert "- 2026-06-09-1  14:08  Get `demo_change_tracker` installing" in text
    assert record_count(seeded) == 11

    first = kb_json("show", "2026-06-09-1")
    assert (first["kind"], first["scope"], first["source"]) == ("session", "proj", "migrated")
    assert first["session_id"] == "journal:2026-06-09-1"
    assert first["tags"] == [
        "demo_change_tracker",
        "bug",
        "feature",
        "testing",
        "ready_for_review",
        "migrated",
    ]
    assert first["opened_at"] == first["closed_at"] == "2026-06-09T06:08:00+00:00"
    assert first["created_at"] == first["updated_at"] == "2026-06-09T06:08:00+00:00"
    assert first["no_summary"] is False and first["reason"] is None
    assert first["body"].startswith("**Intent:** Get `demo_change_tracker`")
    assert first["body"].endswith("### Open threads\nnone")
    assert [(e["kind"], e["note"]) for e in first["events"]] == [
        ("closed", None),
        ("opened", None),
        ("created", "migrated from journal/2026-06-09.md"),
    ]
    assert all(e["at"] == "2026-06-09T06:08:00+00:00" for e in first["events"])
    assert sorted((lk["type"], lk["direction"], lk["id"]) for lk in first["links"]) == [
        ("OPENED_IN", "<-", 18),
        ("TOUCHED", "->", 18),
        ("TOUCHED", "->", kb_json("show", "conventions_journal_format")["id"]),
        ("TOUCHED", "->", kb_json("show", "feedback_session_close")["id"]),
    ]
    shown = kb("show", "2026-06-09-1").output
    assert "\ncreated here:\n  [18] PR 766\n" in shown
    assert "\ntouched here:\n  [18] PR 766\n" in shown
    assert shown.splitlines()[2] == (
        "session: journal:2026-06-09-1  opened: 2026-06-09 06:08Z  closed: 2026-06-09 06:08Z"
    )

    task = kb_json("show", "6")
    assert sorted(lk["title"] for lk in task["links"] if lk["type"] == "TOUCHED") == [
        "Close WIP [6] per user confirmation",
        "First block with a fence that hides a heading and a rule",
        "session 2026-04-17 1",
    ]  # 2026-04-17-2, 2026-05-01-1, 2026-04-17-1; the standup's [6] did not count
    forty = kb_json("show", "40")
    assert sorted(lk["type"] for lk in forty["links"]) == ["OPENED_IN", "TOUCHED", "TOUCHED"]

    listing = kb("session", "list", "--scope", "proj", "--limit", "3").output.splitlines()
    assert (
        listing[0].startswith("[") and "] 2026-06-15-1  Rework the `/my-status` skill" in listing[0]
    )
    assert listing[0].endswith("closed 2026-06-15 04:19Z  · 1 created / 2 touched")
    assert [line.split()[1] for line in listing] == ["2026-06-15-1", "2026-06-15-2", "2026-06-09-3"]
    assert "without summary" not in kb("today", "--scope", "proj").output


def test_second_run_skips_everything_and_a_new_target_gets_linked(seeded) -> None:
    kb("migrate", "journal", str(JOURNAL), "--scope", "proj")
    again = kb("migrate", "journal", str(JOURNAL), "--scope", "proj")
    assert again.stdout.splitlines()[-1] == (
        "created 0 sessions across 0 days, linked 0 touched / 0 opened_in edges, "
        "19 unresolved targets, skipped 11 existing"
    )
    assert "## Skipped, already in the store (11)" in again.stdout
    assert record_count(seeded) == 11
    assert [e["kind"] for e in kb_json("show", "2026-06-09-2")["events"]] == [
        "closed",
        "opened",
        "created",
    ]
    kb(
        "add",
        "Scope discipline",
        "--kind",
        "feedback",
        "--slug",
        "feedback_scope_discipline",
        "--scope",
        "proj",
    )
    third = kb("migrate", "journal", str(JOURNAL), "--scope", "proj", "--dry-run")
    assert third.stdout.splitlines()[-1] == (
        "would create 0 sessions across 0 days, link 2 touched / 0 opened_in edges, "
        "18 unresolved targets, skip 11 existing"
    )
    real = kb("migrate", "journal", str(JOURNAL), "--scope", "proj")
    assert "linked 2 touched / 0 opened_in edges, 18 unresolved" in real.stdout.splitlines()[-1]
    links = kb_json("links", "feedback_scope_discipline")["links"]
    assert sorted(lk["slug"] for lk in links) == ["2026-06-09-1", "2026-06-15-2"]


@pytest.mark.usefixtures("kb_env")
def test_report_into_a_missing_directory_and_empty_dir(kb_env, tmp_path: Path) -> None:
    missing = tmp_path / "nope" / "report.md"
    result = kb(
        "migrate", "journal", str(JOURNAL), "--scope", "proj", "--report", str(missing), code=1
    )
    assert result.output.strip() == f"Error: not found: {tmp_path / 'nope'}"
    assert record_count(kb_env) == 0
    (tmp_path / "empty").mkdir()
    (tmp_path / "empty" / "README.md").write_text("# Journal\n")
    assert kb("migrate", "journal", str(tmp_path / "empty"), "--scope", "proj").output.strip() == (
        f"{tmp_path / 'empty'}: no session blocks found"
    )
    assert (
        "unknown scope 'nope'"
        in kb("migrate", "journal", str(JOURNAL), "--scope", "nope", code=2).output
    )


# --- what an id is allowed to bind to ----------------------------------------------------


def _day(directory: Path, name: str, text: str) -> Path:
    directory.mkdir(exist_ok=True)
    (directory / name).write_text(text)
    return directory


def test_an_id_binds_only_to_a_task_that_already_existed(kb_env, tmp_path: Path) -> None:
    """[NNN] was a state-item number: a howto or a later task wearing that id is not it."""
    state = tmp_path / "state.md"
    state.write_text("## WIP\n- [1] 2026-06-01: early task\n- [3] 2026-07-01: late task\n")
    kb("migrate", "state", str(state), "--scope", "proj")
    kb("add", "A howto", "--kind", "howto", "--slug", "a_howto", "--scope", "proj")
    kb_env.driver.execute_query(
        "MATCH (r:Record {slug: 'a_howto'}) SET r.id = 2", database_="neo4j"
    )
    journal = _day(
        tmp_path / "journal",
        "2026-06-10.md",
        "## Session 1 — 09:00\n\n**Intent:** three mentions\n\n### Done\n"
        "- touched [1], [2] and [3]\n",
    )

    report = kb_json("migrate", "journal", str(journal), "--scope", "proj", "--dry-run")
    assert report["unresolved_ids"] == {"2": ["2026-06-10-1"], "3": ["2026-06-10-1"]}
    assert report["id_reasons"] == {"2": "not a task (howto)", "3": "created after the block"}
    assert (report["touched"], report["opened_in"]) == (1, 0)

    kb("migrate", "journal", str(journal), "--scope", "proj")
    assert [lk["id"] for lk in kb_json("show", "2026-06-10-1")["links"]] == [1]


def test_a_task_created_the_night_of_the_block_still_binds(kb_env, tmp_path: Path) -> None:
    """One day of slack: an entry written after midnight names a task created "today"."""
    state = tmp_path / "state.md"
    state.write_text("## WIP\n- [5] 2026-06-11: same night\n- [6] 2026-06-12: a day too late\n")
    kb("migrate", "state", str(state), "--scope", "proj")
    journal = _day(
        tmp_path / "journal", "2026-06-10.md", "## Session 1 — 23:40\n\n- filed [5], [6]\n"
    )
    report = kb_json("migrate", "journal", str(journal), "--scope", "proj", "--dry-run")
    assert report["unresolved_ids"] == {"6": ["2026-06-10-1"]}
    assert report["id_reasons"]["6"] == "created after the block"
    assert (report["touched"], report["opened_in"]) == (1, 1)  # [5] both ways, [6] neither


def test_a_live_session_holding_the_slug_renumbers_the_block(kb_env, tmp_path: Path) -> None:
    """A slug is a date, and a live session of that date already holds one."""
    kb("session", "open", "--scope", "proj", "--id", "live", "--source", "startup")
    journal = _day(
        tmp_path / "journal",
        "2026-09-14.md",
        "## Session 1 — 08:00\n\n**Intent:** the imported one\n\n### Done\n- work\n",
    )
    out = kb("migrate", "journal", str(journal), "--scope", "proj").stdout
    assert out.splitlines()[-1].startswith("created 1 sessions across 1 days")

    imported = kb_json("show", "2026-09-14-2")
    assert imported["session_id"] == "journal:2026-09-14-1"  # keyed on its own numbering
    assert imported["title"] == "the imported one" and imported["source"] == "migrated"
    live = kb_json("show", "2026-09-14-1")
    assert live["session_id"] == "live" and live["body"] == "" and live["closed_at"] is None

    report = kb_json("migrate", "journal", str(journal), "--scope", "proj", "--dry-run")
    assert report["renumbered"] == []  # it was renumbered on the first run, now it just exists
    assert report["skipped"] == ["2026-09-14-2"] and report["created"] == []
    again = kb("migrate", "journal", str(journal), "--scope", "proj").stdout
    assert again.splitlines()[-1].startswith("created 0 sessions across 0 days")
    assert again.splitlines()[-1].endswith("skipped 1 existing")


def test_nodes_created_line_opens_the_records_it_names(kb_env) -> None:
    """``**Nodes created:** [[a]]; updated [[b]]``: only a was opened in that session."""
    for slug in (
        "feedback_fork_third_party_tools",
        "project_prod_deploy",
        "reference_teams_mcp_auth_setup",
    ):
        kb("add", slug, "--kind", "reference", "--slug", slug, "--scope", "proj")
    kb("migrate", "journal", str(JOURNAL), "--scope", "proj")
    links = kb_json("show", "2026-06-09-3")["links"]
    by_slug: dict[str, set[str]] = {}
    for lk in links:
        by_slug.setdefault(lk["title"], set()).add(lk["type"])
    assert by_slug["feedback_fork_third_party_tools"] == {"OPENED_IN", "TOUCHED"}
    assert by_slug["project_prod_deploy"] == {"OPENED_IN", "TOUCHED"}
    assert by_slug["reference_teams_mcp_auth_setup"] == {"TOUCHED"}  # after the ";", updated
