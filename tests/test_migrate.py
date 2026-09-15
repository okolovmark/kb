import datetime as dt
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from kb import migrate
from kb.cli import main
from kb.db import counter_value

FIXTURE = Path(__file__).parent / "fixtures" / "state_sample.md"


def test_parse_the_sample() -> None:
    items = {i.id: i for i in migrate.parse(FIXTURE.read_text())}
    assert sorted(items) == [144, 202, 208, 210, 251, 256, 264, 265]
    open_items = [i for i in items.values() if i.closed_on is None]
    assert len(open_items) == 6
    assert {i.tag for i in open_items} == {"blocked", "ready_for_review", "wip", "open_thread"}

    kb = items[264]
    assert kb.date == dt.date(2026, 9, 11)
    assert kb.title == "kb"
    assert kb.journal_ref == "journal/2026-09-13"
    assert kb.external_id is None
    assert kb.text.startswith("**kb** — phase 0 MERGED")

    odoo = items[256]
    assert odoo.external_id == "33172"
    assert odoo.title.startswith("KIO-1001 the 2,906 parts")
    assert "[odoo:33172]" not in odoo.title
    assert odoo.text.endswith("[odoo:33172]")
    assert odoo.journal_ref == "journal/2026-09-09"

    bold = items[208]
    assert bold.title == "KIO-1002 description template mechanism"
    assert bold.tag == "wip"

    closed = items[202]
    assert closed.closed_on == dt.date(2026, 8, 26)
    assert closed.date == dt.date(2026, 8, 26)  # no inner date: the CLOSED date is all we have
    assert closed.journal_ref == "journal/2026-08-26"
    assert closed.title.startswith("PR 104 merged and deployed to prod;")

    multi = items[210]
    assert multi.closed_on == dt.date(2026, 9, 1)
    assert multi.date == dt.date(2026, 8, 26)  # the inner `[210] 2026-08-26:` is the item date
    assert multi.title.startswith("the 202 products whose Type")
    assert "and let the template fill Type" in multi.text  # the comment spanned two lines
    assert len(items[251].title) <= 100


def test_title_is_capped_at_100_chars() -> None:
    item = migrate.Item(1, dt.date(2026, 1, 1), "x" * 300, "wip")
    assert len(item.title) == 100
    assert migrate.Item(2, dt.date(2026, 1, 1), "**bold** rest — tail", "wip").title == "bold rest"


@pytest.mark.usefixtures("kb_env")
def test_dry_run_writes_nothing(kb_env) -> None:
    result = CliRunner().invoke(
        main, ["migrate", "state", str(FIXTURE), "--scope", "proj", "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    assert "would create 8 tasks (2 closed), skip 0" in result.output
    assert "[256] 2026-09-09 open_thread      open              KIO-1001" in result.output
    assert "[odoo:33172]" in result.output
    assert "[210] 2026-08-26 open_thread      closed 2026-09-01" in result.output
    records, _, _ = kb_env.driver.execute_query(
        "MATCH (r:Record) RETURN count(r) AS n", database_="neo4j"
    )
    assert records[0]["n"] == 0


def test_migrate_creates_tasks_with_their_ids(kb_env) -> None:
    runner = CliRunner()
    assert counter_value(kb_env.driver) == 264
    result = runner.invoke(main, ["migrate", "state", str(FIXTURE), "--scope", "proj"])
    assert result.exit_code == 0, result.output
    # the sample carries [265] while the fresh Counter sits at 264: the Counter moves up
    assert result.output.strip() == (
        "created 8 tasks (2 closed), skipped 0 existing; counter set to 265"
    )
    assert counter_value(kb_env.driver) == 265

    shown = runner.invoke(main, ["--json", "show", "256"])
    rec = json.loads(shown.stdout)
    assert rec["kind"] == "task"
    assert rec["scope"] == "proj"
    assert rec["sched"] == "age"
    assert rec["anchor"] == "2026-09-09"
    assert rec["days"] == "work"
    assert rec["tags"] == ["open_thread"]
    assert rec["source"] == "odoo"
    assert rec["external_id"] == "33172"
    assert rec["journal_ref"] == "journal/2026-09-09"
    assert rec["created_at"].startswith("2026-09-08T16:00:00")  # midnight Manila, in UTC
    assert rec["done_at"] is None
    assert [e["kind"] for e in rec["events"]] == ["created"]
    assert rec["events"][0]["at"].startswith("2026-09-08T16:00:00")

    closed = json.loads(runner.invoke(main, ["--json", "show", "210"]).stdout)
    assert closed["done_at"].startswith("2026-08-31T16:00:00")
    assert closed["created_at"].startswith("2026-08-25T16:00:00")
    assert [e["kind"] for e in closed["events"]] == ["done", "created"]
    assert closed["source"] == "manual"

    plain = json.loads(runner.invoke(main, ["--json", "show", "264"]).stdout)
    assert plain["title"] == "kb"
    assert plain["source"] == "manual"
    assert plain["body"].startswith("**kb** — phase 0 MERGED")

    # a second run skips every id and reports them
    again = runner.invoke(main, ["migrate", "state", str(FIXTURE), "--scope", "proj"])
    assert again.exit_code == 0, again.output
    assert again.output.strip() == (
        "created 0 tasks (0 closed), skipped 8 existing: 144, 264, 202, 208, 265, 256, 210, 251"
    )
    # open migrated items are ordinary age tasks: today (2026-09-14) they are old enough to show
    today = runner.invoke(main, ["today", "--scope", "proj"])
    assert today.exit_code == 0, today.output
    assert "[144] L3 loud  vendor payment term defaults" in today.output
    assert "[210]" not in today.output


def test_unknown_scope_and_empty_file(kb_env, tmp_path: Path) -> None:
    runner = CliRunner()
    bad = runner.invoke(main, ["migrate", "state", str(FIXTURE), "--scope", "nope"])
    assert bad.exit_code == 2
    assert "unknown scope 'nope'" in bad.output
    empty = tmp_path / "empty.md"
    empty.write_text("# nothing\n")
    result = runner.invoke(main, ["migrate", "state", str(empty), "--scope", "proj"])
    assert result.exit_code == 0
    assert "no state items found" in result.output


def test_counter_option_sets_the_counter_or_refuses(kb_env) -> None:
    runner = CliRunner()
    low = runner.invoke(
        main, ["migrate", "state", str(FIXTURE), "--scope", "proj", "--counter", "100"]
    )
    assert low.exit_code == 1
    assert low.output.strip() == (
        "Error: --counter 100 is below the floor: highest item id 265, Counter 264, "
        "highest record id 0"
    )
    assert counter_value(kb_env.driver) == 264

    dry = runner.invoke(
        main, ["migrate", "state", str(FIXTURE), "--scope", "proj", "--counter", "267", "--dry-run"]
    )
    assert dry.exit_code == 0, dry.output
    assert (
        dry.output.splitlines()[-1] == "would create 8 tasks (2 closed), skip 0; counter set to 267"
    )
    assert counter_value(kb_env.driver) == 264

    real = runner.invoke(
        main, ["migrate", "state", str(FIXTURE), "--scope", "proj", "--counter", "267"]
    )
    assert real.exit_code == 0, real.output
    assert (
        real.output.strip() == "created 8 tasks (2 closed), skipped 0 existing; counter set to 267"
    )
    assert counter_value(kb_env.driver) == 267
    fresh = runner.invoke(main, ["--json", "add", "after the migration", "--scope", "proj"])
    assert json.loads(fresh.stdout)["id"] == 268


def test_counter_option_never_drops_below_existing_records(kb_env) -> None:
    runner = CliRunner()
    for n in range(6):  # ids 265..270
        assert runner.invoke(main, ["add", f"task {n}", "--scope", "proj"]).exit_code == 0
    assert counter_value(kb_env.driver) == 270
    for args in (["--counter", "267"], ["--counter", "267", "--dry-run"]):
        low = runner.invoke(main, ["migrate", "state", str(FIXTURE), "--scope", "proj", *args])
        assert low.exit_code == 1, low.output
        assert low.output.strip() == (
            "Error: --counter 267 is below the floor: highest item id 265, Counter 270, "
            "highest record id 270"
        )
    assert counter_value(kb_env.driver) == 270
    ok = runner.invoke(
        main, ["migrate", "state", str(FIXTURE), "--scope", "proj", "--counter", "300"]
    )
    assert ok.exit_code == 0, ok.output
    assert counter_value(kb_env.driver) == 300
    fresh = runner.invoke(main, ["--json", "add", "after", "--scope", "proj"])
    assert fresh.exit_code == 0 and json.loads(fresh.stdout)["id"] == 301
