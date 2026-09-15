"""kb migrate nodes against tests/fixtures/nodes: five real memory nodes, one file without
frontmatter. The set resolves three link pairs and leaves 14 targets unresolved (links inside
code spans and fences do not count)."""

import datetime as dt
import json
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner, Result

from kb import migrate_nodes
from kb.cli import main
from kb.config import load
from kb.records import RecordError

NODES = Path(__file__).parent / "fixtures" / "nodes"
NOW = dt.datetime(2026, 9, 14, 8, 0, tzinfo=dt.UTC)
UTC = dt.UTC
SUMMARY_LINE = (
    "created 5 records (1 feedback, 1 user notes, 1 project notes, 1 references, 1 howtos), "
    "skipped 0 existing, linked 3 edges, 14 unresolved link targets"
)


def kb(*args: str, code: int = 0) -> Result:
    result = CliRunner().invoke(main, list(args))
    assert result.exit_code == code, result.output
    return result


def kb_json(*args: str):
    return json.loads(kb("--json", *args).stdout)


def record_count(kb_env) -> int:
    rows, _, _ = kb_env.driver.execute_query(
        "MATCH (r:Record) RETURN count(r) AS n", database_="neo4j"
    )
    return rows[0]["n"]


@pytest.fixture
def cfg(tmp_path: Path):
    return load(path=tmp_path / "none.toml", home=tmp_path)  # defaults: Asia/Manila


# --- parsing ----------------------------------------------------------------------------


def test_parse_frontmatter_subset() -> None:
    text = (
        "---\n"
        "name: x\n"
        'description: "a \\"quoted\\" one — with ** and `ticks`"\n'
        "metadata: \n"
        "  node_type: memory\n"
        "  type: reference\n"
        "  tags: \n"
        "    - mcp\n"
        "    - uvx\n"
        "  modified: 2026-07-31T03:47:26.949Z\n"
        "---\n"
        "# Title\n\nbody\n"
    )
    front, body = migrate_nodes.parse_frontmatter(text)
    assert front["name"] == "x"
    assert front["description"] == 'a "quoted" one — with ** and `ticks`'
    assert front["metadata"]["type"] == "reference"
    assert front["metadata"]["tags"] == ["mcp", "uvx"]
    assert front["metadata"]["modified"] == "2026-07-31T03:47:26.949Z"
    assert body == "# Title\n\nbody\n"
    assert migrate_nodes.parse_frontmatter("# no frontmatter\n") is None
    flat, _ = migrate_nodes.parse_frontmatter(
        "---\nname: y\ntype: user\ncreated: 2026-01-02\n---\nb"
    )
    assert flat == {"name": "y", "type": "user", "created": "2026-01-02"}


def test_kind_mapping_pinned_title_summary_and_dates(cfg) -> None:
    nodes, skipped = migrate_nodes.load_dir(NODES, cfg, NOW)
    assert skipped == ["no_frontmatter.md"]
    by = {n.slug: n for n in nodes}
    assert sorted(by) == [
        "conventions_node_format",
        "feedback_scope_discipline",
        "project_kb_tool",
        "reference_mcp_sdk_2_fastmcp_removed",
        "user_role",
    ]

    fb = by["feedback_scope_discipline"]
    assert (fb.kind, fb.pinned, fb.node_type) == ("feedback", True, "feedback")
    assert fb.title == "Scope discipline"  # no heading: the slug minus its type prefix
    assert fb.summary.startswith("Unrelated improvements become open threads")
    assert fb.body.startswith("When Claude spots an unrelated improvement")
    assert fb.tags == ["feedback", "migrated"]
    assert fb.created_at == dt.datetime(2026, 4, 8, 16, 0, tzinfo=UTC)  # 2026-04-09 Manila
    assert fb.updated_at == dt.datetime(2026, 8, 17, 16, 0, tzinfo=UTC)  # updated beats modified

    user = by["user_role"]
    assert (user.kind, user.pinned, user.tags) == ("note", True, ["user", "migrated"])
    assert user.title == "User — role and preferences"
    assert user.body.startswith("## Role")  # the h1 went into the title, the h2 stays
    assert user.updated_at == dt.datetime(2026, 9, 10, 16, 0, tzinfo=UTC)
    assert user.targets.count("feedback_scope_discipline") == 1  # cited twice, one target

    project = by["project_kb_tool"]
    assert (project.kind, project.pinned, project.tags) == ("note", False, ["project", "migrated"])
    assert project.title == "kb: one graph for tasks, notes and sessions"
    assert project.summary.startswith("kb — the user's personal knowledge graph")
    assert project.body.startswith("**Status 2026-09-11:**")
    assert project.created_at == project.updated_at == dt.datetime(2026, 9, 10, 16, 0, tzinfo=UTC)
    assert set(project.targets) == {
        "reference_entire_session_recording",
        "user_role",
        "reference_nixodoo_copier_template",
    }  # `[[slug]]` and `[[wikilinks]]` sit in code spans; the self-link is dropped

    ref = by["reference_mcp_sdk_2_fastmcp_removed"]
    assert (ref.kind, ref.pinned, ref.tags) == ("reference", False, ["reference", "migrated"])
    assert ref.summary == (
        "uvx-launched MCP servers die with \"No module named 'mcp.server.fastmcp'\" — "
        "mcp SDK 2.0.0 dropped the module; pin --with 'mcp<2'"
    )
    assert ref.created_at == dt.datetime(2026, 7, 30, 16, 0, tzinfo=UTC)
    assert ref.updated_at == dt.datetime(2026, 7, 31, 3, 47, 26, 949000, tzinfo=UTC)  # modified

    howto = by["conventions_node_format"]
    assert (howto.kind, howto.pinned, howto.node_type) == ("howto", False, "reference")
    assert howto.targets == ("conventions_behavior_protocol",)  # the rest are examples in code
    assert howto.summary == "Rules for writing and linking memory nodes in this project"


@pytest.mark.parametrize(
    ("slug", "title"),
    [
        ("feedback_attach_prod_scripts", "Attach prod scripts"),
        ("reference_kio1064_e2e_harness", "Kio1064 e2e harness"),
        ("conventions_node_format", "Node format"),
        ("user_role", "Role"),
        ("project_kb_tool", "Kb tool"),
        ("plain_slug", "Plain slug"),
        ("feedback_", "feedback_"),  # nothing left: the slug itself
    ],
)
def test_title_from_slug(slug: str, title: str) -> None:
    assert migrate_nodes.title_from_slug(slug) == title


def test_missing_type_and_dates_fall_back(cfg, tmp_path: Path) -> None:
    (tmp_path / "bare.md").write_text("---\nname: bare\n---\n\n  \nJust a body.\n")
    node = migrate_nodes.parse_node(tmp_path / "bare.md", cfg, NOW)
    assert (node.kind, node.pinned, node.tags, node.title) == ("note", False, ["migrated"], "Bare")
    assert node.summary is None
    assert node.created_at == node.updated_at == NOW
    assert node.dated_now is True
    assert node.body == "Just a body.\n"  # leading blank lines go, heading or not
    (tmp_path / "stem_only.md").write_text(
        "---\ndescription: d\nmetadata:\n  type: user\n  modified: 2026-05-01\n---\n"
        "\n# Heading\n\ntext [[stem_only]] [[other]] [[Not-a-slug!]]\n"
    )
    node = migrate_nodes.parse_node(tmp_path / "stem_only.md", cfg, NOW)
    assert (node.slug, node.title, node.kind, node.pinned) == ("stem_only", "Heading", "note", True)
    assert node.body == "text [[stem_only]] [[other]] [[Not-a-slug!]]\n"
    assert node.targets == ("other",)
    assert node.created_at == node.updated_at == dt.datetime(2026, 4, 30, 16, 0, tzinfo=UTC)
    assert node.dated_now is False
    assert migrate_nodes.parse_node(NODES / "no_frontmatter.md", cfg, NOW) is None


def test_links_inside_code_do_not_count() -> None:
    body = (
        "Prose [[real]] and `[[inline]]` and ``[[double]]``.\n"
        "```md\nfenced [[fenced]] ](nodes/fenced_md.md)\n```\n"
        "  ~~~\n[[tilde]]\n  ~~~\n"
        "After the fence [[after]] and [text](nodes/md_link.md).\n"
    )
    assert migrate_nodes.link_targets(body, "me") == ("real", "after", "md_link")


def test_slug_not_in_slug_form_is_refused(cfg, tmp_path: Path) -> None:
    (tmp_path / "reference_KIO-1.md").write_text("---\nname: reference_KIO-1\n---\nbody\n")
    (tmp_path / "ok.md").write_text("---\nname: ok\n---\nbody\n")
    with pytest.raises(RecordError) as err:
        migrate_nodes.load_dir(tmp_path, cfg, NOW)
    assert str(err.value) == (
        "reference_KIO-1.md: slug 'reference_KIO-1' is not in slug form ('reference_kio_1'); "
        "rename it first (1 such files)"
    )


def test_scope_file_parsing(tmp_path: Path) -> None:
    f = tmp_path / "scopes.txt"
    f.write_text("# slug scope\nuser_role   personal\n\nproject_kb_tool\tglobal  # trailing\n")
    assert migrate_nodes.read_scope_file(f) == {
        "user_role": "personal",
        "project_kb_tool": "global",
    }
    f.write_text("user_role personal extra\n")
    with pytest.raises(RecordError, match="expected 'slug scope'"):
        migrate_nodes.read_scope_file(f)
    f.write_text("user_role personal\nuser_role global\n")
    with pytest.raises(RecordError, match=r"scopes.txt:2: user_role listed twice"):
        migrate_nodes.read_scope_file(f)


def test_duplicate_slug_is_an_error(cfg, tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("---\nname: same\n---\nA\n")
    (tmp_path / "b.md").write_text("---\nname: same\n---\nB\n")
    with pytest.raises(RecordError, match="duplicate slug"):
        migrate_nodes.load_dir(tmp_path, cfg, NOW)


# --- the command ------------------------------------------------------------------------


@pytest.mark.usefixtures("kb_env")
def test_dry_run_prints_the_plan_and_writes_nothing(kb_env) -> None:
    result = kb("migrate", "nodes", str(NODES), "--scope", "proj", "--dry-run")
    lines = result.output.splitlines()
    plan = {line.split()[0]: line for line in lines[:5]}
    assert plan["conventions_node_format"].split()[1:6] == ["howto", "proj", "-", "0", "links"]
    assert plan["conventions_node_format"].endswith("Node format and conventions")
    assert plan["feedback_scope_discipline"].split()[1:6] == [
        "feedback",
        "proj",
        "pinned",
        "1",
        "links",
    ]
    assert plan["user_role"].split()[1:6] == ["note", "proj", "pinned", "2", "links"]
    assert plan["reference_mcp_sdk_2_fastmcp_removed"].split()[1:6] == [
        "reference",
        "proj",
        "-",
        "0",
        "links",
    ]
    assert "## Skipped, no frontmatter (1)" in result.output
    assert "- no_frontmatter.md" in result.output
    assert "## Unresolved link targets (14)" in result.output
    assert (
        "- conventions_behavior_protocol ← 3 files: conventions_node_format.md, "
        "feedback_scope_discipline.md, user_role.md"
    ) in result.output
    assert "- reference_mcpscore_audit ← 1 file: feedback_scope_discipline.md" in result.output
    assert "## Dated now, no date in the frontmatter (0)" in result.output
    assert lines[-1] == (
        "would create 5 records (1 feedback, 1 user notes, 1 project notes, 1 references, "
        "1 howtos), skip 0 existing, link 3 edges, 14 unresolved link targets"
    )
    assert record_count(kb_env) == 0
    as_json = kb_json("migrate", "nodes", str(NODES), "--scope", "proj", "--dry-run")
    assert len(as_json["plan"]) == 5 and as_json["linked"] == 3
    assert as_json["unresolved"]["reference_nixodoo_copier_template"] == [
        "project_kb_tool.md",
        "reference_mcp_sdk_2_fastmcp_removed.md",
    ]
    assert as_json["dated_now"] == []


@pytest.mark.usefixtures("kb_env")
def test_scope_errors_come_before_any_write(kb_env, tmp_path: Path) -> None:
    bad = kb("migrate", "nodes", str(NODES), "--scope", "nope", code=2)
    assert "unknown scope 'nope'" in bad.output
    scope_file = tmp_path / "scopes.txt"
    scope_file.write_text("user_role personal\nproject_kb_tool elsewhere\n")
    bad = kb(
        "migrate", "nodes", str(NODES), "--scope", "proj", "--scope-file", str(scope_file), code=2
    )
    assert "project_kb_tool: unknown scope 'elsewhere'" in bad.output
    assert record_count(kb_env) == 0


def test_migrate_creates_records_events_and_links(kb_env, tmp_path: Path) -> None:
    scope_file = tmp_path / "scopes.txt"
    scope_file.write_text("# personal notes\nuser_role personal\nnot_in_dir global\n")
    result = kb("migrate", "nodes", str(NODES), "--scope", "proj", "--scope-file", str(scope_file))
    assert result.stderr.strip() == f"scope file: 1 slugs not in {NODES}: not_in_dir"
    assert result.stdout.splitlines()[-1] == SUMMARY_LINE
    assert "## Created (5)" in result.stdout
    assert "- feedback_scope_discipline  feedback  pinned" in result.stdout
    assert "- user_role  note  pinned" in result.stdout
    assert record_count(kb_env) == 5

    fb = kb_json("show", "feedback_scope_discipline")
    assert (fb["kind"], fb["scope"], fb["pinned"]) == ("feedback", "proj", True)
    assert fb["tags"] == ["feedback", "migrated"]
    assert fb["title"] == "Scope discipline"
    assert fb["summary"].startswith("Unrelated improvements become open threads")
    assert fb["body"].startswith("When Claude spots an unrelated improvement")
    assert fb["created_at"] == "2026-04-08T16:00:00+00:00"
    assert fb["updated_at"] == "2026-08-17T16:00:00+00:00"
    assert [(e["kind"], e["note"]) for e in fb["events"]] == [
        ("linked", None),
        ("edited", "last markdown update"),
        ("created", "migrated from nodes/feedback_scope_discipline.md"),
    ]
    assert fb["events"][1]["at"] == "2026-08-17T16:00:00+00:00"
    assert fb["events"][2]["at"] == "2026-04-08T16:00:00+00:00"
    assert fb["events"][0]["at"].startswith("2026-09-14")  # the link pass runs now
    assert [(lk["type"], lk["direction"]) for lk in fb["links"]] == [("RELATED", "->")]

    user = kb_json("show", "user_role")
    assert (user["kind"], user["scope"], user["pinned"]) == ("note", "personal", True)
    assert user["title"] == "User — role and preferences"
    assert user["tags"] == ["user", "migrated"]
    # two pairs were made from the other side; the third (to the howto) from user_role itself
    assert [e["kind"] for e in user["events"]] == ["linked", "edited", "created"]
    assert sorted((lk["direction"], lk["title"]) for lk in user["links"]) == [
        ("->", "Node format and conventions"),
        ("<-", "Scope discipline"),
        ("<-", "kb: one graph for tasks, notes and sessions"),
    ]
    assert all(lk["type"] == "RELATED" for lk in user["links"])

    project = kb_json("show", "project_kb_tool")
    assert (project["kind"], project["pinned"], project["tags"]) == (
        "note",
        False,
        ["project", "migrated"],
    )
    assert project["created_at"] == project["updated_at"] == "2026-09-10T16:00:00+00:00"
    assert [e["kind"] for e in project["events"]] == ["linked", "created"]  # no edited: same day
    assert "[[reference_nixodoo_copier_template]]" in project["body"]  # unresolved stays text

    ref = kb_json("show", "reference_mcp_sdk_2_fastmcp_removed")
    assert (ref["kind"], ref["pinned"]) == ("reference", False)
    assert ref["updated_at"] == "2026-07-31T03:47:26.949000+00:00"
    assert [e["kind"] for e in ref["events"]] == ["edited", "created"]
    assert ref["links"] == []

    howto = kb_json("show", "conventions_node_format")
    assert (howto["kind"], howto["pinned"], howto["tags"]) == (
        "howto",
        False,
        ["reference", "migrated"],
    )
    assert [e["kind"] for e in howto["events"]] == ["created"]  # its only real link came inbound
    linked = [e for e in user["events"] if e["kind"] == "linked"]
    assert linked[0]["payload"] == {"type": "RELATED", "target": howto["id"]}

    # the pinned migrated records lead the index, summaries (cut at 160) in place of titles
    index = kb("index", "--scope", "proj").output.splitlines()
    assert index[2] == "## Pinned"
    assert index[3] == (
        f"- [{fb['id']}] feedback_scope_discipline — "
        "Unrelated improvements become open threads in state.md, not opportunistic fixes; a bug "
        "found while testing a ticket's branch is committed to THAT branch/PR, nev…"
    )
    assert f"[{user['id']}]" not in "\n".join(index)  # personal is hidden in a project scope


def test_second_run_skips_everything(kb_env) -> None:
    kb("migrate", "nodes", str(NODES), "--scope", "proj")
    again = kb("migrate", "nodes", str(NODES), "--scope", "proj")
    assert again.stdout.splitlines()[-1] == (
        "created 0 records (0 feedback, 0 user notes, 0 project notes, 0 references, 0 howtos), "
        "skipped 5 existing, linked 0 edges, 14 unresolved link targets"
    )
    assert "## Skipped, already in the store (5)" in again.stdout
    assert record_count(kb_env) == 5
    assert [e["kind"] for e in kb_json("show", "user_role")["events"]] == [
        "linked",
        "edited",
        "created",
    ]


def test_rerun_links_records_migrated_earlier_to_a_new_target(kb_env, tmp_path: Path) -> None:
    nodes = tmp_path / "nodes"
    shutil.copytree(NODES, nodes)
    kb("migrate", "nodes", str(nodes), "--scope", "proj")
    (nodes / "conventions_behavior_protocol.md").write_text(
        "---\nname: conventions_behavior_protocol\ndescription: the protocol\n"
        "metadata:\n  type: reference\n  created: 2026-04-09\n---\n# Behavior protocol\n\n"
        "See [[conventions_node_format]] and [[user_role]].\n"
    )
    result = kb("migrate", "nodes", str(nodes), "--scope", "global")
    # the new record pairs with the 3 files that cited it (2 of them it cites back); 19 left
    assert result.stdout.splitlines()[-1] == (
        "created 1 records (0 feedback, 0 user notes, 0 project notes, 0 references, 1 howtos), "
        "skipped 5 existing, linked 3 edges, 13 unresolved link targets"
    )
    protocol = kb_json("show", "conventions_behavior_protocol")
    assert (protocol["kind"], protocol["scope"], protocol["summary"]) == (
        "howto",
        "global",
        "the protocol",
    )
    assert sorted((lk["direction"], lk["title"]) for lk in protocol["links"]) == [
        ("->", "Node format and conventions"),
        ("->", "User — role and preferences"),
        ("<-", "Scope discipline"),
    ]
    # a record migrated earlier gets the edge and the event; user_role's pair came the other way
    assert [e["kind"] for e in kb_json("show", "feedback_scope_discipline")["events"]] == [
        "linked",
        "linked",
        "edited",
        "created",
    ]
    assert [e["kind"] for e in kb_json("show", "user_role")["events"]] == [
        "linked",
        "edited",
        "created",
    ]


def test_report_file_and_json(kb_env, tmp_path: Path) -> None:
    report = tmp_path / "report.md"
    result = kb("migrate", "nodes", str(NODES), "--scope", "proj", "--report", str(report))
    assert result.stdout.strip() == SUMMARY_LINE
    text = report.read_text()
    assert text.startswith("# kb migrate nodes\n\n## Created (5)\n")
    assert "## Unresolved link targets (14)" in text
    assert "- reference_nixodoo_copier_template ← 2 files: project_kb_tool.md, " in text
    assert text.endswith("\n")
    kb_env.driver.execute_query(
        "MATCH (n) WHERE n:Record OR n:Event DETACH DELETE n", database_="neo4j"
    )
    as_json = kb_json("migrate", "nodes", str(NODES), "--scope", "proj")
    assert [c["slug"] for c in as_json["created"]] == [
        "conventions_node_format",
        "feedback_scope_discipline",
        "project_kb_tool",
        "reference_mcp_sdk_2_fastmcp_removed",
        "user_role",
    ]
    assert as_json["no_frontmatter"] == ["no_frontmatter.md"]
    assert as_json["linked"] == 3 and len(as_json["unresolved"]) == 14


def test_report_into_a_missing_directory_fails_before_writing(kb_env, tmp_path: Path) -> None:
    missing = tmp_path / "nope" / "report.md"
    result = kb("migrate", "nodes", str(NODES), "--scope", "proj", "--report", str(missing), code=1)
    assert result.output.strip() == f"Error: not found: {tmp_path / 'nope'}"
    assert record_count(kb_env) == 0


@pytest.mark.usefixtures("kb_env")
def test_dated_now_files_are_reported(tmp_path: Path) -> None:
    nodes = tmp_path / "nodes"
    nodes.mkdir()
    (nodes / "undated.md").write_text("---\nname: undated\n---\nbody [[dated]]\n")
    (nodes / "dated.md").write_text("---\nname: dated\nmetadata:\n  created: 2026-01-01\n---\nb\n")
    out = kb("migrate", "nodes", str(nodes), "--scope", "proj", "--dry-run").output
    assert "## Dated now, no date in the frontmatter (1)\n- undated.md" in out
    real = kb("migrate", "nodes", str(nodes), "--scope", "proj")
    assert "## Dated now, no date in the frontmatter (1)\n- undated.md" in real.output
    rec = kb_json("show", "undated")
    assert rec["created_at"] == rec["updated_at"] and rec["created_at"].startswith("2026-09-14")


@pytest.mark.usefixtures("kb_env")
def test_empty_directory_and_help(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    assert kb("migrate", "nodes", str(tmp_path / "empty"), "--scope", "proj").output.strip() == (
        f"{tmp_path / 'empty'}: no node files found"
    )
    assert kb("migrate", "nodes", "--help").exit_code == 0
