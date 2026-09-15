import tomllib
from pathlib import Path

import pytest
from click.testing import CliRunner

from kb.cli import main
from kb.config import ConfigError, dump_toml, load, render_default


def test_defaults_when_file_is_absent(tmp_path: Path) -> None:
    cfg = load(path=tmp_path / "missing.toml", home=tmp_path)
    assert cfg.general.timezone == "Asia/Manila"
    assert not hasattr(cfg.general, "language")
    assert cfg.neo4j.bolt_port == 7687
    assert cfg.neo4j.http_port == 7474
    assert cfg.neo4j.data_dir == tmp_path / ".local" / "share" / "kb"
    assert cfg.neo4j.start_timeout == 30
    assert cfg.neo4j.heap_max == "512m"
    assert cfg.scopes == {}
    assert cfg.sources.odoo.enabled is False
    assert cfg.sources.odoo.url == ""
    assert cfg.sources.odoo.database == ""
    assert cfg.sources.odoo.username == ""
    assert cfg.sources.odoo.assignee_login == ""
    assert cfg.sources.odoo.password_file == tmp_path / ".config" / "kb" / "odoo-auth"
    assert cfg.sources.odoo.project_id == 0
    assert cfg.sources.odoo.scope == ""
    assert cfg.sources.odoo.timeout == 10
    assert cfg.index.budget_lines == 200
    assert cfg.calendar.country == "PH"
    assert cfg.calendar.extra_days_off == ()
    assert (cfg.escalation.hard.whisper, cfg.escalation.hard.normal, cfg.escalation.hard.loud) == (
        14,
        7,
        2,
    )
    assert (cfg.escalation.age.whisper, cfg.escalation.age.normal, cfg.escalation.age.loud) == (
        5,
        10,
        15,
    )
    assert cfg.escalation.window.skips_to_backlog == 2
    assert cfg.backup.dir == tmp_path / "kb-backups"


def test_render_default_parses_and_equals_defaults(tmp_path: Path) -> None:
    data = tomllib.loads(render_default())
    assert data["neo4j"]["bolt_port"] == 7687
    assert data["scopes"] == {}
    file = tmp_path / "config.toml"
    file.write_text(render_default())
    assert load(path=file, home=tmp_path) == load(path=tmp_path / "none", home=tmp_path)


def test_the_docs_carry_the_default_config_verbatim() -> None:
    """docs/configuration.md quotes the default file, so a new key cannot go undocumented."""
    doc = (Path(__file__).parent.parent / "docs" / "configuration.md").read_text()
    assert f"```toml\n{render_default()}```" in doc


def test_override_file_and_tilde_expansion(tmp_path: Path) -> None:
    file = tmp_path / "config.toml"
    file.write_text(
        '[neo4j]\nbolt_port = 17687\ndata_dir = "~/kbdata"\n'
        '[scopes]\nproject-16 = "~/projects/project-16"\nabs = "/srv/x"\n'
        '[calendar]\nextra_days_off = ["2026-12-24"]\n'
        "[sources.odoo]\nenabled = true\n"
    )
    cfg = load(path=file, home=tmp_path)
    assert cfg.neo4j.bolt_port == 17687
    assert cfg.neo4j.http_port == 7474
    assert cfg.neo4j.data_dir == tmp_path / "kbdata"
    assert cfg.scopes == {
        "project-16": tmp_path / "projects" / "project-16",
        "abs": Path("/srv/x"),
    }
    assert cfg.calendar.extra_days_off == ("2026-12-24",)
    assert cfg.sources.odoo.enabled is True


def test_odoo_scope_must_be_global_or_a_configured_scope(tmp_path: Path) -> None:
    file = tmp_path / "config.toml"
    odoo = "[sources.odoo]\nenabled = true\n"
    file.write_text('[scopes]\nproj = "/srv/proj"\n' + odoo + 'scope = "proj"\n')
    assert load(path=file, home=tmp_path).sources.odoo.scope == "proj"
    file.write_text(odoo + 'scope = "global"\n')
    assert load(path=file, home=tmp_path).sources.odoo.scope == "global"
    file.write_text(odoo + 'scope = "personal"\n')
    with pytest.raises(ConfigError, match="none configured"):
        load(path=file, home=tmp_path)
    # a stale scope in a disabled section must not break every other command
    file.write_text('[sources.odoo]\nenabled = false\nscope = "gone-project"\n')
    assert load(path=file, home=tmp_path).sources.odoo.scope == "gone-project"


def test_kb_config_env_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    file = tmp_path / "elsewhere.toml"
    file.write_text("[neo4j]\nbolt_port = 1\n")
    monkeypatch.setenv("KB_CONFIG", str(file))
    assert load(home=tmp_path).neo4j.bolt_port == 1


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("[neo4j]\nbolt = 1\n", "unknown key: neo4j.bolt"),
        ("[nowhere]\nx = 1\n", "unknown key: nowhere"),
        ("[escalation.hardd]\nwhisper = 1\n", "unknown key: escalation.hardd"),
        ('[sources.odoo]\npassword = "x"\n', "unknown key: sources.odoo.password"),
        ('[index]\nbudget_lines = "200"\n', "index.budget_lines: expected int, got str"),
        ('[neo4j]\nbolt_port = "7687"\n', "neo4j.bolt_port: expected int, got str"),
        ("[neo4j]\nbolt_port = true\n", "neo4j.bolt_port: expected int, got bool"),
        ('[calendar]\nextra_days_off = "2026-01-01"\n', "calendar.extra_days_off: expected array"),
        ("[scopes]\nfoo = 3\n", "scopes.foo: expected path string, got int"),
    ],
)
def test_invalid_config_raises_naming_the_key(tmp_path: Path, content: str, message: str) -> None:
    file = tmp_path / "config.toml"
    file.write_text(content)
    with pytest.raises(ConfigError, match=message):
        load(path=file, home=tmp_path)


def test_dump_toml_round_trips(tmp_path: Path) -> None:
    file = tmp_path / "config.toml"
    file.write_text('[scopes]\nkb = "~/projects/kb"\n"odoo.16" = "/srv/odoo-16"\n')
    cfg = load(path=file, home=tmp_path)
    text = dump_toml(cfg)
    data = tomllib.loads(text)
    assert data["scopes"]["kb"] == str(tmp_path / "projects" / "kb")
    assert data["scopes"]["odoo.16"] == "/srv/odoo-16"
    assert '"odoo.16" = ' in text
    assert data["neo4j"]["data_dir"] == str(tmp_path / ".local" / "share" / "kb")
    assert data["escalation"]["window"]["skips_to_backlog"] == 2
    assert "[scopes]" in text
    assert "[sources.odoo]" in text


def test_config_init_and_show(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    file = tmp_path / "cfg" / "config.toml"
    monkeypatch.setenv("KB_CONFIG", str(file))
    runner = CliRunner()
    first = runner.invoke(main, ["config", "init"])
    assert first.exit_code == 0, first.output
    assert first.output.strip() == f"wrote {file}"
    assert file.read_text() == render_default()

    file.write_text("[neo4j]\nbolt_port = 4242\n")
    second = runner.invoke(main, ["config", "init"])
    assert second.exit_code == 0, second.output
    assert "not overwritten" in second.output
    assert file.read_text() == "[neo4j]\nbolt_port = 4242\n"

    shown = runner.invoke(main, ["config", "show"])
    assert shown.exit_code == 0, shown.output
    assert tomllib.loads(shown.output)["neo4j"]["bolt_port"] == 4242

    as_json = runner.invoke(main, ["--json", "config", "show"])
    assert as_json.exit_code == 0, as_json.output
    assert '"bolt_port": 4242' in as_json.output
