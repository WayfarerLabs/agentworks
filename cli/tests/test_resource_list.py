"""Tests for ``agentworks.resources.inspect.list_resources`` -- the
service layer behind ``agw resource list`` (Phase 2c).

The cross-kind list stops at framework-uniform fields (kind, name,
origin, usage count, description). Kind-specific detail lives in the
per-kind commands. Filters (``--kind``, ``--origin``) narrow the rows;
the summary counts reflect the post-filter view.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

from agentworks.bootstrap import build_registry
from agentworks.config import load_config
from agentworks.resources.inspect import list_resources
from agentworks.resources.render import format_origin_line


def _write_base(config_path: Path, *, extras: str = "") -> None:
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
        + dedent(extras),
    )


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
        extras="""
        [vm_templates.default]
        apt = ["zsh"]

        [secrets.my-key]
        description = "k"

        [secret_config]
        backends = ["env-var"]
        """,
    )
    registry = _load(cfg_file)
    listing = list_resources(registry)

    kinds_seen = {row.kind for row in listing.rows}
    # vm-template (operator-declared default), secret (operator + auto),
    # secret-backend (active env-var), agent-template (default built-in),
    # apt-package (catalog publisher), git-credential-provider (catalog),
    # etc. We assert presence of the key cross-kind expectations rather
    # than the full set, since publishers may add more.
    assert "vm-template" in kinds_seen
    assert "secret" in kinds_seen
    assert "secret-backend" in kinds_seen
    assert "agent-template" in kinds_seen


def test_kind_filter_narrows_rows_and_summary(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        extras="""
        [vm_templates.default]
        apt = ["zsh"]

        [secrets.my-key]
        description = "k"

        [secret_config]
        backends = ["env-var"]
        """,
    )
    registry = _load(cfg_file)
    listing = list_resources(registry, kinds=("secret",))

    assert {r.kind for r in listing.rows} == {"secret"}
    # Summary counts are post-filter -- they reflect only the visible
    # rows so the header doesn't mislead the operator.
    assert (
        listing.operator_count
        + listing.auto_count
        + listing.code_count
        == len(listing.rows)
    )


def test_kind_filter_accepts_multiple_kinds(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        extras="""
        [vm_templates.default]
        apt = ["zsh"]

        [secret_config]
        backends = ["env-var"]
        """,
    )
    registry = _load(cfg_file)
    listing = list_resources(registry, kinds=("vm-template", "secret-backend"))

    kinds_seen = {row.kind for row in listing.rows}
    assert kinds_seen == {"vm-template", "secret-backend"}


def test_unknown_kind_filter_yields_empty_listing(tmp_path: Path) -> None:
    """Unknown kinds aren't an error at the service layer -- they just
    return zero rows. CLI flag validation is a separate concern.
    """
    cfg_file = tmp_path / "config.toml"
    _write_base(cfg_file)
    registry = _load(cfg_file)
    listing = list_resources(registry, kinds=("does_not_exist",))

    assert listing.rows == ()
    assert listing.operator_count == 0
    assert listing.auto_count == 0
    assert listing.code_count == 0


# -- Origin filter ----------------------------------------------------------


def test_origin_filter_operator_only_shows_operator_declared(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        extras="""
        [vm_templates.default]
        apt = ["zsh"]

        [secrets.my-key]
        description = "k"

        [secret_config]
        backends = ["env-var"]
        """,
    )
    registry = _load(cfg_file)
    listing = list_resources(registry, origin_filter="operator")

    assert all(
        row.origin is not None and row.origin.variant == "operator-declared"
        for row in listing.rows
    )
    assert listing.operator_count == len(listing.rows)
    assert listing.auto_count == 0
    assert listing.code_count == 0


def test_origin_filter_auto_only_shows_auto_declared(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        extras="""
        [vm_templates.default]
        apt = ["zsh"]

        [secret_config]
        backends = ["env-var"]
        """,
    )
    registry = _load(cfg_file)
    listing = list_resources(registry, origin_filter="auto")

    assert all(
        row.origin is not None and row.origin.variant == "auto-declared"
        for row in listing.rows
    )
    assert listing.auto_count == len(listing.rows)


def test_origin_filter_code_only_shows_built_in(tmp_path: Path) -> None:
    """Code-declared resources include the default ``agent-template``
    (and other framework defaults). The filter narrows to just those.
    """
    cfg_file = tmp_path / "config.toml"
    _write_base(cfg_file)
    registry = _load(cfg_file)
    listing = list_resources(registry, origin_filter="builtin")

    assert all(
        row.origin is not None and row.origin.variant == "built-in"
        for row in listing.rows
    )
    assert listing.code_count == len(listing.rows)


# -- Origin rendering --------------------------------------------------------


def test_format_origin_line_renders_each_variant(tmp_path: Path) -> None:
    """``format_origin_line`` is the framework-shared origin renderer
    used by both the cross-kind list and per-kind describe views; the
    list view emits it as a single cell, so we assert variant strings
    are present and no unknown variants slip in.
    """
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        extras="""
        [secrets.my-key]
        description = "k"

        [secret_config]
        backends = ["env-var"]
        """,
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
        extras="""
        [vm_templates.default]
        apt = ["zsh"]

        [secrets.my-key]
        description = "operator note"

        [secret_config]
        backends = ["env-var"]
        """,
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


def test_cli_names_only_emits_kind_colon_name_per_line(
    tmp_path: Path, monkeypatch
) -> None:
    """``agw resource list --names-only`` is the source for shell
    completion; the line format is ``<kind>:<name>``. Completion
    snippets (bash/zsh/powershell) parse this with ``awk -F:``.
    """
    from typer.testing import CliRunner

    from agentworks.cli import app

    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        extras="""
        [vm_templates.default]
        apt = ["zsh"]
        """,
    )
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", cfg_file)

    result = CliRunner().invoke(app, ["resource", "list", "--names-only"])
    assert result.exit_code == 0, result.stdout
    lines = [line for line in result.stdout.splitlines() if line]
    assert lines, "expected at least one resource row"
    for line in lines:
        assert ":" in line
    # Spot-check known framework defaults appear (vm-template:default
    # operator-declared; tailscale-auth-key auto-declared).
    assert "vm-template:default" in lines
    assert "secret:tailscale-auth-key" in lines


def test_cli_kind_csv_filter(tmp_path: Path, monkeypatch) -> None:
    from typer.testing import CliRunner

    from agentworks.cli import app

    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        extras="""
        [vm_templates.default]
        apt = ["zsh"]
        """,
    )
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", cfg_file)

    result = CliRunner().invoke(
        app,
        ["resource", "list", "--kind", "vm-template,secret", "--names-only"],
    )
    assert result.exit_code == 0, result.stdout
    lines = [line for line in result.stdout.splitlines() if line]
    seen_kinds = {line.split(":", 1)[0] for line in lines}
    assert seen_kinds == {"vm-template", "secret"}


def test_cli_kind_csv_filter_tolerates_whitespace(
    tmp_path: Path, monkeypatch
) -> None:
    """``--kind vm-template, secret`` (with a space) parses the same as
    ``--kind vm-template,secret``. Commas can't appear in kind
    identifiers, so a forgiving parse is safe.
    """
    from typer.testing import CliRunner

    from agentworks.cli import app

    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        extras="""
        [vm_templates.default]
        apt = ["zsh"]
        """,
    )
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", cfg_file)

    result = CliRunner().invoke(
        app,
        ["resource", "list", "--kind", "vm-template, secret", "--names-only"],
    )
    assert result.exit_code == 0, result.stdout
    lines = [line for line in result.stdout.splitlines() if line]
    seen_kinds = {line.split(":", 1)[0] for line in lines}
    assert seen_kinds == {"vm-template", "secret"}


def test_cli_names_only_with_unknown_kind_emits_nothing(
    tmp_path: Path, monkeypatch
) -> None:
    """``--names-only`` against a filter that resolves to zero rows
    emits no output -- no header, no "No resources match." message.
    Required by the ``--names-only`` cli convention so completion
    candidate sets stay clean when nothing matches.
    """
    from typer.testing import CliRunner

    from agentworks.cli import app

    cfg_file = tmp_path / "config.toml"
    _write_base(cfg_file)
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", cfg_file)

    result = CliRunner().invoke(
        app,
        ["resource", "list", "--kind", "does_not_exist", "--names-only"],
    )
    assert result.exit_code == 0, result.stdout
    assert result.stdout.strip() == ""


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


def test_cli_invalid_origin_filter_is_rejected(
    tmp_path: Path, monkeypatch
) -> None:
    from typer.testing import CliRunner

    from agentworks.cli import app

    cfg_file = tmp_path / "config.toml"
    _write_base(cfg_file)
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", cfg_file)

    from agentworks.errors import ValidationError

    result = CliRunner().invoke(
        app, ["resource", "list", "--origin", "bogus"]
    )
    assert result.exit_code != 0
    # The typed ValidationError surfaces with the allowed list so the
    # operator can self-correct without reading the help text.
    assert isinstance(result.exception, ValidationError)
    assert "operator" in str(result.exception)
