"""``agw resource edit KIND/NAME`` -- open the declaring YAML manifest.

Only operator-declared YAML resources are editable (maintainer ruling,
2026-07-05, keep-it-simple scope): TOML-declared resources point at
``agw resource migrate`` / ``agw config edit``; built-in and
auto-declared resources have no file to open. ``edit_location`` is the
service authority; the CLI adds only the KIND/NAME parse and the
$EDITOR launch.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from agentworks.bootstrap import build_registry
from agentworks.config import load_config
from agentworks.errors import NotFoundError, ValidationError
from agentworks.resources.inspect import edit_location


def _write_base(cfg_path: Path, extras: str = "") -> None:
    tmp = cfg_path.parent
    (tmp / "id.pub").write_text("ssh-ed25519 AAAA...")
    (tmp / "id").write_text("-----BEGIN OPENSSH PRIVATE KEY-----")
    cfg_path.write_text(
        dedent(f"""\
        [operator]
        ssh_public_key = "{(tmp / "id.pub").as_posix()}"
        ssh_private_key = "{(tmp / "id").as_posix()}"
        """)
        + dedent(extras)
    )


def _registry(tmp_path: Path, *, toml_extras: str = "", manifest: str = ""):
    cfg = tmp_path / "config.toml"
    _write_base(cfg, toml_extras)
    if manifest:
        resources = tmp_path / "resources"
        resources.mkdir(exist_ok=True)
        (resources / "res.yaml").write_text(dedent(manifest))
    config = load_config(cfg, warn_issues=False)
    return build_registry(config)


def test_yaml_declared_resource_resolves_to_file_and_line(tmp_path: Path) -> None:
    registry = _registry(
        tmp_path,
        manifest="""\
        apiVersion: agentworks/v1
        kind: secret
        metadata:
          name: npm-token
          description: npm token
        spec: {}
        """,
    )
    path, line = edit_location(registry, "secret", "npm-token")
    assert path == tmp_path / "resources" / "res.yaml"
    assert line == 1


def test_toml_declared_resource_points_at_migrate_or_config_edit(
    tmp_path: Path,
) -> None:
    registry = _registry(
        tmp_path,
        toml_extras="""
        [secrets.npm-token]
        description = "npm token"
        """,
    )
    with pytest.raises(ValidationError, match="declared in TOML") as exc:
        edit_location(registry, "secret", "npm-token")
    assert "agw resource migrate secret/npm-token" in (exc.value.hint or "")
    assert "agw config edit" in (exc.value.hint or "")


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
    """Declarable kinds with built-in rows (apt / install-commands) keep
    the sample pointer."""
    registry = _registry(tmp_path)
    with pytest.raises(ValidationError, match="built-in") as exc:
        edit_location(registry, "apt-package", "gh")
    assert "agw resource sample apt-package" in (exc.value.hint or "")


def test_auto_declared_resource_has_no_file_to_edit(tmp_path: Path) -> None:
    """A bare [vm_templates.default] auto-declares tailscale-auth-key."""
    registry = _registry(
        tmp_path,
        toml_extras="""
        [vm_templates.default]
        """,
    )
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


def test_cli_edit_works_when_config_fails_validation(tmp_path: Path, monkeypatch) -> None:
    """The fix-it path: a broken config is exactly when edit is needed
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
    assert "prompt backend has no meaning" in result.output


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
    _write_base(
        cfg,
        """
        [secrets.bad]
        description = "d"
        backend_mappings.prompt = "nope"
        """,
    )
    resources = tmp_path / "resources"
    resources.mkdir()
    (resources / "broken.yaml").write_text("kind: [unclosed\n")
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", cfg)
    monkeypatch.setenv("EDITOR", "test-editor")

    result = CliRunner().invoke(app, ["resource", "edit", "secret/mystery"])
    assert result.exit_code != 0
    assert "broken.yaml" in str(result.exception)
    assert "edit the file directly" in (result.exception.hint or "")  # type: ignore[union-attr]
