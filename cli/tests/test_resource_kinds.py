"""``agw resource kinds`` -- the read-only, code-defined kind inventory.

CATEGORY is per-kind by construction (ADR 0016's expanded resource
definition): `declarable` kinds hold data, `capability` kinds hold
read-only rows backed by registered code. Kinds are baked into the app;
plugins publish resources of existing kinds (declarable and capability
alike), never new kinds.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

from agentworks.resources import KIND_REGISTRY
from agentworks.secrets import SECRET_BACKEND_REGISTRY


def _write_base(cfg_path: Path) -> None:
    tmp = cfg_path.parent
    (tmp / "id.pub").write_text("ssh-ed25519 AAAA...")
    (tmp / "id").write_text("-----BEGIN OPENSSH PRIVATE KEY-----")
    cfg_path.write_text(
        dedent(f"""\
        [operator]
        ssh_public_key = "{(tmp / "id.pub").as_posix()}"
        ssh_private_key = "{(tmp / "id").as_posix()}"
        """)
    )


def test_every_kind_declares_category_and_description() -> None:
    """The protocol contract: every registered kind carries the per-kind
    classifier and an operator-facing description."""
    for name, handler in KIND_REGISTRY.items():
        assert handler.category in ("declarable", "capability"), name
        assert handler.description, name


def test_capability_kinds_are_exactly_the_code_backed_ones() -> None:
    capability = {
        name
        for name, handler in KIND_REGISTRY.items()
        if handler.category == "capability"
    }
    assert capability == {
        "secret-backend",
        "git-credential-provider",
        "vm-platform",
        "harness",
    }


def test_names_only_needs_no_config(tmp_path: Path, monkeypatch) -> None:
    """The completion path must work with a broken or absent config:
    kinds are static code. Point CONFIG_PATH at a nonexistent file and
    the names-only listing still succeeds."""
    from typer.testing import CliRunner

    from agentworks.cli import app

    monkeypatch.setattr(
        "agentworks.config.CONFIG_PATH", tmp_path / "nope" / "config.toml"
    )
    result = CliRunner().invoke(app, ["resource", "kinds", "--names-only"])
    assert result.exit_code == 0, result.output
    lines = [line for line in result.output.splitlines() if line]
    assert lines == sorted(KIND_REGISTRY)


def test_table_shows_categories_and_counts(tmp_path: Path, monkeypatch) -> None:
    from typer.testing import CliRunner

    from agentworks.cli import app

    cfg = tmp_path / "config.toml"
    _write_base(cfg)
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", cfg)

    result = CliRunner().invoke(app, ["resource", "kinds"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "KIND" in out and "CATEGORY" in out and "RESOURCES" in out
    # The capability rows carry their classifier and real counts (all
    # built-in backends registered).
    (backend_line,) = [
        line for line in out.splitlines() if line.startswith("secret-backend ")
    ]
    assert backend_line.split()[1] == "capability"
    # env-var, prompt, onepassword.
    assert backend_line.split()[2] == str(len(SECRET_BACKEND_REGISTRY))
    (secret_line,) = [
        line
        for line in out.splitlines()
        if line.startswith("secret ") or line.startswith("secret  ")
    ]
    assert "declarable" in secret_line
