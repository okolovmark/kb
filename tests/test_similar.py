"""Search over the summary, the escaped retry, and the duplicate check of add."""

import json
from typing import Any

import pytest
from click.testing import CliRunner, Result

from conftest import with_area
from kb import similar
from kb.cli import EXIT_SIMILAR, main
from kb.records import lucene_escape


def kb(*args: str, code: int = 0) -> Result:
    result = CliRunner().invoke(main, with_area(list(args)))
    assert result.exit_code == code, result.output
    return result


def kb_json(*args: str, code: int = 0) -> Any:
    return json.loads(kb("--json", *args, code=code).stdout)


def add(title: str, *args: str) -> int:
    return kb_json("add", title, "--scope", "proj", *args)["id"]


def count_records(kb_env) -> int:
    rows, _, _ = kb_env.driver.execute_query(
        "MATCH (r:Record) RETURN count(r) AS n", database_="neo4j"
    )
    return rows[0]["n"]


# --- search -----------------------------------------------------------------------------


def test_lucene_escape_leaves_words_and_neutralises_syntax() -> None:
    assert lucene_escape("plain words") == "plain words"
    assert lucene_escape("/srv full") == "\\/srv full"
    assert lucene_escape('a "b (c) [d] {e} ^f ~g *h ?i :j \\k + -l !m') == (
        'a \\"b \\(c\\) \\[d\\] \\{e\\} \\^f \\~g \\*h \\?i \\:j \\\\k \\+ \\-l \\!m'
    )
    assert lucene_escape("x && y || z") == "x \\&& y \\|| z"
    assert lucene_escape("cats AND dogs OR NOT birds") == "cats and dogs or not birds"
    assert lucene_escape("ANDROID ORDER") == "ANDROID ORDER"  # whole words only


@pytest.mark.usefixtures("kb_env")
def test_search_finds_the_summary() -> None:
    rid = add("Payment terms", "--kind", "reference", "--summary", "shared across companies")
    hits = kb_json("search", "companies")["hits"]
    assert [h["id"] for h in hits] == [rid]


@pytest.mark.usefixtures("kb_env")
def test_search_keeps_lucene_syntax() -> None:
    first = add("Alpha widget")
    second = add("Beta gadget")
    assert {h["id"] for h in kb_json("search", "alpha OR beta")["hits"]} == {first, second}
    assert [h["id"] for h in kb_json("search", "title:alpha")["hits"]] == [first]
    result = kb_json("search", "alp*")
    assert [h["id"] for h in result["hits"]] == [first]
    assert result["plain_text"] is False


@pytest.mark.usefixtures("kb_env")
def test_a_query_lucene_rejects_is_searched_as_plain_text() -> None:
    rid = add("Scratch space", "--kind", "note", "--body", "/srv is a small disk")
    result = kb("search", "/srv disk")  # an unterminated /regex/
    assert result.stderr == "kb search: not valid Lucene syntax, searched as plain text\n"
    assert result.stdout.startswith(f"[{rid}] note scratch_space Scratch space")
    as_json = CliRunner().invoke(main, ["--json", "search", 'unbalanced "quote'])
    assert as_json.exit_code == 0, as_json.output
    assert json.loads(as_json.stdout) == {"hits": [], "plain_text": True}


# --- similarity -------------------------------------------------------------------------


def test_cosine_weighs_rare_words_and_ignores_common_ones() -> None:
    corpus = similar.Corpus(
        [
            "the cron has no savepoint",
            "the widget is broken",
            "the report is slow",
            "the export fails",
        ]
    )
    same = similar.words("cron savepoint")
    assert corpus.cosine(same, same) == pytest.approx(1.0)
    assert corpus.cosine(same, similar.words("the widget")) == 0.0
    assert corpus.cosine(similar.words("the cron"), similar.words("the report")) < 0.1
    assert corpus.cosine(set(), same) == 0.0
    assert similar.words("KIO-1001 re-net Cron_job") == {"kio", "1001", "re", "net", "cron", "job"}


# --- the duplicate check of add ---------------------------------------------------------


def test_add_refuses_a_similar_open_task(kb_env) -> None:
    old = add("The re-net cron has no savepoint per master")
    add("Write the release notes")
    before = count_records(kb_env)
    refused = kb("add", "Re-net cron: no savepoint per master", "--scope", "proj", code=5)
    assert refused.stdout == ""
    assert refused.stderr.splitlines() == [
        f"similar: [{old}] task The re-net cron has no savepoint per master",
        f"hint: kb append {old} TEXT, or re-run with --distinct",
    ]
    assert count_records(kb_env) == before  # nothing written, not even an event
    data = kb_json("add", "Re-net cron: no savepoint per master", "--scope", "proj", code=5)
    assert [(s["id"], s["kind"], s["scope"]) for s in data["similar"]] == [(old, "task", "proj")]
    assert 0.4 <= data["similar"][0]["similarity"] <= 1
    assert EXIT_SIMILAR == 5


@pytest.mark.usefixtures("kb_env")
def test_distinct_creates_it_and_the_created_event_names_the_similar() -> None:
    old = add("Ship the widget to the warehouse")
    new = add("Ship the widget to the warehouse", "--distinct")
    created = kb_json("log", str(new))["events"][-1]
    assert created["kind"] == "created"
    assert created["payload"] == {"distinct": True, "similar": [old]}
    lone = add("Something nobody wrote before", "--distinct")
    assert kb_json("log", str(lone))["events"][-1]["payload"] == {"distinct": True, "similar": []}
    plain = add("Unrelated chore")
    assert kb_json("log", str(plain))["events"][-1]["payload"] is None


@pytest.mark.usefixtures("kb_env")
def test_only_open_records_of_the_same_family_count() -> None:
    done = add("Rotate the backup keys")
    kb("done", str(done))
    archived = add("Clean the build cache", "--kind", "note")
    kb("archive", str(archived), "--reason", "gone")
    add("Rotate the backup keys")  # the done task no longer counts
    add("Clean the build cache", "--kind", "note")  # nor the archived note
    # a note on the same subject as a task is its context, not its duplicate
    add("Renew the TLS certificate")
    add("Renew the TLS certificate", "--kind", "howto")
    kb("add", "Renew the TLS certificate", "--kind", "reference", "--scope", "proj", code=5)


@pytest.mark.usefixtures("kb_env")
def test_the_check_looks_at_the_callers_view() -> None:
    kb("add", "Book the dentist appointment", "--scope", "personal")
    add("Book the dentist appointment")  # personal is outside the proj view
    kb("add", "Tidy the shared glossary", "--scope", "global")
    refused = kb("add", "Tidy the shared glossary", "--scope", "proj", code=5)
    assert "similar: [" in refused.stderr


@pytest.mark.usefixtures("kb_env")
def test_at_most_three_similar_records_are_listed() -> None:
    ids = [add("Fix the invoice report totals", "--distinct") for _ in range(5)]
    refused = kb("add", "Fix the invoice report totals", "--scope", "proj", code=5)
    lines = refused.stderr.splitlines()
    assert [line for line in lines if line.startswith("similar:")] == [
        f"similar: [{i}] task Fix the invoice report totals" for i in ids[:3]
    ]
    assert lines[-1] == f"hint: kb append {ids[0]} TEXT, or re-run with --distinct"
