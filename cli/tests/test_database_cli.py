"""CLI contracts for direct database backup and restore."""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentworks import output
from agentworks.cli import app
from agentworks.cli._typer_output import TyperHandler
from agentworks.db import Database, backup_directory, create_manual_backup
from agentworks.db.migrations import LATEST_VERSION, MIGRATIONS, MigrationContext


@contextmanager
def _cli_output() -> Iterator[None]:
    previous = output.get_handler()
    output.set_handler(TyperHandler())
    try:
        yield
    finally:
        output.set_handler(previous)


def _set_value(path: Path, value: str) -> None:
    connection = sqlite3.connect(path)
    connection.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('cli-test', ?)", (value,))
    connection.commit()
    connection.close()


def _value(path: Path) -> str | None:
    connection = sqlite3.connect(path)
    row = connection.execute("SELECT value FROM settings WHERE key = 'cli-test'").fetchone()
    connection.close()
    return None if row is None else str(row[0])


def _build_stale_schema(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute(
        "CREATE TABLE schema_version ("
        "version INTEGER NOT NULL, "
        "applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')))"
    )
    context = MigrationContext()
    for version in range(1, LATEST_VERSION):
        step = MIGRATIONS[version]
        if callable(step):
            step(connection, context)
        else:
            for statement in step.split(";"):
                if statement.strip():
                    connection.execute(statement)
        connection.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
        connection.commit()
    connection.close()


def test_database_backup_stdout_is_only_the_completed_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import agentworks.db as db

    live = tmp_path / "agentworks.db"
    Database(live).close()
    _set_value(live, "preserved")
    monkeypatch.setattr(db, "DB_PATH", live)
    monkeypatch.setattr(db, "Database", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("migrated")))

    with _cli_output():
        result = CliRunner().invoke(app, ["database", "backup"])

    assert result.exit_code == 0, result.output
    path = Path(result.stdout.strip())
    assert result.stdout == f"{path}\n"
    assert result.stderr == "Creating database backup...\n"
    assert path.parent == backup_directory(live)
    assert _value(path) == "preserved"


def test_database_restore_yes_uses_stderr_and_creates_no_implicit_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agentworks.db as db

    live = tmp_path / "agentworks.db"
    Database(live).close()
    _set_value(live, "selected")
    selected = create_manual_backup(live)
    _set_value(live, "replace-me")
    before = {path.name for path in backup_directory(live).glob("*.db")}
    monkeypatch.setattr(db, "DB_PATH", live)
    monkeypatch.setattr(db, "Database", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("migrated")))

    with _cli_output():
        result = CliRunner().invoke(app, ["database", "restore", str(selected), "-y"])

    assert result.exit_code == 0, result.output
    assert result.stdout == ""
    assert f"Backup: {selected}" in result.stderr
    assert f"Live database: {live}" in result.stderr
    assert result.stderr.endswith("Database restore complete.\n")
    assert _value(live) == "selected"
    assert {path.name for path in backup_directory(live).glob("*.db")} == before


def test_database_restore_decline_prompts_on_stderr_and_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agentworks.db as db

    selected = tmp_path / "selected.db"
    live = tmp_path / "agentworks.db"
    Database(selected).close()
    Database(live).close()
    _set_value(selected, "selected")
    _set_value(live, "unchanged")
    monkeypatch.setattr(db, "DB_PATH", live)
    monkeypatch.setattr(output, "is_interactive", lambda: True)

    with _cli_output():
        result = CliRunner().invoke(app, ["database", "restore", str(selected)], input="n\n")

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "Replace the live state database with this backup? [y/N]" in result.stderr
    assert _value(live) == "unchanged"
    assert _value(selected) == "selected"


def test_database_restore_non_interactive_without_yes_refuses_cleanly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import agentworks.db as db
    from agentworks import cli

    selected = tmp_path / "selected.db"
    live = tmp_path / "agentworks.db"
    Database(selected).close()
    monkeypatch.setattr(db, "DB_PATH", live)
    monkeypatch.setattr(sys, "argv", ["agw", "--non-interactive", "database", "restore", str(selected)])

    with pytest.raises(SystemExit) as exit_info:
        cli.main()

    assert exit_info.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "database restore requires confirmation" in captured.err
    assert "Pass --yes" in captured.err
    assert not live.exists()


@pytest.mark.parametrize("machine_output", [False, True])
def test_interactive_migration_notice_and_prompt_keep_stdout_machine_pure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, machine_output: bool
) -> None:
    import json
    from types import SimpleNamespace

    import agentworks.config as config_module
    import agentworks.db as db

    live = tmp_path / "agentworks.db"
    _build_stale_schema(live)
    monkeypatch.setattr(db, "DB_PATH", live)
    monkeypatch.setattr(config_module, "CONFIG_PATH", tmp_path / "absent.toml")
    monkeypatch.setattr(output, "is_interactive", lambda: True)
    monkeypatch.setattr("agentworks.cli._helpers.sys", SimpleNamespace(stderr=SimpleNamespace(isatty=lambda: True)))
    args = ["vm", "list"]
    if machine_output:
        args.extend(["--output", "json"])
    else:
        args.append("--names-only")

    with _cli_output():
        result = CliRunner().invoke(app, args, input="\n")

    assert result.exit_code == 0, result.output
    if machine_output:
        assert json.loads(result.stdout)["data"] == {"vms": []}
    else:
        assert result.stdout == ""
    assert f"version {LATEST_VERSION - 1} to {LATEST_VERSION}" in result.stderr
    assert "Back up the state database before migrating? [Y/n]" in result.stderr
    assert "Pre-migration database backup completed:" in result.stderr
