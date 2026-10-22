"""kb consolidate: merge, condense and archive proposals over knowledge records; no writes."""

import json
from typing import Any

import pytest
from click.testing import CliRunner, Result

from kb.cli import main
from kb.consolidate import CONDENSE_BYTES

BIG = "\n\n".join(f"**2026-09-{d:02d}:** " + "a finding worth keeping " * 30 for d in range(1, 20))


def kb(*args: str, code: int = 0) -> Result:
    result = CliRunner().invoke(main, list(args))
    assert result.exit_code == code, result.output
    return result


def kb_json(*args: str) -> Any:
    return json.loads(kb("--json", *args).stdout)


def add(title: str, *args: str, scope: str = "proj") -> int:
    return kb_json("add", title, "--scope", scope, "--distinct", *args)["id"]


def proposals(action: str | None = None) -> list[dict[str, Any]]:
    found = kb_json("consolidate", "--scope", "proj")["proposals"]
    return [p for p in found if action is None or p["action"] == action]


def ids(proposal: dict[str, Any]) -> list[int]:
    return [r["id"] for r in proposal["records"]]


def events_count(kb_env) -> int:
    rows, _, _ = kb_env.driver.execute_query(
        "MATCH (e:Event) RETURN count(e) AS n", database_="neo4j"
    )
    return rows[0]["n"]


def test_overlapping_records_with_the_same_areas_are_proposed_for_a_merge(kb_env) -> None:
    (kb_env.proj / ".kb" / "tags.toml").write_text('[areas]\ndemo = "x"\nbilling = "y"\n')
    short = add("Payment terms are shared across companies", "--kind", "note", "--tag", "demo")
    long_ = add(
        "Payment terms shared across companies",
        "--kind",
        "reference",
        "--tag",
        "demo",
        "--tag",
        "bug",
        "--body",
        "the longer one",
    )
    add("Payment terms are shared across companies", "--kind", "note", "--tag", "billing")
    add("Something else entirely", "--kind", "note", "--tag", "demo")
    # the third one says the same about another area: not the same record
    (merge,) = proposals("merge")
    assert sorted(ids(merge)) == sorted([short, long_])
    assert merge["keep"] == long_  # the longer body
    assert merge["reason"].startswith("same areas: demo; title and summary similarity")
    assert merge["commands"] == [
        f"kb edit {long_} --body-file <merged.md>",
        f"kb merge {short} {long_}",
    ]


def test_untagged_overlaps_are_proposed_too(kb_env) -> None:
    a = add("Mouser rate limits per minute", "--kind", "note", "--tag", "demo")
    b = add("Mouser rate limits per minute and per day", "--kind", "note", "--tag", "demo")
    for other in ("Deploy checklist", "Backup schedule", "Release notes"):  # words get their weight
        add(other, "--kind", "note", "--tag", "demo")
    kb_env.driver.execute_query("MATCH (r:Record) SET r.tags = []", database_="neo4j")
    (merge,) = proposals("merge")
    assert sorted(ids(merge)) == [a, b]
    assert merge["reason"].startswith("no area tags on any of them;")


@pytest.mark.usefixtures("kb_env")
def test_condense_covers_long_knowledge_but_never_decisions_tasks_or_sessions() -> None:
    assert len(BIG.encode()) >= CONDENSE_BYTES
    note = add("A long note", "--kind", "note", "--tag", "demo", "--body", BIG)
    add("A short note", "--kind", "note", "--tag", "demo", "--body", "brief")
    add("A long decision", "--kind", "decision", "--tag", "demo", "--body", BIG)
    add("A long task", "--tag", "demo", "--body", BIG)
    (condense,) = proposals("condense")
    assert ids(condense) == [note]
    assert condense["reason"] == (
        f"body {len(BIG.encode()) / 1000:.1f} KB, 19 dated paragraphs (2026-09-01 … 2026-09-19); "
        "the edited event keeps the full text"
    )
    assert condense["commands"] == [f"kb edit {note} --body-file <condensed.md>"]


@pytest.mark.usefixtures("kb_env")
def test_superseded_records_and_decisions_are_proposed_for_archive() -> None:
    old = add("Use the old exporter", "--kind", "decision", "--tag", "demo")
    new = add("Use the new exporter", "--kind", "decision", "--tag", "demo")
    kb("link", str(new), "SUPERSEDES", str(old))
    stale = add("Old howto", "--kind", "howto", "--tag", "demo")
    gone = add("Its replacement, archived too", "--kind", "howto", "--tag", "demo")
    kb("link", str(gone), "SUPERSEDES", str(stale))
    kb("archive", str(gone), "--reason", "test")
    (archive,) = proposals("archive")
    assert ids(archive) == [old]
    assert archive["reason"] == f"superseded by [{new}] Use the new exporter"
    assert archive["commands"] == [f'kb archive {old} --reason "superseded by [{new}]"']


def test_notes_of_finished_work_are_proposed_for_archive(kb_env, monkeypatch) -> None:
    monkeypatch.setenv("KB_TODAY", "2026-09-01")
    done_with = add("Notes on the export bug", "--kind", "note", "--tag", "demo")
    cited = add("Notes another record cites", "--kind", "note", "--tag", "demo")
    kb(
        "link",
        str(add("A reference", "--kind", "reference", "--tag", "demo")),
        "RELATED",
        str(cited),
    )
    touched = add("Notes appended to later", "--kind", "note", "--tag", "demo")
    recent = add("Notes on a task closed last week", "--kind", "note", "--tag", "demo")
    pinned = add("A pinned note", "--kind", "note", "--tag", "demo", "--pin")
    task = add("Fix the export bug", "--tag", "demo")
    for note in (done_with, cited, touched, pinned):
        kb("link", str(note), "RELATED", str(task))
    kb("done", str(task))
    monkeypatch.setenv("KB_TODAY", "2026-09-02")
    kb("append", str(touched), "still relevant")
    monkeypatch.setenv("KB_TODAY", "2026-09-12")
    later = add("Fix the import bug", "--tag", "demo")
    kb("link", str(recent), "RELATED", str(later))
    kb("done", str(later))
    open_task = add("An open task", "--tag", "demo")
    open_note = add("Notes on open work", "--kind", "note", "--tag", "demo")
    kb("link", str(open_note), "RELATED", str(open_task))

    monkeypatch.setenv("KB_TODAY", "2026-09-20")
    (archive,) = proposals("archive")
    assert ids(archive) == [done_with]
    assert archive["reason"] == (
        f"every task it is linked to is closed ([{task}], the last on 2026-09-01) "
        "and it has not changed since"
    )
    assert archive["commands"] == [
        f'kb archive {done_with} --reason "its tasks are closed: [{task}]"'
    ]


def test_consolidate_writes_nothing_and_prints_by_action(kb_env, monkeypatch) -> None:
    note = add("A long note", "--kind", "note", "--tag", "demo", "--body", BIG)
    old = add("Use the old exporter", "--kind", "decision", "--tag", "demo")
    new = add("Use the new exporter", "--kind", "decision", "--tag", "demo")
    kb("link", str(new), "SUPERSEDES", str(old))
    before = events_count(kb_env)
    monkeypatch.chdir(kb_env.proj)
    text = kb("consolidate").output.splitlines()
    assert text[0] == (
        "kb consolidate (proj): 0 merge, 1 condense, 1 archive — proposals only, nothing written"
    )
    assert text[1:3] == ["condense:", f"  [{note}] note A long note"]
    assert text[5:7] == ["archive:", f"  [{old}] decision Use the old exporter"]
    data = kb_json("consolidate")
    assert data["scope"] == "proj"
    assert [p["action"] for p in data["proposals"]] == ["condense", "archive"]
    assert set(data["proposals"][0]["records"][0]) == {
        "id",
        "kind",
        "scope",
        "slug",
        "title",
        "tags",
        "bytes",
    }
    assert events_count(kb_env) == before
