"""Tests for ``agentworks.resources.inspect.list_resources`` -- the
service layer behind ``agw resource list`` (Phase 2c).

The cross-kind list stops at framework-uniform fields (kind, name,
origin, usage count, description). Kind-specific detail lives in the
per-kind commands. Filters (``--kind``, ``--origin``) narrow the rows;
the summary counts reflect the post-filter view.
"""

from __future__ import annotations

import sqlite3
from textwrap import dedent
from typing import TYPE_CHECKING

import pytest

from agentworks.bootstrap import build_registry
from agentworks.config import load_config
from agentworks.errors import NotFoundError
from agentworks.resources.inspect import list_resources
from agentworks.resources.render import format_origin_line
from tests.conftest import ManifestDoc, write_manifests

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

# The operator vm-template these tests seed, so vm-template/default is
# operator-declared and its tailscale requirement auto-declares a secret.
_VM_DEFAULT = ManifestDoc("vm-template", "default", {"apt": ["zsh"]})
# The env-var secret backend, the settings block most of these tests carry.
_ENV_VAR_BACKEND = """
[secret_config]
sources = ["env-var"]
"""


def _write_base(
    config_path: Path,
    *,
    settings: str = "",
    manifests: Sequence[ManifestDoc | str] = (),
) -> None:
    """Write a settings-only config.toml plus its resources/ manifests.
    ``settings`` carries settings-only TOML ([secret_config]); resources
    go in ``manifests``."""
    pub = config_path.parent / "id.pub"
    priv = config_path.parent / "id"
    pub.write_text("ssh-ed25519 AAAA...")
    priv.write_text("-----BEGIN OPENSSH PRIVATE KEY-----")
    config_path.write_text(
        dedent(f"""\
        [operator]
        ssh_public_key = "{pub.as_posix()}"
        ssh_private_key = "{priv.as_posix()}"
        """)
        + dedent(settings),
    )
    if manifests:
        write_manifests(config_path.parent, *manifests)


def _load(cfg_file: Path):
    cfg = load_config(cfg_file, warn_issues=False)
    return build_registry(cfg)


# -- Cross-kind enumeration -------------------------------------------------


def test_lists_every_kind_present_when_no_kind_filter(tmp_path: Path) -> None:
    """Without ``kinds=``, every kind with at least one published
    resource appears; rows are grouped by kind and sorted by name.
    """
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        settings=_ENV_VAR_BACKEND,
        manifests=[_VM_DEFAULT, ManifestDoc("secret", "my-key", description="k")],
    )
    registry = _load(cfg_file)
    listing = list_resources(registry)

    kinds_seen = {row.kind for row in listing.rows}
    # vm-template (operator-declared default), secret (operator + auto),
    # secret-backend (active env-var), agent-template (default built-in),
    # apt-package (disabled plugin manifest), git-credential-provider (built-in
    # capability row), etc. We assert presence of the key cross-kind
    # expectations rather than the full set, since publishers may add
    # more.
    assert "vm-template" in kinds_seen
    assert "secret" in kinds_seen
    assert "secret-backend" in kinds_seen
    assert "agent-template" in kinds_seen


def test_kind_filter_narrows_rows_and_summary(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        settings=_ENV_VAR_BACKEND,
        manifests=[_VM_DEFAULT, ManifestDoc("secret", "my-key", description="k")],
    )
    registry = _load(cfg_file)
    listing = list_resources(registry, kinds=("secret",))

    assert {r.kind for r in listing.rows} == {"secret"}
    # Summary counts are post-filter -- they reflect only the visible
    # rows so the header doesn't mislead the operator.
    assert listing.operator_count + listing.auto_count + listing.code_count == len(listing.rows)


def test_kind_filter_accepts_multiple_kinds(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.toml"
    _write_base(cfg_file, settings=_ENV_VAR_BACKEND, manifests=[_VM_DEFAULT])
    registry = _load(cfg_file)
    listing = list_resources(registry, kinds=("vm-template", "secret-backend"))

    kinds_seen = {row.kind for row in listing.rows}
    assert kinds_seen == {"vm-template", "secret-backend"}


def test_unknown_kind_filter_raises_unknown_kind_error(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.toml"
    _write_base(cfg_file)
    registry = _load(cfg_file)
    with pytest.raises(NotFoundError, match="unknown kind 'does_not_exist'"):
        list_resources(registry, kinds=("does_not_exist",))


# -- Origin filter ----------------------------------------------------------


def test_origin_filter_operator_only_shows_operator_declared(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        settings=_ENV_VAR_BACKEND,
        manifests=[_VM_DEFAULT, ManifestDoc("secret", "my-key", description="k")],
    )
    registry = _load(cfg_file)
    listing = list_resources(registry, origin_filter="operator")

    assert all(row.origin is not None and row.origin.variant == "operator-declared" for row in listing.rows)
    assert listing.operator_count == len(listing.rows)
    assert listing.auto_count == 0
    assert listing.code_count == 0


def test_origin_filter_auto_only_shows_auto_declared(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.toml"
    _write_base(cfg_file, settings=_ENV_VAR_BACKEND, manifests=[_VM_DEFAULT])
    registry = _load(cfg_file)
    listing = list_resources(registry, origin_filter="auto")

    assert all(row.origin is not None and row.origin.variant == "auto-declared" for row in listing.rows)
    assert listing.auto_count == len(listing.rows)


def test_origin_filter_code_only_shows_built_in(tmp_path: Path) -> None:
    """Code-declared resources include the default ``agent-template``
    (and other framework defaults). The filter narrows to just those.
    """
    cfg_file = tmp_path / "config.toml"
    _write_base(cfg_file)
    registry = _load(cfg_file)
    listing = list_resources(registry, origin_filter="builtin")

    assert all(row.origin is not None and row.origin.variant == "built-in" for row in listing.rows)
    assert listing.code_count == len(listing.rows)


# -- Origin rendering --------------------------------------------------------


def test_format_origin_line_renders_each_variant(tmp_path: Path) -> None:
    """``format_origin_line`` is the framework-shared origin renderer
    used by the resource list and secret describe views; the list view
    emits it as a single cell, so we assert variant strings are present
    and no unknown variants slip in.
    """
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        settings=_ENV_VAR_BACKEND,
        manifests=[ManifestDoc("secret", "my-key", description="k")],
    )
    registry = _load(cfg_file)
    listing = list_resources(registry)

    rendered = [format_origin_line(row.origin) for row in listing.rows]
    assert any(s.startswith("operator-declared") for s in rendered)
    # auto- and built-in lines may or may not have a source --
    # both shapes are valid; just assert no unknown variants slip in.
    for s in rendered:
        assert s.startswith(("operator-declared", "auto-declared", "built-in"))


# -- Description coverage ----------------------------------------------------


def test_description_populated_for_operator_and_auto_resources(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        settings=_ENV_VAR_BACKEND,
        manifests=[_VM_DEFAULT, ManifestDoc("secret", "my-key", description="operator note")],
    )
    registry = _load(cfg_file)
    listing = list_resources(registry)

    by_kn = {(row.kind, row.name): row for row in listing.rows}
    op = by_kn[("secret", "my-key")]
    assert op.description == "operator note"

    # Auto-declared tailscale-auth-key carries the framework-synthesized
    # polish text. Empty-usage auto-declared rows would carry the
    # "(auto) auto-declared default <kind>" fallback.
    ts = by_kn.get(("secret", "tailscale-auth-key"))
    assert ts is not None
    assert ts.description.startswith("(auto) ")


# -- CLI surface -----------------------------------------------------------


def test_missing_ssh_keys_do_not_block_resource_list(tmp_path: Path, monkeypatch) -> None:
    """A config whose only defect is a nonexistent operator SSH key path
    (the sample config's placeholder, before ``agw config init`` writes a
    real one) must not stop `resource list` from listing resources: it
    needs no operator identity."""
    from typer.testing import CliRunner

    from agentworks.cli import app

    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        dedent(f"""\
        [operator]
        ssh_public_key = "{(tmp_path / "id.pub").as_posix()}"
        ssh_private_key = "{(tmp_path / "id").as_posix()}"
        """)
    )
    assert not (tmp_path / "id.pub").exists()
    assert not (tmp_path / "id").exists()
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", cfg_file)

    result = CliRunner().invoke(app, ["resource", "list"])

    assert result.exit_code == 0, result.output
    assert "KIND" in result.output and "NAME" in result.output


def test_cli_names_only_emits_kind_slash_name_per_line(tmp_path: Path, monkeypatch) -> None:
    """``agw resource list --names-only`` is the source for shell
    completion; the line format is ``<kind>/<name>``.
    """
    from typer.testing import CliRunner

    from agentworks import db
    from agentworks.cli import app
    from agentworks.cli.commands import resource

    cfg_file = tmp_path / "config.toml"
    _write_base(cfg_file, manifests=[_VM_DEFAULT])
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", cfg_file)
    monkeypatch.setattr(resource, "get_db", lambda: (_ for _ in ()).throw(AssertionError("no get_db")))
    database_path = tmp_path / "state.db"
    monkeypatch.setattr(db, "DB_PATH", database_path)

    result = CliRunner().invoke(app, ["resource", "list", "--names-only"])
    assert result.exit_code == 0, result.stdout
    lines = [line for line in result.stdout.splitlines() if line]
    assert lines, "expected at least one resource row"
    for line in lines:
        assert "/" in line
    # Spot-check known framework defaults appear (vm-template/default
    # operator-declared; tailscale-auth-key auto-declared).
    assert "vm-template/default" in lines
    assert "secret/tailscale-auth-key" in lines
    assert not database_path.exists()


def test_registry_list_order_is_stable(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.toml"
    _write_base(cfg_file, manifests=[_VM_DEFAULT])
    registry = build_registry(load_config(cfg_file))
    first = list_resources(registry)
    second = list_resources(registry)
    assert tuple((row.kind, row.name) for row in first.rows) == tuple((row.kind, row.name) for row in second.rows)


@pytest.mark.parametrize(
    "database_state",
    [
        "absent",
        "stale",
        "newer",
        "malformed",
        "busy",
        "unreadable",
        "unsupported-overlay",
        "stranded-overlay",
    ],
)
def test_names_only_ignores_unusable_database_states(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    database_state: str,
) -> None:
    from typer.testing import CliRunner

    from agentworks import db
    from agentworks.cli import app
    from agentworks.db import LATEST_VERSION, Database, VersionedPayload

    cfg_file = tmp_path / "config.toml"
    _write_base(cfg_file, manifests=[_VM_DEFAULT])
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", cfg_file)
    database_path = tmp_path / "state.db"
    lock: sqlite3.Connection | None = None

    if database_state in {"stale", "newer", "busy"}:
        Database(database_path).close()
    if database_state in {"stale", "newer"}:
        connection = sqlite3.connect(database_path)
        version = LATEST_VERSION - 1 if database_state == "stale" else LATEST_VERSION + 1
        connection.execute("UPDATE schema_version SET version = ?", (version,))
        connection.commit()
        connection.close()
    elif database_state == "malformed":
        database_path.write_bytes(b"not sqlite")
    elif database_state == "busy":
        lock = sqlite3.connect(database_path)
        lock.execute("BEGIN EXCLUSIVE")
    elif database_state == "unreadable":
        database_path.mkdir()
    elif database_state in {"unsupported-overlay", "stranded-overlay"}:
        database = Database(database_path)
        database.insert_vm("box", "lima-local", "box")
        database.insert_agent("dev", "box", "dev")
        payload = (
            VersionedPayload(2, {"future_field": True})
            if database_state == "unsupported-overlay"
            else VersionedPayload(1, {"user_install_commands": ["gone"]})
        )
        database.instance_state.put_desired_overlay(
            "agent",
            "dev",
            payload,
        )
        database.close()

    monkeypatch.setattr(db, "DB_PATH", database_path)
    try:
        result = CliRunner().invoke(app, ["resource", "list", "--names-only"])
    finally:
        if lock is not None:
            lock.rollback()
            lock.close()

    assert result.exit_code == 0, result.output
    assert "vm-template/default\n" in result.stdout


def test_cli_kind_csv_filter(tmp_path: Path, monkeypatch) -> None:
    from typer.testing import CliRunner

    from agentworks.cli import app

    cfg_file = tmp_path / "config.toml"
    _write_base(cfg_file, manifests=[_VM_DEFAULT])
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", cfg_file)

    result = CliRunner().invoke(
        app,
        ["resource", "list", "--kind", "vm-template,secret", "--names-only"],
    )
    assert result.exit_code == 0, result.stdout
    lines = [line for line in result.stdout.splitlines() if line]
    seen_kinds = {line.split("/", 1)[0] for line in lines}
    assert seen_kinds == {"vm-template", "secret"}


def test_cli_kind_csv_filter_tolerates_whitespace(tmp_path: Path, monkeypatch) -> None:
    """``--kind vm-template, secret`` (with a space) parses the same as
    ``--kind vm-template,secret``. Commas can't appear in kind
    identifiers, so a forgiving parse is safe.
    """
    from typer.testing import CliRunner

    from agentworks.cli import app

    cfg_file = tmp_path / "config.toml"
    _write_base(cfg_file, manifests=[_VM_DEFAULT])
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", cfg_file)

    result = CliRunner().invoke(
        app,
        ["resource", "list", "--kind", "vm-template, secret", "--names-only"],
    )
    assert result.exit_code == 0, result.stdout
    lines = [line for line in result.stdout.splitlines() if line]
    seen_kinds = {line.split("/", 1)[0] for line in lines}
    assert seen_kinds == {"vm-template", "secret"}


def test_cli_old_kind_filter_is_an_unknown_kind_error(tmp_path: Path, monkeypatch) -> None:
    from typer.testing import CliRunner

    from agentworks.cli import app

    cfg_file = tmp_path / "config.toml"
    _write_base(cfg_file)
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", cfg_file)

    result = CliRunner().invoke(
        app,
        ["resource", "list", "--kind", "harness", "--names-only"],
    )
    assert result.exit_code != 0
    assert "unknown kind 'harness'" in str(result.exception)


def test_cli_empty_kind_csv_is_rejected(tmp_path: Path, monkeypatch) -> None:
    """``--kind ""`` (or all-whitespace, or just commas) parses to zero
    kinds; rejecting is more honest than silently treating it as
    ``--kind <all>``.
    """
    from typer.testing import CliRunner

    from agentworks.cli import app
    from agentworks.errors import ValidationError

    cfg_file = tmp_path / "config.toml"
    _write_base(cfg_file)
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", cfg_file)

    result = CliRunner().invoke(app, ["resource", "list", "--kind", ""])
    assert result.exit_code != 0
    assert isinstance(result.exception, ValidationError)


def test_cli_invalid_origin_filter_is_rejected(tmp_path: Path, monkeypatch) -> None:
    from typer.testing import CliRunner

    from agentworks.cli import app

    cfg_file = tmp_path / "config.toml"
    _write_base(cfg_file)
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", cfg_file)

    from agentworks.errors import ValidationError

    result = CliRunner().invoke(app, ["resource", "list", "--origin", "bogus"])
    assert result.exit_code != 0
    # The typed ValidationError surfaces with the allowed list so the
    # operator can self-correct without reading the help text.
    assert isinstance(result.exception, ValidationError)
    assert "operator" in str(result.exception)
