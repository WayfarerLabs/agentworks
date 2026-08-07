from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import cast

import pytest

from agentworks.config import Config
from agentworks.db import Database
from agentworks.errors import StateError
from agentworks.guide import GuideMode
from agentworks.guide.service import render_guide
from agentworks.resources import Registry


class _LiveRegistry:
    is_finalized = True

    def iter_kind_items(self, kind: str):  # noqa: ANN201
        if kind == "vm-template":
            return iter((("demo", object()),))
        return iter(())


def test_read_only_database_handles_uri_significant_path_without_migrating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "state?#.db"
    writable = Database(path)
    writable.close()

    monkeypatch.setattr(Database, "_migrate", lambda self: pytest.fail("migration invoked"))
    readonly = Database(path, read_only=True)
    try:
        assert readonly.list_vms() == []
    finally:
        readonly.close()


def test_read_only_database_rejects_an_actual_write(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    writable = Database(path)
    writable.close()
    readonly = Database(path, read_only=True)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            readonly.insert_vm("forbidden", site="local", hostname="forbidden")
    finally:
        readonly.close()


def test_render_guide_constructs_database_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agentworks.db as db_package

    path = tmp_path / "state.db"
    Database(path).close()
    constructed: list[bool] = []

    class DatabaseSpy(Database):
        def __init__(self, path: Path | None = None, *, read_only: bool = False) -> None:
            constructed.append(read_only)
            super().__init__(path, read_only=read_only)

    monkeypatch.setattr(db_package, "DB_PATH", path)
    monkeypatch.setattr(db_package, "Database", DatabaseSpy)
    response = render_guide(
        ("vm-template",),
        GuideMode.AGENT,
        load_config_fn=lambda: cast("Config", object()),
        load_registry_fn=lambda config: cast("Registry", _LiveRegistry()),
    )

    assert response.exit_code == 0
    assert constructed == [True]


def test_render_guide_frames_accidental_read_only_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agentworks.db as db_package
    import agentworks.guide.service as guide_service

    path = tmp_path / "state.db"
    Database(path).close()
    monkeypatch.setattr(db_package, "DB_PATH", path)

    def accidental_write(topic: object, registry: object, db: Database) -> object:
        db.insert_vm("forbidden", site="local", hostname="forbidden")
        raise AssertionError("read-only write unexpectedly succeeded")

    monkeypatch.setattr(guide_service, "build_guide_view", accidental_write)
    response = render_guide(
        ("vm-template",),
        GuideMode.AGENT,
        load_config_fn=lambda: cast("Config", object()),
        load_registry_fn=lambda config: cast("Registry", _LiveRegistry()),
    )

    assert response.exit_code == 1
    assert "state database rejected a guide projection" in response.markdown
    assert "readonly database" not in response.markdown


def test_read_only_database_rejects_stale_schema(tmp_path: Path) -> None:
    path = tmp_path / "stale.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    connection.execute("INSERT INTO schema_version VALUES (0)")
    connection.commit()
    connection.close()

    with pytest.raises(StateError, match="outdated"):
        Database(path, read_only=True)


def test_read_only_database_frames_malformed_schema(tmp_path: Path) -> None:
    path = tmp_path / "malformed.db"
    path.write_bytes(b"not a sqlite database")
    with pytest.raises(StateError, match="malformed"):
        Database(path, read_only=True)
