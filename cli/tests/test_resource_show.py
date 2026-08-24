"""Command and renderer boundaries for ``agw resource show``."""

from __future__ import annotations

import json
import unicodedata
from pathlib import Path
from textwrap import dedent
from typing import TYPE_CHECKING, cast

import pytest
import yaml
from typer.testing import CliRunner

from agentworks.capabilities.secret_backend import TtyInteractionAccess
from agentworks.cli import app
from agentworks.completions.bash import generate_bash
from agentworks.completions.powershell import generate_powershell
from agentworks.completions.spec import DYNAMIC_COMPLETIONS, CommandSpec, build_spec, completion_version
from agentworks.completions.zsh import generate_zsh
from agentworks.doctor import HealthCheck, Status
from agentworks.errors import NotFoundError
from agentworks.machine_output import JsonObject
from agentworks.origin import Origin
from agentworks.output import Role
from agentworks.resources.access import ResourceIdentity
from agentworks.resources.graph import Enablement, Readiness
from agentworks.resources.graph_query import GraphEdge, GraphEdgeType, GraphIdentity, GraphNodeType
from agentworks.resources.inspect import ResourceSummary
from agentworks.resources.kind import InstanceRef
from agentworks.resources.reference import RefRelationship
from agentworks.resources.show import FocusedRelationships, ResourceShow, render_resource_show
from tests.conftest import CapturedOutput, ManifestDoc, write_cfg, write_manifests

if TYPE_CHECKING:
    from collections.abc import Callable


def _shown(
    *,
    declaration: JsonObject | None = None,
    diagnostics: tuple[HealthCheck, ...] = (),
) -> ResourceShow:
    return ResourceShow(
        summary=ResourceSummary(
            kind="secret",
            name="npm-token",
            origin=Origin.operator_declared(file=Path("resources/secrets.yaml"), line=7),
            reference_count=0,
            used_by_count=0,
            description="npm registry token",
            not_ready_reason="backend unavailable",
        ),
        category="declarable" if declaration is not None else "capability",
        enablement=Enablement.enabled,
        readiness=Readiness.blocked("backend unavailable"),
        relationships=FocusedRelationships((), ()),
        used_by=(),
        diagnostics=diagnostics,
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
    assert len(top_level) >= 12
    assert len(nested) > 3
    assert all(value in "\n".join(top_level) for value in ("secret", "npm-token", "declarable", "enabled"))
    assert all(value in "\n".join(nested[:3]) for value in ("false", "true", "backend unavailable"))
    declaration_lines = nested[-len(yaml.safe_dump(declaration, allow_unicode=False, sort_keys=False).splitlines()) :]
    assert yaml.safe_load("\n".join(declaration_lines)) == declaration


def test_human_renderer_makes_disabled_capability_nulls_structural(
    captured_output: CapturedOutput,
) -> None:
    shown = ResourceShow(
        summary=ResourceSummary("vm-platform", "example", None, 0, None, "", disabled=True),
        category="capability",
        enablement=Enablement.disabled,
        readiness=None,
        relationships=FocusedRelationships((), ()),
        used_by=None,
        diagnostics=(),
        declaration=None,
    )

    render_resource_show(shown)

    assert all(role is Role.BODY for role, _level, _message in captured_output.lines)
    assert sum(message.endswith("null") for _role, _level, message in captured_output.lines) >= 3


def test_human_renderer_neutralizes_scalar_lines_and_preserves_yaml_values(
    captured_output: CapturedOutput,
) -> None:
    ordinary_unicode = "café 雪"
    hostile = "one\npeer\t\x1b[31mred\x7f\u0085\u009b\u2028\u2029\u202e\u2066\ud800"
    combined = f"{ordinary_unicode} {hostile}"
    declaration: JsonObject = {
        "apiVersion": "agentworks/v1",
        "kind": "secret",
        "metadata": {"name": "npm-token", "description": combined},
        "spec": {"hint": combined},
    }
    dependency = GraphEdge(
        GraphEdgeType.DECLARED,
        GraphIdentity(GraphNodeType.RESOURCE, "secret", f"npm-{combined}"),
        GraphIdentity(GraphNodeType.RESOURCE, "secret-source", combined),
        RefRelationship.USES,
        combined,
        ResourceIdentity("secret", combined),
    )
    shown = ResourceShow(
        summary=ResourceSummary(
            "secret",
            f"npm-{combined}",
            Origin.system_plugin(plugin=combined, source=combined),
            0,
            0,
            combined,
            combined,
        ),
        category="declarable",
        enablement=Enablement.enabled,
        readiness=Readiness.blocked(combined),
        relationships=FocusedRelationships((dependency,), ()),
        used_by=(InstanceRef(combined, combined),),
        diagnostics=(HealthCheck(combined, Status.WARN, combined, combined),),
        declaration=declaration,
    )

    render_resource_show(shown)

    messages = [message for _role, _level, message in captured_output.lines]
    assert all(
        not any(unicodedata.category(character) in {"Cc", "Cf", "Cs", "Zl", "Zp"} for character in message)
        for message in messages
    )
    top_level = [message for _role, level, message in captured_output.lines if level == 0]
    assert any(ordinary_unicode in message for message in top_level)
    nested = [message for _role, level, message in captured_output.lines if level == 1]
    declaration_lines = nested[-len(yaml.safe_dump(declaration, allow_unicode=False, sort_keys=False).splitlines()) :]
    assert all(message.isascii() for message in declaration_lines)
    assert yaml.safe_load("\n".join(declaration_lines)) == declaration


def test_cli_wires_warning_loaders_and_human_renderer(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentworks import bootstrap, config
    from agentworks.resources import show
    from agentworks.resources.graph_query import DatabaseLiveSource

    loaded_config = object()
    registry = object()
    expected = _shown()
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(config, "load_config", lambda **kwargs: calls.append(("config", kwargs)) or loaded_config)
    monkeypatch.setattr(
        bootstrap,
        "load_request_registry",
        lambda _config, **kwargs: calls.append(("registry", kwargs)) or registry,
    )
    monkeypatch.setattr(
        show,
        "show_resource",
        lambda *args, **kwargs: calls.append(("show", *args, kwargs)) or expected,
    )
    monkeypatch.setattr(show, "render_resource_show", lambda shown: calls.append(("render", shown)))
    result = CliRunner().invoke(app, ["resource", "show", "secret/npm-token"])

    assert result.exit_code == 0, result.output
    assert [call for call in calls if call[0] != "show"] == [
        ("config", {"warn_issues": True, "workload_gated_issues_fatal": False}),
        ("registry", {"warn": True}),
        ("render", expected),
    ]
    show_call = next(call for call in calls if call[0] == "show")
    assert show_call[1] is loaded_config
    assert show_call[2] is registry
    identity = show_call[3]
    assert isinstance(identity, ResourceIdentity)
    assert (identity.kind, identity.name) == ("secret", "npm-token")
    assert isinstance(show_call[4], DatabaseLiveSource)
    show_kwargs = cast("dict[str, object]", show_call[5])
    assert show_kwargs == {"tty_access": TtyInteractionAccess.UNAVAILABLE}


def test_cli_json_uses_resource_show_identity_and_closed_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentworks import bootstrap, config
    from agentworks.resources import show

    expected = _shown(declaration=None)
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(config, "load_config", lambda **kwargs: calls.append(("config", kwargs)) or object())
    monkeypatch.setattr(
        bootstrap,
        "load_request_registry",
        lambda _config, **kwargs: calls.append(("registry", kwargs)) or object(),
    )
    monkeypatch.setattr(show, "show_resource", lambda *_args, **_kwargs: expected)

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
        "origin",
        "reference_count",
        "used_by_count",
        "description",
        "not_ready_reason",
        "disabled",
        "category",
        "enablement",
        "readiness",
        "relationships",
        "used_by",
        "diagnostics",
        "declaration",
    ]
    assert resource["declaration"] is None
    assert resource["readiness"] == {
        "is_ready": False,
        "is_available": True,
        "reason": "backend unavailable",
    }
    assert calls == [
        ("config", {"warn_issues": False, "workload_gated_issues_fatal": False}),
        ("registry", {"warn": False}),
    ]


def test_cli_json_safely_encodes_hostile_manifest_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentworks import config

    ordinary = "café 雪"
    unsafe = "\ud800\u2028\u2029\u202e\u2066"
    hint = f"{ordinary} {unsafe}"
    config_path = write_cfg(
        tmp_path,
        ManifestDoc("secret", "surrogate-probe", {"hint": hint}, description="surrogate probe"),
    )
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)

    result = CliRunner().invoke(
        app,
        ["resource", "show", "secret/surrogate-probe", "--output", "json"],
    )

    assert result.exit_code == 0, result.output
    assert result.stderr_bytes == b""
    document = json.loads(result.stdout_bytes)
    assert document["data"]["resource"]["declaration"]["spec"]["hint"] == hint
    encoded_document = result.stdout_bytes.removesuffix(b"\n").decode("utf-8")
    assert all(unicodedata.category(character) not in {"Cc", "Cf", "Cs", "Zl", "Zp"} for character in encoded_document)
    assert ordinary.encode() in result.stdout_bytes


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
    monkeypatch.setattr(
        show,
        "show_resource",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(error),
    )

    result = CliRunner().invoke(
        app,
        ["resource", "show", "secret/missing", "--output", "json"],
    )

    assert result.exit_code != 0
    assert result.stdout_bytes == b""
    assert result.exception is error


def test_missing_ssh_keys_do_not_block_resource_show(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A config whose only defect is a nonexistent operator SSH key path
    (the sample config's placeholder, before ``agw config init`` writes a
    real one) must not stop `resource show` from rendering one resource's
    facts and readiness: it needs no operator identity (the per-resource
    diagnostics `show_resource` gathers route through
    ``doctor.checks_for_resource``, which never reads ``config.operator``).
    """
    from agentworks import config

    write_manifests(tmp_path, ManifestDoc("secret", "npm-token", description="npm registry token"))
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        dedent(f"""\
        [operator]
        ssh_public_key = "{(tmp_path / "id.pub").as_posix()}"
        ssh_private_key = "{(tmp_path / "id").as_posix()}"

        [secret_config]
        sources = ["env-var"]
        """)
    )
    assert not (tmp_path / "id.pub").exists()
    assert not (tmp_path / "id").exists()
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)

    result = CliRunner().invoke(app, ["resource", "show", "secret/npm-token"])

    assert result.exit_code == 0, result.output
    assert "npm-token" in result.output
    # Readiness actually got computed and rendered, not skipped or blank.
    assert "is_ready:" in result.output
    assert "is_available:" in result.output


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
