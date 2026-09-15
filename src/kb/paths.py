"""Filesystem layout derived from the config, and the nix-provided Neo4j package."""

import os
from dataclasses import dataclass
from pathlib import Path

from kb.config import Config, config_path

UNIT_NAME = "neo4j-kb.service"
LOG_CONFIGS = ("server-logs.xml", "user-logs.xml")


def neo4j_package() -> Path:
    """The neo4j store path from ``KB_NEO4J_PACKAGE``; RuntimeError when it is unset."""
    value = os.environ.get("KB_NEO4J_PACKAGE")
    if not value:
        raise RuntimeError(
            "KB_NEO4J_PACKAGE is not set; run kb from the nix package or inside nix develop"
        )
    return Path(value)


def neo4j_version(pkg: Path) -> str:
    """``<hash>-neo4j-2026.07.0`` -> ``2026.07.0``."""
    return pkg.name.split("-", 1)[-1].removeprefix("neo4j-")


def auth_path(home: Path) -> Path:
    """``$KB_NEO4J_AUTH`` when set, else ``<home>/.config/kb/neo4j-auth``."""
    override = os.environ.get("KB_NEO4J_AUTH")
    return Path(override) if override else home / ".config" / "kb" / "neo4j-auth"


@dataclass(frozen=True)
class Paths:
    """Every file and directory kb reads or writes, derived from the config and home."""

    home: Path
    root: Path
    neo4j_root: Path
    pkg_root: Path
    config_file: Path
    auth_file: Path
    unit_file: Path
    conf_dir: Path
    conf_file: Path
    data_dir: Path
    logs_dir: Path
    run_dir: Path
    import_dir: Path
    plugins_dir: Path
    tx_logs_dir: Path
    backup_dir: Path

    @classmethod
    def from_config(cls, cfg: Config, home: Path) -> Paths:
        """Layout under ``cfg.neo4j.data_dir``, ``cfg.backup.dir`` and ``home``."""
        root = cfg.neo4j.data_dir / "neo4j"
        return cls(
            home=home,
            root=cfg.neo4j.data_dir,
            neo4j_root=root,
            # indirect nix GC root for the neo4j package the unit runs
            pkg_root=cfg.neo4j.data_dir / "neo4j-pkg",
            config_file=config_path(home),
            auth_file=auth_path(home),
            unit_file=home / ".config" / "systemd" / "user" / UNIT_NAME,
            conf_dir=root / "conf",
            conf_file=root / "conf" / "neo4j.conf",
            data_dir=root / "data",
            logs_dir=root / "logs",
            run_dir=root / "run",
            import_dir=root / "import",
            plugins_dir=root / "plugins",
            tx_logs_dir=root / "data" / "transactions",
            backup_dir=cfg.backup.dir,
        )

    @property
    def neo4j_dirs(self) -> tuple[Path, ...]:
        """Directories kb-setup creates under ``<data_dir>/neo4j``: conf and the writable ones."""
        return (
            self.conf_dir,
            self.data_dir,
            self.logs_dir,
            self.run_dir,
            self.import_dir,
            self.plugins_dir,
        )
