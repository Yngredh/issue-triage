"""Descoberta das migrações (D2), sem banco."""

import re
from pathlib import Path

import pytest

from issue_triage.migrate import MIGRATIONS_DIR, discover_migrations


def test_discovers_triage_schema_migration() -> None:
    migrations = discover_migrations()
    assert migrations[0] == ("0001", MIGRATIONS_DIR / "0001_triage_schema.sql")


def test_migrations_create_only_triage_schema() -> None:
    for _, path in discover_migrations():
        sql = path.read_text(encoding="utf-8").lower()
        assert re.findall(r"create schema if not exists (\w+)", sql) in ([], ["triage"])
        assert re.findall(r"create (?:table|view|unique index|index) [^(]*?(\w+)\.", sql) == [
            "triage"
        ] * len(re.findall(r"create (?:table|view|unique index|index) ", sql))


def test_orders_by_version_and_ignores_other_files(tmp_path: Path) -> None:
    (tmp_path / "0002_b.sql").write_text("", encoding="utf-8")
    (tmp_path / "0001_a.sql").write_text("", encoding="utf-8")
    (tmp_path / "notes.md").write_text("", encoding="utf-8")
    assert [version for version, _ in discover_migrations(tmp_path)] == ["0001", "0002"]


def test_rejects_duplicate_versions(tmp_path: Path) -> None:
    (tmp_path / "0001_a.sql").write_text("", encoding="utf-8")
    (tmp_path / "0001_b.sql").write_text("", encoding="utf-8")
    with pytest.raises(ValueError):
        discover_migrations(tmp_path)
