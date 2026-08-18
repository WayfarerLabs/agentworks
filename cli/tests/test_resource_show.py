"""Command and renderer boundaries for ``agw resource show``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import yaml
from typer.testing import CliRunner

from agentworks.cli import app
from agentworks.completions.bash import generate_bash
from agentworks.completions.powershell import generate_powershell
from agentworks.completions.spec import DYNAMIC_COMPLETIONS, CommandSpec, build_spec, completion_version
from agentworks.completions.zsh import generate_zsh
from agentworks.errors import NotFoundError
from agentworks.machine_output import JsonObject
from agentworks.origin import Origin
from agentworks.output import Role
from agentworks.resources.access import ResourceIdentity
from agentworks.resources.graph import Enablement
from agentworks.resources.show import ResourceReadiness, ResourceShow, render_resource_show
from tests.conftest import CapturedOutput

if TYPE_CHECKING:
    from collections.abc import Callable


def _shown(*, declaration: JsonObject | None = None) -> ResourceShow:
    return ResourceShow(
        identity=ResourceIdentity("secret", "npm-token"),
        category="declarable" if declaration is not None else "capability",
        description="npm registry token",
        origin=Origin.operator_declared(file=Path("resources/secrets.yaml"), line=7),
        enablement=Enablement.enabled,
        readiness=ResourceReadiness(is_ready=False, is_available=True, reason="backend unavailable"),
        declaration=declaration,
    )


def test_human_renderer_projects_complete_structural_facts_and_parseable_manifest(
    captured_output: CapturedOutput,
) -> None:
    declaration: JsonObject = {
        "apiVersion": "agentworks/v1",
        "kind": "secret",
        "metadata": {"name": "npm-token", "description": "npm registry token"},
        "spec": {"backend_mappings": {"env-var": "NPM_TOKEN"}},
    }
    shown = _shown(declaration=declaration)

    render_resource_show(shown)

    top_level = [message for role, level, message in captured_output.lines if role is Role.BODY and level == 0]
    nested = [message for role, level, message in captured_output.lines if role is Role.BODY and level == 1]
    assert len(top_level) == 7
    assert len(nested) > 3
    assert all(value in "\n".join(top_level) for value in ("secret", "npm-token", "declarable", "enabled"))
    assert all(value in "\n".join(nested[:3]) for value in ("false", "true", "backend unavailable"))
    assert yaml.safe_load("\n".join(nested[3:])) == declaration


def test_human_renderer_makes_disabled_capability_nulls_structural(
    captured_output: CapturedOutput,
) -> None:
    shown = ResourceShow(
        identity=ResourceIdentity("vm-platform", "example"),
        category="capability",
        description="",
        origin=None,
        enablement=Enablement.disabled,
        readiness=None,
        declaration=None,
    )

    render_resource_show(shown)

    assert len(captured_output.lines) == 7
    assert all(role is Role.BODY and level == 0 for role, level, _message in captured_output.lines)
    assert sum(message.endswith("null") for _role, _level, message in captured_output.lines) == 2


def test_human_renderer_neutralizes_scalar_lines_and_preserves_yaml_values(
    captured_output: CapturedOutput,
) -> None:
    hostile = "one\npeer\t\x1b[31mred\x7f\u009b"
    declaration: JsonObject = {
        "apiVersion": "agentworks/v1",
        "kind": "secret",
        "metadata": {"name": "npm-token", "description": hostile},
        "spec": {"hint": hostile},
    }
    shown = ResourceShow(
        identity=ResourceIdentity("secret", f"npm-{hostile}"),
        category="declarable",
        description=hostile,
        origin=Origin.system_plugin(plugin=hostile, source=hostile),
        enablement=Enablement.enabled,
        readiness=ResourceReadiness(False, True, hostile),
        declaration=declaration,
    )

    render_resource_show(shown)

    messages = [message for _role, _level, message in captured_output.lines]
    assert all("\n" not in message and "\t" not in message for message in messages)
    assert all(
        not any(ord(character) < 0x20 or 0x7F <= ord(character) <= 0x9F for character in message)
        for message in messages
    )
    nested = [message for _role, level, message in captured_output.lines if level == 1]
    assert yaml.safe_load("\n".join(nested[3:])) == declaration


def test_cli_wires_silent_loaders_and_human_renderer(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentworks import bootstrap, config
    from agentworks.cli.commands import resource
    from agentworks.resources import show

    registry = object()
    expected = _shown()
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(config, "load_config", lambda **kwargs: calls.append(("config", kwargs)) or object())
    monkeypatch.setattr(
        bootstrap,
        "load_request_registry",
        lambda _config, **kwargs: calls.append(("registry", kwargs)) or registry,
    )
    monkeypatch.setattr(
        show,
        "show_resource",
        lambda actual, identity: calls.append(("show", actual, identity)) or expected,
    )
    monkeypatch.setattr(show, "render_resource_show", lambda shown: calls.append(("render", shown)))
    monkeypatch.setattr(resource, "get_db", lambda: pytest.fail("resource show opened the database"))

    result = CliRunner().invoke(app, ["resource", "show", "secret/npm-token"])

    assert result.exit_code == 0, result.output
    assert calls == [
        ("config", {"warn_issues": False}),
        ("registry", {"warn": False}),
        ("show", registry, ResourceIdentity("secret", "npm-token")),
        ("render", expected),
    ]


def test_cli_json_uses_resource_show_identity_and_closed_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentworks import bootstrap, config
    from agentworks.resources import show

    expected = _shown(declaration=None)
    monkeypatch.setattr(config, "load_config", lambda **_kwargs: object())
    monkeypatch.setattr(bootstrap, "load_request_registry", lambda _config, **_kwargs: object())
    monkeypatch.setattr(show, "show_resource", lambda _registry, _identity: expected)

    result = CliRunner().invoke(
        app,
        ["resource", "show", "secret/npm-token", "--output", "json"],
    )

    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    assert document["schema_version"] == 1
    assert document["command"] == "resource.show"
    resource = document["data"]["resource"]
    assert list(resource) == [
        "kind",
        "name",
        "category",
        "description",
        "origin",
        "enablement",
        "readiness",
        "declaration",
    ]
    assert resource["declaration"] is None
    assert resource["readiness"] == {
        "is_ready": False,
        "is_available": True,
        "reason": "backend unavailable",
    }


@pytest.mark.parametrize(
    "argv",
    [
        ["resource", "show", "missing-slash"],
        ["resource", "show", "secret/npm-token", "--output", "yaml"],
    ],
)
def test_cli_grammar_errors_precede_config(
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
) -> None:
    from agentworks import config

    monkeypatch.setattr(config, "load_config", lambda **_kwargs: pytest.fail("config loaded"))

    result = CliRunner().invoke(app, argv)

    assert result.exit_code != 0
    assert result.stdout_bytes == b""


def test_cli_lookup_failure_writes_no_partial_output(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentworks import bootstrap, config
    from agentworks.resources import show

    error = NotFoundError("missing")
    monkeypatch.setattr(config, "load_config", lambda **_kwargs: object())
    monkeypatch.setattr(bootstrap, "load_request_registry", lambda _config, **_kwargs: object())
    monkeypatch.setattr(show, "show_resource", lambda _registry, _identity: (_ for _ in ()).throw(error))

    result = CliRunner().invoke(
        app,
        ["resource", "show", "secret/missing", "--output", "json"],
    )

    assert result.exit_code != 0
    assert result.stdout_bytes == b""
    assert result.exception is error


def test_help_and_completion_spec_expose_one_ref_and_closed_output() -> None:
    spec = build_spec(app)
    show = spec.subcommands["resource"].subcommands["show"]
    ref = next(parameter for parameter in show.params if parameter.name == "ref")
    output_format = next(parameter for parameter in show.params if parameter.name == "output_format")

    assert ref.is_argument
    assert ref.required
    assert ref.dynamic_completer == "resource_refs"
    assert output_format.choices == ["human", "json"]
    assert DYNAMIC_COMPLETIONS[("resource.show", "ref")] == "resource_refs"


def test_all_shell_generators_use_registry_identity_completion() -> None:
    full = build_spec(app)
    show = full.subcommands["resource"].subcommands["show"]
    focused = CommandSpec(
        name="agentworks",
        help="",
        subcommands={
            "resource": CommandSpec(
                name="resource",
                help="",
                subcommands={"show": show},
            )
        },
    )
    version = completion_version(focused)
    generators: tuple[Callable[[CommandSpec, str], str], ...] = (
        generate_bash,
        generate_zsh,
        generate_powershell,
    )

    for generate in generators:
        script = generate(focused, version)
        assert "agw resource list --names-only" in script
