"""``agw resource edit KIND/NAME`` -- open the declaring YAML manifest.

Every operator-declared resource is a YAML manifest (ADR 0022), so an
operator-declared origin opens straight away. Built-in and auto-declared
resources have no file to open and say so (maintainer ruling, 2026-07-05,
keep-it-simple scope). ``edit_location`` is the service authority; the CLI
adds only the KIND/NAME parse and the $EDITOR launch.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent
from typing import TYPE_CHECKING

import pytest

from agentworks.bootstrap import build_registry
from agentworks.config import load_config
from agentworks.errors import NotFoundError, ValidationError
from agentworks.resources.inspect import edit_location
from tests.conftest import ManifestDoc, write_manifests

if TYPE_CHECKING:
    from collections.abc import Sequence


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


def _registry(tmp_path: Path, *, manifests: Sequence[ManifestDoc | str] = ()):
    cfg = tmp_path / "config.toml"
    _write_base(cfg)
    # These tests assert on the declaring file's name, so the manifests land
    # in a single ``res.yaml``.
    if manifests:
        write_manifests(tmp_path, *manifests, filename="res.yaml")
    config = load_config(cfg, warn_issues=False)
    return build_registry(config)


def test_yaml_declared_resource_resolves_to_file_and_line(tmp_path: Path) -> None:
    registry = _registry(
        tmp_path,
        manifests=[
            """\
        apiVersion: agentworks/v1
        kind: secret
        metadata:
          name: npm-token
          description: npm token
        spec: {}
        """
        ],
    )
    path, line = edit_location(registry, "secret", "npm-token")
    assert path == tmp_path / "resources" / "res.yaml"
    assert line == 1


# The former ``test_toml_declared_resource_points_at_migrate_or_config_edit``
# was removed here: it pinned ``edit_location``'s "declared in TOML" branch,
# which the TOML resource sunset (ADR 0022) made unreachable, since a
# TOML-declared resource can no longer enter the registry (config.toml
# hard-errors on [secrets.*]). Only YAML resources are editable now, and that
# path is covered by test_yaml_declared_resource_resolves_to_file_and_line.


def test_builtin_capability_has_no_file_to_edit(tmp_path: Path) -> None:
    """Descriptor kinds get the capability wording, never a sample
    pointer (post-collapse, `agw resource sample secret-backend` would
    itself error)."""
    registry = _registry(tmp_path)
    with pytest.raises(ValidationError, match="built-in") as exc:
        edit_location(registry, "secret-backend", "env-var")
    assert "capability provided by the app" in (exc.value.hint or "")
    assert "resource sample" not in (exc.value.hint or "")


def test_builtin_declarable_resource_points_at_sample(tmp_path: Path) -> None:
    """The remaining built-in declarable row keeps its sample pointer."""
    registry = _registry(tmp_path)
    with pytest.raises(ValidationError, match="built-in") as exc:
        edit_location(registry, "vm-site", "lima-local")
    assert "agw resource sample vm-site" in (exc.value.hint or "")


def test_auto_declared_resource_has_no_file_to_edit(tmp_path: Path) -> None:
    """A bare vm-template/default auto-declares tailscale-auth-key."""
    registry = _registry(tmp_path, manifests=[ManifestDoc("vm-template", "default")])
    with pytest.raises(ValidationError, match="auto-declared"):
        edit_location(registry, "secret", "tailscale-auth-key")


def test_unknown_kind_and_name_reuse_describe_errors(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    with pytest.raises(NotFoundError, match="unknown kind"):
        edit_location(registry, "nope", "x")
    with pytest.raises(NotFoundError, match="no secret named"):
        edit_location(registry, "secret", "nope")


def test_cli_edit_launches_editor_on_manifest(tmp_path: Path, monkeypatch) -> None:
    from typer.testing import CliRunner

    from agentworks.cli import app

    cfg = tmp_path / "config.toml"
    _write_base(cfg)
    resources = tmp_path / "resources"
    resources.mkdir()
    (resources / "secrets.yaml").write_text(
        dedent("""\
        apiVersion: agentworks/v1
        kind: secret
        metadata:
          name: npm-token
          description: npm token
        spec: {}
        """)
    )
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", cfg)
    monkeypatch.setenv("EDITOR", "test-editor")
    calls: list[list[str]] = []
    monkeypatch.setattr("subprocess.call", lambda argv: calls.append(argv) or 0)

    result = CliRunner().invoke(app, ["resource", "edit", "secret/npm-token"])
    assert result.exit_code == 0, result.output
    assert calls == [["test-editor", str(resources / "secrets.yaml")]]
    assert "Editing secret/npm-token" in result.output
    assert "secrets.yaml:1" in result.output


def test_cli_edit_names_the_manifest_home_relative(tmp_path: Path, monkeypatch) -> None:
    """The "Editing" line frames its path like every manifest error does.

    The assertion above it is a SUFFIX match, which is why this needs its
    own test: ``tmp_path`` is never under ``$HOME``, so ``~/`` and the
    absolute path are byte-identical there and a hand-rolled
    ``f"{path}:{line}"`` passes it. Patching home is what makes the two
    distinguishable, and an operator always has the distinguishing case.
    """
    from typer.testing import CliRunner

    from agentworks.cli import app

    home = tmp_path / "home"
    (home / ".config" / "agentworks").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    cfg = home / ".config" / "agentworks" / "config.toml"
    _write_base(cfg)
    resources = cfg.parent / "resources"
    resources.mkdir()
    (resources / "secrets.yaml").write_text(
        dedent("""\
        apiVersion: agentworks/v1
        kind: secret
        metadata:
          name: npm-token
          description: npm token
        spec: {}
        """)
    )
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", cfg)
    monkeypatch.setenv("EDITOR", "test-editor")
    monkeypatch.setattr("subprocess.call", lambda argv: 0)

    result = CliRunner().invoke(app, ["resource", "edit", "secret/npm-token"])

    assert result.exit_code == 0, result.output
    assert "Editing secret/npm-token (~/.config/agentworks/resources/secrets.yaml:1)" in result.output
    assert str(home) not in result.output


def test_cli_edit_requires_editor_env(tmp_path: Path, monkeypatch) -> None:
    from typer.testing import CliRunner

    from agentworks.cli import app

    cfg = tmp_path / "config.toml"
    _write_base(cfg)
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", cfg)
    monkeypatch.delenv("EDITOR", raising=False)
    monkeypatch.delenv("VISUAL", raising=False)

    result = CliRunner().invoke(app, ["resource", "edit", "secret/x"])
    assert result.exit_code == 1
    assert "$EDITOR is not set" in result.output


def test_cli_edit_rejects_token_without_slash(tmp_path: Path, monkeypatch) -> None:
    from typer.testing import CliRunner

    from agentworks.cli import app

    cfg = tmp_path / "config.toml"
    _write_base(cfg)
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", cfg)
    monkeypatch.setenv("EDITOR", "test-editor")

    result = CliRunner().invoke(app, ["resource", "edit", "secret"])
    assert result.exit_code != 0
    assert "expected KIND/NAME" in str(result.exception)


def test_cli_edit_works_when_manifests_fail_validation(tmp_path: Path, monkeypatch) -> None:
    """The fix-it path: a broken manifest is exactly when edit is needed
    most (the maintainer hit this breaking YAML intentionally). A
    ConfigError from the strict path falls back to a tolerant,
    validation-free envelope scan; the declaring file still opens, with
    a warning naming the validation failure."""
    from typer.testing import CliRunner

    from agentworks.cli import app

    cfg = tmp_path / "config.toml"
    _write_base(cfg)
    resources = tmp_path / "resources"
    resources.mkdir()
    (resources / "secrets.yaml").write_text(
        dedent("""\
        apiVersion: agentworks/v1
        kind: secret
        metadata:
          name: openai-api-key
          description: OpenAI key
        spec:
          backend_mappings:
            prompt: broken-on-purpose
        """)
    )
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", cfg)
    monkeypatch.setenv("EDITOR", "test-editor")
    calls: list[list[str]] = []
    monkeypatch.setattr("subprocess.call", lambda argv: calls.append(argv) or 0)

    result = CliRunner().invoke(app, ["resource", "edit", "secret/openai-api-key"])
    assert result.exit_code == 0, result.output
    assert calls == [["test-editor", str(resources / "secrets.yaml")]]
    assert "config is currently failing validation" in result.output
    assert "prompt backend has no mapping vocabulary" in result.output


def test_fallback_scan_tolerates_broken_sibling_files(tmp_path: Path) -> None:
    """A file with a YAML syntax error is skipped (and reported); the
    target in a parseable file is still found."""
    from agentworks.manifests.loader import locate_document

    resources = tmp_path / "resources"
    resources.mkdir()
    (resources / "broken.yaml").write_text("kind: [unclosed\n")
    (resources / "ok.yaml").write_text(
        dedent("""\
        apiVersion: agentworks/v1
        kind: secret
        metadata:
          name: findable
        spec: {}
        """)
    )
    found = locate_document(resources, "secret", "findable")
    assert found.location is not None
    assert found.location.file == resources / "ok.yaml"
    assert found.location.line == 1
    assert found.unreadable == (resources / "broken.yaml",)


def test_fallback_miss_names_unreadable_files(tmp_path: Path, monkeypatch) -> None:
    """When the target isn't found AND some files couldn't be parsed,
    the original config error re-raises with a hint naming them --
    the resource may live in the unparseable file."""
    from typer.testing import CliRunner

    from agentworks.cli import app

    cfg = tmp_path / "config.toml"
    _write_base(cfg)
    # An unparseable manifest is itself what makes the config fail validation
    # here (the strict registry build raises), so the CLI falls back to the
    # tolerant scan; that scan cannot read broken.yaml and must name it.
    resources = tmp_path / "resources"
    resources.mkdir()
    (resources / "broken.yaml").write_text("kind: [unclosed\n")
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", cfg)
    monkeypatch.setenv("EDITOR", "test-editor")

    result = CliRunner().invoke(app, ["resource", "edit", "secret/mystery"])
    assert result.exit_code != 0
    assert "broken.yaml" in str(result.exception)
    assert "edit the file directly" in (result.exception.hint or "")  # type: ignore[union-attr]
