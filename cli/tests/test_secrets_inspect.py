"""Tests for ``agentworks.secrets.inspect``: the table builder behind
``agw secret list``."""

from __future__ import annotations

from textwrap import dedent
from typing import TYPE_CHECKING

from agentworks.bootstrap import build_registry
from agentworks.config import load_config
from agentworks.secrets.inspect import (
    _BACKEND_CELL_WIDTH,
    _NAME_CELL_WIDTH,
    SecretCell,
    SecretRow,
    SecretTable,
    build_secret_table,
    render_secret_table,
)
from tests.conftest import ManifestDoc, write_manifests

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from tests.conftest import CapturedOutput


def _build_table(cfg_file: Path):
    cfg = load_config(cfg_file, warn_issues=False)
    registry = build_registry(cfg)
    return build_secret_table(cfg, registry)


def _write_base(
    config_path: Path,
    *,
    settings: str = "",
    admin_env: dict[str, object] | None = None,
    manifests: Sequence[ManifestDoc | str] = (),
) -> None:
    """Write a settings-only config.toml plus resources/ manifests.

    The base always declares the ``default`` vm-template (whose
    ``tailscale_auth_key`` requirement is what auto-declares the
    ``tailscale-auth-key`` secret these tests assert on). ``admin_env``
    seeds the ``default`` admin-template's env block (the operator's
    secret-referencing env), ``manifests`` carries the operator secrets,
    and ``settings`` any settings-only TOML ([secret_config], [plugins]).
    """
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
    docs: list[ManifestDoc | str] = [ManifestDoc("vm-template", "default", {"apt": ["zsh"]})]
    if admin_env is not None:
        docs.append(ManifestDoc("admin-template", "default", {"env": admin_env}))
    docs.extend(manifests)
    write_manifests(config_path.parent, *docs)


def test_no_operator_secrets_still_shows_auto_declared(tmp_path: Path) -> None:
    """No operator-declared secrets, but Phase 1c's VMTemplate
    ``tailscale_auth_key`` requirement always auto-declares the
    ``tailscale-auth-key`` secret. The table iterates the Registry
    (per Phase 1e) so that auto-declared row is surfaced.
    """
    cfg_file = tmp_path / "config.toml"
    _write_base(cfg_file)

    table = _build_table(cfg_file)
    assert table.backends == ("env-var", "prompt")
    names = [r.name for r in table.rows]
    assert "tailscale-auth-key" in names
    # The auto-declared row carries a synthesized description so the
    # list view's Description column is populated without an operator
    # having to write one in ``[secrets.<name>]``. The text is derived
    # from the first requirement's usage + source: "what this secret
    # is for, and who's asking".
    ts = next(r for r in table.rows if r.name == "tailscale-auth-key")
    assert ts.description == "(auto) the Tailscale auth key for vm-template/default"
    # Counts match the operator/auto split.
    assert table.operator_count == 0
    assert table.auto_count >= 1


def test_rows_sorted_alphabetically_by_secret_name(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        admin_env={"Z": {"secret": "z-token"}, "A": {"secret": "a-token"}, "M": {"secret": "m-token"}},
        manifests=[
            ManifestDoc("secret", "z-token", description="Z"),
            ManifestDoc("secret", "a-token", description="A"),
            ManifestDoc("secret", "m-token", description="M"),
        ],
    )
    table = _build_table(cfg_file)
    # Operator-declared secrets are sorted alphabetically; the
    # registry also auto-declares ``tailscale-auth-key`` via Phase 1c's
    # VMTemplate requirement, so filter to only the operator-typed
    # names for the order assertion.
    operator_typed = {"a-token", "m-token", "z-token"}
    seen = [r.name for r in table.rows if r.name in operator_typed]
    assert seen == ["a-token", "m-token", "z-token"]


def test_env_var_cell_shows_default_convention_identifier(tmp_path: Path) -> None:
    """No explicit mapping: env-var cell shows ``AW_SECRET_<UPPER>``."""
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        admin_env={"TOKEN": {"secret": "github-token"}},
        manifests=[ManifestDoc("secret", "github-token", description="GitHub PAT")],
    )
    table = _build_table(cfg_file)
    row = table.rows[0]
    env_var_cell = next(c for c in row.cells if c.backend == "env-var")
    assert env_var_cell.would_attempt is True
    assert env_var_cell.identifier == "AW_SECRET_GITHUB_TOKEN"


def test_env_var_cell_shows_mapping_override(tmp_path: Path) -> None:
    """``backend_mappings.env-var = "..."`` is the identifier shown in
    the cell, not the default convention."""
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        admin_env={"TOKEN": {"secret": "github-token"}},
        manifests=[
            ManifestDoc(
                "secret",
                "github-token",
                {"backend_mappings": {"env-var": "GITHUB_TOKEN"}},
                description="GitHub PAT",
            )
        ],
    )
    table = _build_table(cfg_file)
    env_var_cell = next(c for c in table.rows[0].cells if c.backend == "env-var")
    assert env_var_cell.identifier == "GITHUB_TOKEN"


def test_env_var_cell_when_opted_out_reports_wont_attempt(tmp_path: Path) -> None:
    """``backend_mappings.env-var = false``: would_attempt is False so the
    renderer reports ``won't attempt``. Identifier is None."""
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        admin_env={"TOKEN": {"secret": "force-prompt"}},
        manifests=[
            ManifestDoc(
                "secret",
                "force-prompt",
                {"backend_mappings": {"env-var": False}},
                description="Always prompt",
            )
        ],
    )
    table = _build_table(cfg_file)
    env_var_cell = next(c for c in table.rows[0].cells if c.backend == "env-var")
    assert env_var_cell.would_attempt is False
    assert env_var_cell.identifier is None


def test_prompt_cell_has_no_static_identifier(tmp_path: Path) -> None:
    """Prompt always attempts but has no static lookup key; CLI renders
    this as ``would attempt``."""
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        admin_env={"TOKEN": {"secret": "any"}},
        manifests=[ManifestDoc("secret", "any", description="any")],
    )
    table = _build_table(cfg_file)
    prompt_cell = next(c for c in table.rows[0].cells if c.backend == "prompt")
    assert prompt_cell.would_attempt is True
    assert prompt_cell.identifier is None


def test_column_order_matches_backend_chain_precedence(tmp_path: Path) -> None:
    """The columns appear in [secret_config].backends order so operators
    see the resolution order directly in the table layout."""
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        settings="""
        [secret_config]
        backends = ["prompt", "env-var"]
        """,
        admin_env={"TOKEN": {"secret": "x"}},
        manifests=[ManifestDoc("secret", "x", description="x")],
    )
    table = _build_table(cfg_file)
    assert table.backends == ("prompt", "env-var")


def test_names_only_lists_every_registry_secret(tmp_path: Path, monkeypatch) -> None:
    """``agw secret list --names-only`` is the source for shell
    completion; it must include auto-declared names like
    ``tailscale-auth-key`` so completion matches what ``agw secret
    describe`` accepts. Names print one per line in the same order as
    the table's rows."""
    from typer.testing import CliRunner

    from agentworks.cli import app

    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        admin_env={"TOKEN": {"secret": "z-token"}, "OTHER": {"secret": "a-token"}},
        manifests=[
            ManifestDoc("secret", "z-token", description="Z"),
            ManifestDoc("secret", "a-token", description="A"),
        ],
    )
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", cfg_file)

    result = CliRunner().invoke(app, ["secret", "list", "--names-only"])
    assert result.exit_code == 0, result.stdout
    names = [line for line in result.stdout.splitlines() if line]
    # Operator-declared names appear alphabetized; the framework-
    # auto-declared ``tailscale-auth-key`` (VMTemplate requirement) is
    # present too -- the prior completer was sed-over-TOML and missed it.
    assert "a-token" in names
    assert "z-token" in names
    assert "tailscale-auth-key" in names


def test_empty_backend_chain_yields_no_columns(tmp_path: Path) -> None:
    """``backends = []`` opts out of all resolution; the table has no
    backend columns. Operator-declared secrets in this state would
    trip the unreachable-secret config-load error. The
    auto-declared ``tailscale-auth-key`` row (Phase 1c) is still
    surfaced in the table since the env-and-secrets reachability check
    only inspects operator-declared secrets.
    """
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        settings="""
        [secret_config]
        backends = []
        """,
    )
    table = _build_table(cfg_file)
    assert table.backends == ()
    # Auto-declared rows still appear (each with empty cells, since
    # there are no backend columns).
    assert all(r.cells == () for r in table.rows)
    assert table.operator_count == 0


def test_render_secret_table_caps_long_backend_identifier(
    captured_output: CapturedOutput,
) -> None:
    """The LIST view truncates a long backend identifier to
    ``_BACKEND_CELL_WIDTH`` with a trailing ``...`` so it cannot blow the
    table width out. Built directly (no op wiring): the account-first
    onepassword identifier here comfortably exceeds the cap."""
    long_ident = "my.1password.com: op://Employee/Registry/token"
    assert len(long_ident) > _BACKEND_CELL_WIDTH
    table = SecretTable(
        backends=("onepassword",),
        rows=(
            SecretRow(
                name="reg",
                description="registry token",
                cells=(
                    SecretCell(
                        backend="onepassword",
                        would_attempt=True,
                        identifier=long_ident,
                        not_ready_reason=None,
                    ),
                ),
            ),
        ),
        operator_count=1,
        auto_count=0,
    )
    render_secret_table(table)

    truncated = long_ident[: _BACKEND_CELL_WIDTH - 3] + "..."
    assert len(truncated) == _BACKEND_CELL_WIDTH
    joined = "\n".join(captured_output.info)
    # The identifier appears truncated, and the full form never does, so the
    # onepassword column width is bounded by ``_BACKEND_CELL_WIDTH``.
    assert truncated in joined
    assert long_ident not in joined
    data_line = next(line for line in captured_output.info if line.startswith("reg "))
    assert truncated in data_line


def test_render_secret_table_caps_long_name(
    captured_output: CapturedOutput,
) -> None:
    """The LIST view truncates a long secret name to ``_NAME_CELL_WIDTH``
    with a trailing ``...`` so a name approaching the 253-char secret cap
    (#275) cannot blow the table width out. The DETAIL view keeps the
    full name."""
    long_name = "rse-" + "x" * 80
    assert len(long_name) > _NAME_CELL_WIDTH
    table = SecretTable(
        backends=("env-var",),
        rows=(
            SecretRow(
                name=long_name,
                description="long-named secret",
                cells=(
                    SecretCell(
                        backend="env-var",
                        would_attempt=True,
                        identifier=None,
                        not_ready_reason=None,
                    ),
                ),
            ),
        ),
        operator_count=1,
        auto_count=0,
    )
    render_secret_table(table)

    truncated = long_name[: _NAME_CELL_WIDTH - 3] + "..."
    joined = "\n".join(captured_output.info)
    assert truncated in joined
    assert long_name not in joined


def _readiness_grid_config(tmp_path: Path) -> Path:
    """A chain of env-var / onepassword / prompt exercising every R9.7 cell
    state, with ``op`` monkeypatched absent so onepassword folds not-ready."""
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        settings="""
        [plugins]
        system = ["onepassword"]

        [secret_config]
        backends = ["env-var", "onepassword", "prompt"]
        """,
        admin_env={
            "A": {"secret": "mapped-op"},
            "B": {"secret": "prompt-only"},
            "C": {"secret": "unmapped-op"},
        },
        manifests=[
            ManifestDoc("secret-source", "onepassword", {"backend": {"name": "onepassword"}}),
            ManifestDoc(
                "secret",
                "mapped-op",
                {"backend_mappings": {"env-var": False, "onepassword": "op://Vault/item/field"}},
                description="op ref, env-var opted out",
            ),
            ManifestDoc(
                "secret",
                "prompt-only",
                {"backend_mappings": {"env-var": False, "onepassword": False}},
                description="no static key anywhere",
            ),
            ManifestDoc(
                "secret",
                "unmapped-op",
                {"backend_mappings": {"env-var": False}},
                description="onepassword mapping-required, no mapping",
            ),
        ],
    )
    return cfg_file


def test_grid_cell_not_ready_wins_over_identifier(tmp_path: Path, monkeypatch) -> None:
    """R9.7: a not-ready backend (onepassword, no ``op`` on PATH) with a mapped
    ``op://`` ref shows ``not ready: <reason>``, not the ref: it cannot run
    here, so not-ready wins over the identifier."""
    monkeypatch.setattr("shutil.which", lambda name: None)
    table = _build_table(_readiness_grid_config(tmp_path))
    row = next(r for r in table.rows if r.name == "mapped-op")
    op = next(c for c in row.cells if c.backend == "onepassword")
    assert op.would_attempt is True  # has a mapping
    assert op.identifier == "op://Vault/item/field"  # the ref is still known
    assert op.not_ready_reason == "op CLI not installed"  # but readiness wins at render


def test_grid_cell_wont_attempt_wins_over_not_ready(tmp_path: Path, monkeypatch) -> None:
    """R9.7 precedence: a backend that would NOT attempt this secret (onepassword
    is mapping-required and has no mapping) shows ``won't attempt`` even when its
    host tool is absent: readiness is moot for a secret it never attempts."""
    monkeypatch.setattr("shutil.which", lambda name: None)
    table = _build_table(_readiness_grid_config(tmp_path))
    row = next(r for r in table.rows if r.name == "unmapped-op")
    op = next(c for c in row.cells if c.backend == "onepassword")
    assert op.would_attempt is False
    assert op.not_ready_reason == "op CLI not installed"  # backend is not-ready...
    # ...but the rendered cell is won't-attempt (checked below), not not-ready.


def test_render_grid_uses_readiness_vocabulary(tmp_path: Path, monkeypatch, captured_output: CapturedOutput) -> None:
    """R9.7 at the rendered level: cells are the identifier / ``would attempt`` /
    ``not ready: <reason>`` / ``won't attempt`` states, never the retired
    ``enabled`` / ``disabled`` literals."""
    monkeypatch.setattr("shutil.which", lambda name: None)
    table = _build_table(_readiness_grid_config(tmp_path))
    render_secret_table(table)
    out = "\n".join(captured_output.info)

    assert "not ready: op CLI not installed" in out
    assert "op://Vault/item/field" not in out  # hidden: not-ready wins over the ref
    assert "won't attempt" in out
    assert "would attempt" in out  # a ready prompt with no static key
    # The retired overloaded literals are gone as standalone cells.
    for line in captured_output.info:
        cells = [c.strip() for c in line.split("  ") if c.strip()]
        assert "disabled" not in cells
        assert "enabled" not in cells
