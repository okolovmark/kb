import json
import tomllib
from pathlib import Path

import pytest
from click.testing import CliRunner

from kb import scopes
from kb.cli import EXIT_NO_SCOPE, EXIT_SCOPE_CONFLICT, App, main
from kb.config import load, render_default

HAND_EDITED = """\
[general]
timezone = "Asia/Manila"   # my own comment

[scopes]                   # name = absolute path
acme-erp = "~/projects/acme-erp"
# old-thing = "~/projects/old"

[sources.odoo]
enabled = false
"""


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    home = tmp_path / "home"
    (home / "projects" / "acme-erp" / "src").mkdir(parents=True)
    (home / "projects" / "shop").mkdir(parents=True)
    file = home / ".config" / "kb" / "config.toml"
    monkeypatch.setenv("KB_CONFIG", str(file))
    monkeypatch.chdir(home)
    return home, file


def run(home: Path, *args: str):
    return CliRunner().invoke(main, list(args), obj=App(home=home))


def test_add_inserts_one_line_and_keeps_the_rest(env: tuple[Path, Path]) -> None:
    home, file = env
    file.parent.mkdir(parents=True)
    file.write_text(HAND_EDITED)
    result = run(home, "scope", "add", "shop", str(home / "projects" / "shop"))
    assert result.exit_code == 0, result.output
    assert result.output.strip() == f"scope shop added: ~/projects/shop ({file})"
    after = file.read_text()
    assert after.replace('shop = "~/projects/shop"\n', "") == HAND_EDITED
    lines = after.splitlines()
    assert (
        lines.index('shop = "~/projects/shop"') == lines.index('# old-thing = "~/projects/old"') + 1
    )
    assert load(path=file, home=home).scopes["shop"] == home / "projects" / "shop"


def test_add_is_idempotent(env: tuple[Path, Path]) -> None:
    home, file = env
    file.parent.mkdir(parents=True)
    file.write_text(HAND_EDITED)
    result = run(home, "scope", "add", "acme-erp", "~/projects/acme-erp")
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "scope acme-erp already ~/projects/acme-erp"
    assert file.read_text() == HAND_EDITED


def test_add_relative_path_and_default_cwd(env: tuple[Path, Path], monkeypatch) -> None:
    home, file = env
    monkeypatch.chdir(home / "projects" / "shop")
    result = run(home, "--json", "scope", "add", "shop")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        "action": "added",
        "name": "shop",
        "path": "~/projects/shop",
        "file": str(file),
    }
    # a missing file starts from the default config, comments included
    text = file.read_text()
    assert text.startswith(render_default().splitlines()[0])
    assert tomllib.loads(text)["scopes"] == {"shop": "~/projects/shop"}


def test_add_refuses_a_name_pointing_elsewhere_unless_forced(env: tuple[Path, Path]) -> None:
    home, file = env
    file.parent.mkdir(parents=True)
    file.write_text(HAND_EDITED)
    refused = run(home, "scope", "add", "acme-erp", "~/projects/shop")
    assert refused.exit_code == EXIT_SCOPE_CONFLICT
    assert "already points at ~/projects/acme-erp" in refused.output
    assert file.read_text() == HAND_EDITED
    forced = run(home, "scope", "add", "acme-erp", "~/projects/shop", "--force")
    assert forced.exit_code == 0, forced.output
    assert forced.output.strip() == "scope acme-erp now points at ~/projects/shop"
    assert file.read_text() == HAND_EDITED.replace("~/projects/acme-erp", "~/projects/shop")


def test_add_never_takes_a_path_from_another_scope(env: tuple[Path, Path]) -> None:
    home, file = env
    file.parent.mkdir(parents=True)
    file.write_text(HAND_EDITED)
    for extra in ([], ["--force"]):
        result = run(home, "scope", "add", "erp2", "~/projects/acme-erp", *extra)
        assert result.exit_code == EXIT_SCOPE_CONFLICT
        assert "already the scope 'acme-erp'" in result.output
    assert file.read_text() == HAND_EDITED


@pytest.mark.parametrize("name", ["global", "personal", "all", "my scope", "a.b"])
def test_add_refuses_reserved_and_bad_names(env: tuple[Path, Path], name: str) -> None:
    home, file = env
    result = run(home, "scope", "add", name, "~/projects/shop")
    assert result.exit_code == 2
    assert not file.exists()


def test_add_creates_the_table_when_absent(env: tuple[Path, Path]) -> None:
    home, file = env
    file.parent.mkdir(parents=True)
    file.write_text("[neo4j]\nbolt_port = 4242")
    assert run(home, "scope", "add", "shop", "~/projects/shop").exit_code == 0
    assert file.read_text() == '[neo4j]\nbolt_port = 4242\n\n[scopes]\nshop = "~/projects/shop"\n'


def test_which_finds_the_longest_match(env: tuple[Path, Path]) -> None:
    home, file = env
    file.parent.mkdir(parents=True)
    file.write_text('[scopes]\nprojects = "~/projects"\nacme-erp = "~/projects/acme-erp"\n')
    assert run(home, "scope", "which", "~/projects/acme-erp/src").output.strip() == "acme-erp"
    assert run(home, "scope", "which", "~/projects/shop").output.strip() == "projects"
    none = run(home, "--json", "scope", "which", str(home))
    assert none.exit_code == EXIT_NO_SCOPE
    assert json.loads(none.output) == {"scope": None, "path": "~"}


def test_which_defaults_to_the_cwd(env: tuple[Path, Path], monkeypatch) -> None:
    home, file = env
    file.parent.mkdir(parents=True)
    file.write_text(HAND_EDITED)
    monkeypatch.chdir(home / "projects" / "acme-erp" / "src")
    result = run(home, "scope", "which")
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "acme-erp"


def test_list_marks_missing_paths(env: tuple[Path, Path]) -> None:
    home, file = env
    file.parent.mkdir(parents=True)
    file.write_text('[scopes]\nacme-erp = "~/projects/acme-erp"\ngone = "~/projects/gone"\n')
    result = run(home, "scope", "list")
    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [
        "acme-erp  ~/projects/acme-erp",
        "gone      ~/projects/gone  (missing)",
    ]
    rows = json.loads(run(home, "--json", "scope", "list").output)["scopes"]
    assert rows == [
        {"name": "acme-erp", "path": "~/projects/acme-erp", "exists": True},
        {"name": "gone", "path": "~/projects/gone", "exists": False},
    ]


def test_list_without_scopes(env: tuple[Path, Path]) -> None:
    home, _ = env
    assert run(home, "scope", "list").output.strip() == (
        "no scopes configured: kb scope add NAME PATH"
    )


def test_scope_of_matches_kb_cwd_resolution(tmp_path: Path) -> None:
    root = tmp_path / "a"
    (root / "b" / "c").mkdir(parents=True)
    table = {"outer": root, "inner": root / "b"}
    assert scopes.scope_of(table, root / "b" / "c") == "inner"
    assert scopes.scope_of(table, root) == "outer"
    assert scopes.scope_of(table, tmp_path) is None
