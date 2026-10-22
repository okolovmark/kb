"""The tag vocabulary: required area tags, kb tags, kb tags init, edit --tag, kb context."""

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner, Result

from kb import tags
from kb.cli import EXIT_TAGS, main


def kb(*args: str, code: int = 0) -> Result:
    result = CliRunner().invoke(main, list(args))
    assert result.exit_code == code, result.output
    return result


def kb_json(*args: str, code: int = 0) -> Any:
    return json.loads(kb("--json", *args, code=code).stdout)


def add(title: str, *args: str, scope: str = "proj") -> int:
    return kb_json("add", title, "--scope", scope, *args)["id"]


def write_areas(kb_env, text: str) -> Path:
    path = kb_env.proj / ".kb" / "tags.toml"
    path.write_text(text)
    return path


def raw_tags(kb_env, rid: int, value: list[str]) -> None:
    """Tags as the migrations wrote them, outside any vocabulary."""
    kb_env.driver.execute_query(
        "MATCH (r:Record {id: $id}) SET r.tags = $tags", id=rid, tags=value, database_="neo4j"
    )


# --- add --------------------------------------------------------------------------------


def test_add_needs_an_area_tag_outside_personal(kb_env) -> None:
    refused = kb("add", "Untagged", "--scope", "proj", code=4)
    assert refused.output.startswith("Error: kb add needs an area tag in scope proj: --tag AREA;")
    assert f"areas: demo (from {kb_env.proj / '.kb' / 'tags.toml'})" in refused.output
    builtin = "built-in areas: kb, claude_setup, tooling, workflow, writing, career, finance, home"
    assert builtin in refused.output
    assert "types: bug, feature, refactor," in refused.output
    # a type tag says what kind of work it is, not where: still no area
    kb("add", "Only a type", "--scope", "proj", "--tag", "bug", code=4)
    kb("add", "A note", "--kind", "note", "--scope", "proj", code=4)
    kb("add", "Global untagged", "--scope", "global", code=4)
    assert kb_json("add", "Personal untagged", "--scope", "personal")["tags"] == []
    rec = kb_json("add", "Tagged", "--scope", "proj", "--tag", "demo", "--tag", "BUG")
    assert rec["tags"] == ["demo", "bug"]
    assert EXIT_TAGS == 4


@pytest.mark.usefixtures("kb_env")
def test_add_refuses_tags_outside_the_vocabulary() -> None:
    unknown = kb("add", "x", "--scope", "proj", "--tag", "demo", "--tag", "nope", code=4)
    assert unknown.output.startswith("Error: unknown tag nope in scope proj;")
    # project areas belong to the project: global and personal records use the built-in ones
    kb("add", "Global", "--scope", "global", "--tag", "demo", code=4)
    kb("add", "Personal", "--scope", "personal", "--tag", "demo", code=4)
    assert kb_json("add", "Global ok", "--scope", "global", "--tag", "kb")["tags"] == ["kb"]
    assert kb_json("add", "Home", "--scope", "personal", "--tag", "home")["tags"] == ["home"]
    written = kb_json("search", "Global OR Personal OR x")["hits"]
    assert [h["title"] for h in written] == ["Global ok"]  # the refused adds wrote nothing


def test_a_scope_without_a_tags_file_has_the_built_in_areas(kb_env) -> None:
    (kb_env.proj / ".kb" / "tags.toml").unlink()
    refused = kb("add", "x", "--scope", "proj", "--tag", "demo", code=4)
    assert f"no {kb_env.proj / '.kb' / 'tags.toml'} (kb tags init writes one)" in refused.output
    assert kb_json("add", "x", "--scope", "proj", "--tag", "tooling")["tags"] == ["tooling"]


def test_a_broken_tags_file_is_an_error_naming_it(kb_env) -> None:
    path = write_areas(kb_env, "[areas\n")
    assert f"Error: {path}:" in kb("add", "x", "--scope", "proj", "--tag", "kb", code=1).output
    for text, message in (
        ("[area]\ndemo = 'x'\n", "unknown key 'area'"),
        ("[areas]\nDemo = 'x'\n", "lower case letters, digits"),
        ("[areas]\nbug = 'x'\n", "'bug' is a built-in type tag"),
        ("[areas]\ndemo = 1\n", "the description must be a string"),
        ("areas = 'x'\n", "areas must be a table"),
    ):
        write_areas(kb_env, text)
        assert message in kb("add", "x", "--scope", "proj", "--tag", "kb", code=1).output


def test_vocabulary_parse_and_project_descriptions_win() -> None:
    path = Path("tags.toml")
    assert tags.parse("", path) == {}
    assert tags.parse(tags.SEED, path) == {}
    areas = tags.parse('[areas]\nmrp = "manufacturing"\nkb = "our kb fork"\nic-chain = "x"\n', path)
    vocab = tags.Vocabulary("proj", path, areas, found=True)
    assert list(vocab.areas)[:3] == ["mrp", "kb", "ic-chain"]
    assert vocab.areas["kb"] == "our kb fork"
    assert vocab.origin("kb") == "file" and vocab.origin("home") == "built-in"
    assert vocab.area_tags(["bug", "mrp", "migrated", "home"]) == ["mrp", "home"]


# --- edit -------------------------------------------------------------------------------


@pytest.mark.usefixtures("kb_env")
def test_edit_adds_and_removes_tags_with_an_event() -> None:
    rid = str(add("Widget dates", "--tag", "demo"))
    rec = kb_json("edit", rid, "--tag", "bug", "--tag", "Tooling")
    assert rec["tags"] == ["demo", "bug", "tooling"]
    assert kb_json("edit", rid, "--untag", "demo", "--untag", "not-there")["tags"] == [
        "bug",
        "tooling",
    ]
    edited = kb_json("log", rid)["events"][0]
    assert edited["kind"] == "edited"
    assert edited["payload"]["fields"] == ["tags"]
    assert edited["payload"]["before"] == {"tags": ["demo", "bug", "tooling"]}
    assert kb("edit", rid, "--tag", "bug").output.endswith("unchanged\n")
    assert kb("edit", rid, "--tag", "nope", code=4).output.startswith("Error: unknown tag nope")
    last = kb("edit", rid, "--untag", "tooling", code=4)
    assert last.output.startswith(f"Error: [{rid}] would be left without an area tag;")
    assert kb_json("show", rid)["tags"] == ["bug", "tooling"]


def test_old_tags_stay_and_the_vocabulary_is_the_records_own(kb_env) -> None:
    rid = add("Migrated note", "--kind", "note", "--tag", "demo")
    raw_tags(kb_env, rid, ["migrated", "demo"])
    rec = kb_json("edit", str(rid), "--title", "Migrated note, renamed")
    assert rec["tags"] == ["migrated", "demo"]
    assert kb_json("edit", str(rid), "--untag", "migrated")["tags"] == ["demo"]
    # a global record checks against the built-in vocabulary from any cwd
    gid = add("Global", "--tag", "kb", scope="global")
    kb("edit", str(gid), "--tag", "demo", code=4)
    # an untagged record (a synced ticket) is tagged without needing one first
    untagged = add("Untagged", scope="personal")
    raw_tags(kb_env, untagged, [])
    assert kb_json("edit", str(untagged), "--tag", "home")["tags"] == ["home"]


# --- kb tags ----------------------------------------------------------------------------


def test_tags_lists_the_vocabulary_with_counts(kb_env, monkeypatch) -> None:
    write_areas(kb_env, '[areas]\ndemo = "the demo module"\nbilling = "invoices and bills"\n')
    add("One", "--tag", "demo", "--tag", "bug")
    add("Two", "--tag", "demo")
    gone = add("Three", "--tag", "billing")
    kb("archive", str(gone), "--reason", "test")
    raw_tags(kb_env, add("Four", "--tag", "demo"), ["demo", "migrated"])
    add("Global", "--tag", "kb", scope="global")
    add("Hidden", "--tag", "home", scope="personal")
    data = kb_json("tags", "--scope", "proj")
    assert data["scope"] == "proj"
    assert data["file"] == str(kb_env.proj / ".kb" / "tags.toml")
    areas = {a["name"]: (a["origin"], a["count"]) for a in data["areas"]}
    assert list(areas)[:3] == ["demo", "billing", "kb"]
    assert areas["demo"] == ("file", 3)
    assert areas["billing"] == ("file", 0)  # archived records are not counted
    assert areas["kb"] == ("built-in", 1)  # global is in the view
    assert areas["home"] == ("built-in", 0)  # personal is not
    assert {t["name"]: t["count"] for t in data["types"]}["bug"] == 1
    assert data["other"] == [{"name": "migrated", "count": 1}]
    monkeypatch.chdir(kb_env.proj)
    text = kb("tags").output.splitlines()
    assert text[0] == f"kb tags proj: {kb_env.proj / '.kb' / 'tags.toml'}"
    assert text[1] == "areas:"
    assert text[2].split() == ["demo", "3", "the", "demo", "module"]
    kb_line = next(line for line in text[2:] if line.split()[0] == "kb")
    assert kb_line.split()[1] == "1"
    assert kb_line.endswith("its hooks and plugins  (built-in)")
    assert text[-1] == "not in the vocabulary (records keep them, add/edit refuse them): migrated 1"


@pytest.mark.usefixtures("kb_env")
def test_tags_outside_every_scope_needs_a_scope() -> None:
    assert "outside every configured scope" in kb("tags", code=2).output
    data = kb_json("tags", "--scope", "global")
    assert data["file"] is None
    assert [a["origin"] for a in data["areas"]] == ["built-in"] * len(tags.BUILTIN_AREAS)
    assert kb("tags", "--scope", "global").output.startswith("kb tags global: built-in areas\n")


def test_tags_init_writes_a_seed_once(kb_env) -> None:
    path = kb_env.proj / ".kb" / "tags.toml"
    path.unlink()
    (kb_env.proj / ".kb").rmdir()
    assert kb("tags", "init", "--scope", "proj").output == f"wrote {path}\n"
    assert path.read_text() == tags.SEED
    path.write_text('[areas]\nmine = "kept"\n')
    again = kb_json("tags", "init", "--scope", "proj")
    assert again == {"file": str(path), "written": False}
    assert path.read_text() == '[areas]\nmine = "kept"\n'
    assert "has no repo root" in kb("tags", "init", "--scope", "global", code=2).output


# --- kb context -------------------------------------------------------------------------


def test_context_needs_an_area_tag(kb_env) -> None:
    task = add("Ticket from the sync", "--tag", "demo")
    raw_tags(kb_env, task, ["bug"])  # as a synced ticket arrives: no area
    refused = kb("context", str(task), code=4)
    assert refused.output.startswith(
        f"Error: [{task}] has no area tag; tag it first: kb edit {task} --tag AREA;"
    )


def test_context_lists_knowledge_and_open_tasks_sharing_an_area(kb_env) -> None:
    write_areas(kb_env, '[areas]\ndemo = "x"\nbilling = "y"\nother = "z"\n')
    task = add("Fix the invoice dates", "--tag", "demo", "--tag", "billing", "--tag", "bug")
    both = add(
        "Billing and demo gotchas",
        "--kind",
        "reference",
        "--tag",
        "demo",
        "--tag",
        "billing",
        "--summary",
        "read this first",
    )
    one = add("Demo howto", "--kind", "howto", "--tag", "demo")
    add("Elsewhere", "--kind", "note", "--tag", "other")
    gone = add("Old demo note", "--kind", "note", "--tag", "demo")
    kb("archive", str(gone), "--reason", "test")
    add("A bug note", "--kind", "note", "--tag", "other", "--tag", "bug")  # types pull nothing
    glob = add("Global gotcha", "--kind", "feedback", "--tag", "workflow", scope="global")
    raw_tags(kb_env, glob, ["workflow", "demo"])  # an old tag still matches
    hidden = add("Personal", "--kind", "note", "--tag", "home", scope="personal")
    raw_tags(kb_env, hidden, ["demo"])
    sibling = add("Another demo task", "--tag", "demo")
    closed = add("A closed demo task", "--tag", "demo")
    kb("done", str(closed))
    data = kb_json("context", str(task), "--scope", "proj")
    assert data["areas"] == ["demo", "billing"]
    # most shared areas first, then pinned (feedback is), then the most recently updated
    assert [k["id"] for k in data["knowledge"]] == [both, glob, one]
    assert data["knowledge"][0]["areas"] == ["demo", "billing"]
    assert [t["id"] for t in data["tasks"]] == [sibling]
    text = kb("context", str(task), "--scope", "proj").output.splitlines()
    assert text[:4] == [
        f"[{task}] Fix the invoice dates",
        "areas: demo, billing",
        "knowledge (3):",
        f"  [{both}] reference Billing and demo gotchas — read this first",
    ]
    assert text[-2:] == ["open tasks (1):", f"  [{sibling}] Another demo task"]
