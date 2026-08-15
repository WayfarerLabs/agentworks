from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import cast

import pytest

from agentworks.config import Config
from agentworks.db import Database
from agentworks.errors import BusyStateError, StateError
from agentworks.guide import GuideMode, GuideTraversalError
from agentworks.guide.service import render_guide
from agentworks.resources import Registry
from agentworks.vms.template import VMTemplate
from tests.conftest import held_exclusive_lock


class _LiveRegistry:
    is_finalized = True

    def iter_kind_items(self, kind: str):  # noqa: ANN201
        if kind == "vm-template":
            return iter((("demo", VMTemplate(name="demo")),))
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
    with pytest.raises(GuideTraversalError):
        render_guide(
            ("vm-template",),
            GuideMode.AGENT,
            load_config_fn=lambda: cast("Config", object()),
            load_registry_fn=lambda config: cast("Registry", _LiveRegistry()),
        )


@pytest.mark.parametrize(
    "malformed",
    (
        "not-json",
        sqlite3.Binary(b"\xff"),
        "[" * 10_000 + "0" + "]" * 10_000,
    ),
)
def test_render_guide_degrades_malformed_persisted_vm_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    malformed: object,
) -> None:
    import agentworks.db as db_package

    path = tmp_path / "state.db"
    writable = Database(path)
    writable.insert_vm("broken", site="local", hostname="broken", template="demo")
    writable.close()
    connection = sqlite3.connect(path)
    connection.execute("UPDATE vms SET extra_packages = ? WHERE name = ?", (malformed, "broken"))
    connection.commit()
    connection.close()
    monkeypatch.setattr(db_package, "DB_PATH", path)

    response = render_guide(
        ("concept-management",),
        GuideMode.AGENT,
        load_config_fn=lambda: cast("Config", object()),
        load_registry_fn=lambda config: cast("Registry", _LiveRegistry()),
    )

    assert response.exit_code == 0
    assert "concept-management/inventory" in response.markdown


def test_render_guide_surfaces_busy_not_malformed_under_a_held_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guide service calls Database(read_only=True) directly (only a
    DB_PATH.exists() check precedes it, not Database.check_schema), so a
    busy database reaches render_guide's exception handling exactly the
    way malformed content does: this is guide's ordinary path, not a
    narrow race window.

    Pinned structurally: render_guide's GuideResponse exposes only
    rendered markdown and an exit code, neither of which distinguishes
    busy from malformed without reading prose, so this spies on
    _framed_error, the function render_guide calls immediately before
    turning the caught exception into markdown, to capture which
    exception object it actually received."""
    import agentworks.db as db_package
    import agentworks.guide.service as guide_service

    path = tmp_path / "state.db"
    Database(path).close()
    monkeypatch.setattr(db_package, "DB_PATH", path)

    captured: list[Exception] = []
    real_framed_error = guide_service._framed_error

    def _spy(error: Exception) -> str:
        captured.append(error)
        return real_framed_error(error)

    monkeypatch.setattr(guide_service, "_framed_error", _spy)

    with held_exclusive_lock(path):
        response = render_guide(
            ("vm-template",),
            GuideMode.AGENT,
            load_config_fn=lambda: cast("Config", object()),
            load_registry_fn=lambda config: cast("Registry", _LiveRegistry()),
        )

    assert response.exit_code == 0
    assert len(captured) == 1
    assert isinstance(captured[0], BusyStateError)


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


def test_read_only_database_raises_busy_not_malformed_under_a_held_lock(tmp_path: Path) -> None:
    """Database(read_only=True)'s own except sqlite3.DatabaseError block
    used to route every connect failure, busy included, through the
    malformed message and repair hint, entirely independent of
    inspect_schema's classification. This is not just doctor's narrow
    TOCTOU window: the guide service (see
    test_render_guide_surfaces_busy_not_malformed_under_a_held_lock below)
    calls this constructor directly with no preceding schema check at
    all, so it is guide's ordinary path.

    Pinned structurally, not by wording: BusyStateError is a distinct
    StateError subtype the old code never raised here (it always raised
    plain StateError, the shape the malformed case above still uses)."""
    path = tmp_path / "state.db"
    Database(path).close()

    with held_exclusive_lock(path), pytest.raises(BusyStateError) as raised:
        Database(path, read_only=True)

    assert raised.value.hint is not None
