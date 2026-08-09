"""End-to-end JSON v1 checks for resource, secret, and doctor commands."""

from __future__ import annotations

import json
from typing import cast

from click.testing import Result
from typer.testing import CliRunner

from agentworks.cli import app
from agentworks.doctor import HealthGroup, HealthReport
from agentworks.origin import Origin
from agentworks.resources.inspect import KindRow, ResourceDescription, ResourceListing, ResourceSummary
from agentworks.resources.kind import InstanceRef
from agentworks.resources.reference import ReferenceEntry
from agentworks.secrets.inspect import (
    BackendMapping,
    ResolutionPreview,
    SecretCell,
    SecretDescription,
    SecretRow,
    SecretTable,
)


def _json_document(result: Result) -> dict[str, object]:
    stdout_bytes = result.stdout_bytes
    assert stdout_bytes.endswith(b"\n")
    assert b"\x1b" not in stdout_bytes
    assert b"\x7f" not in stdout_bytes
    return cast("dict[str, object]", json.loads(stdout_bytes))


def test_resource_list_json_is_parseable_deterministic_and_uses_safe_fields(monkeypatch) -> None:
    from agentworks import bootstrap, config
    from agentworks.cli import _helpers
    from agentworks.resources import inspect

    listing = ResourceListing(
        rows=(
            ResourceSummary(
                kind="secret",
                name="token",
                origin=Origin.built_in(source="agentworks.test"),
                reference_count=2,
                used_by_count=None,
                description="test token",
                not_ready_reason="backend unavailable",
                disabled=False,
            ),
        ),
        operator_count=0,
        auto_count=0,
        code_count=1,
        plugin_count=0,
    )
    monkeypatch.setattr(config, "load_config", lambda **_kwargs: object())
    monkeypatch.setattr(bootstrap, "load_request_registry", lambda _config, **_kwargs: object())
    monkeypatch.setattr(_helpers, "get_db", lambda: None)
    monkeypatch.setattr(inspect, "list_resources", lambda *_args, **_kwargs: listing)

    first = CliRunner().invoke(app, ["resource", "list", "--output", "json"])
    second = CliRunner().invoke(app, ["resource", "list", "--output", "json"])

    assert first.exit_code == 0, first.output
    assert first.stdout_bytes == second.stdout_bytes
    document = _json_document(first)
    assert list(document) == ["schema_version", "command", "data"]
    assert document["command"] == "resource.list"
    data = document["data"]
    assert isinstance(data, dict)
    assert list(data) == ["resources", "counts"]
    resource = data["resources"][0]
    assert list(resource) == [
        "kind",
        "name",
        "origin",
        "reference_count",
        "used_by_count",
        "description",
        "not_ready_reason",
        "disabled",
    ]
    assert resource["used_by_count"] is None


def test_secret_list_json_preserves_backend_precedence_without_values(monkeypatch) -> None:
    from agentworks import bootstrap, config
    from agentworks.secrets import inspect

    table = SecretTable(
        backends=("first", "second"),
        rows=(
            SecretRow(
                name="token",
                description="test token",
                cells=(
                    SecretCell("first", True, "TOKEN", None),
                    SecretCell("second", False, None, "not configured"),
                ),
            ),
        ),
        operator_count=1,
        auto_count=0,
    )
    monkeypatch.setattr(config, "load_config", lambda **_kwargs: object())
    monkeypatch.setattr(bootstrap, "load_request_registry", lambda _config, **_kwargs: object())
    monkeypatch.setattr(inspect, "build_secret_table", lambda *_args: table)

    result = CliRunner().invoke(app, ["secret", "list", "--output", "json"])

    assert result.exit_code == 0, result.output
    data = _json_document(result)["data"]
    assert isinstance(data, dict)
    assert data["backends"] == ["first", "second"]
    assert [item["backend"] for item in data["secrets"][0]["backends"]] == ["first", "second"]
    assert "value" not in result.stdout


def test_resource_kinds_and_describe_json_use_closed_data_shapes(monkeypatch) -> None:
    from agentworks import bootstrap, config
    from agentworks.cli.commands import resource
    from agentworks.resources import inspect

    description = ResourceDescription(
        kind="secret",
        name="token",
        origin=None,
        description="test token",
        references=(
            ReferenceEntry(("vm-template", "default"), "first reference"),
            ReferenceEntry(("vm-template", "default"), "first reference"),
        ),
        used_by=(InstanceRef("session", "one"),),
        not_ready_reason=None,
        disabled_reason="disabled",
    )
    monkeypatch.setattr(config, "load_config", lambda **_kwargs: object())
    monkeypatch.setattr(bootstrap, "load_request_registry", lambda _config, **_kwargs: object())
    monkeypatch.setattr(resource, "get_db", lambda: None)
    monkeypatch.setattr(
        inspect,
        "list_kinds",
        lambda _registry: [KindRow("secret", "declarable", 1, "secret configuration")],
    )
    monkeypatch.setattr(inspect, "describe_resource", lambda *_args, **_kwargs: description)

    kinds = CliRunner().invoke(app, ["resource", "kinds", "--output", "json"])
    describe = CliRunner().invoke(app, ["resource", "describe", "secret/token", "--output", "json"])

    assert kinds.exit_code == 0, kinds.output
    assert _json_document(kinds)["data"] == {
        "kinds": [
            {
                "kind": "secret",
                "category": "declarable",
                "resource_count": 1,
                "description": "secret configuration",
            },
        ],
    }
    assert describe.exit_code == 0, describe.output
    resource_data = _json_document(describe)["data"]
    assert isinstance(resource_data, dict)
    described = resource_data["resource"]
    assert isinstance(described, dict)
    assert described["origin"] is None
    assert described["disabled_reason"] == "disabled"
    assert len(described["references"]) == 2


def test_secret_describe_json_preserves_nulls_and_backend_order(monkeypatch) -> None:
    from agentworks import bootstrap, config
    from agentworks.cli.commands import secret
    from agentworks.secrets import inspect

    description = SecretDescription(
        name="token",
        kind="secret",
        origin=None,
        description="test token",
        hint=None,
        references=(),
        used_by=None,
        backend_mappings=(
            BackendMapping("first", True, "TOKEN", None),
            BackendMapping("second", True, None, "backend unavailable"),
        ),
        resolution=ResolutionPreview("first", True, (("second", "backend unavailable"),)),
    )
    monkeypatch.setattr(config, "load_config", lambda **_kwargs: object())
    monkeypatch.setattr(bootstrap, "load_request_registry", lambda _config, **_kwargs: object())
    monkeypatch.setattr(secret, "get_db", lambda: None)
    monkeypatch.setattr(inspect, "describe_secret", lambda *_args, **_kwargs: description)

    result = CliRunner().invoke(app, ["secret", "describe", "token", "--output", "json"])

    assert result.exit_code == 0, result.output
    data = _json_document(result)["data"]
    assert isinstance(data, dict)
    described = data["secret"]
    assert isinstance(described, dict)
    assert described["origin"] is None
    assert described["hint"] is None
    assert described["used_by"] is None
    assert [mapping["backend"] for mapping in described["backend_mappings"]] == ["first", "second"]


def test_doctor_json_writes_complete_failing_report_before_exit(monkeypatch) -> None:
    from agentworks import doctor

    report = HealthReport()
    group = HealthGroup("Configuration")
    group.fail("Config", "missing config", hint="run agw config init")
    report.groups.append(group)
    monkeypatch.setattr(doctor, "run_checks", lambda **_kwargs: report)

    result = CliRunner().invoke(app, ["doctor", "--output", "json"])

    assert result.exit_code == 1
    data = _json_document(result)["data"]
    assert isinstance(data, dict)
    assert data["counts"] == {"ok": 0, "info": 0, "warn": 0, "fail": 1}
    assert data["groups"][0]["checks"][0]["status"] == "fail"

    human_default = CliRunner().invoke(app, ["doctor"])
    human_explicit = CliRunner().invoke(app, ["doctor", "--output", "human"])
    assert human_default.exit_code == human_explicit.exit_code == 1
    assert human_default.stdout_bytes == human_explicit.stdout_bytes
    assert human_default.stderr_bytes == human_explicit.stderr_bytes


def test_invalid_output_and_names_only_json_fail_before_config_or_service_work(monkeypatch) -> None:
    from agentworks import config

    calls = 0

    def fail_load_config(**_kwargs: object) -> object:
        nonlocal calls
        calls += 1
        raise AssertionError("must not load config")

    monkeypatch.setattr(config, "load_config", fail_load_config)

    invalid = CliRunner().invoke(app, ["resource", "list", "--output", "yaml"])
    incompatible = CliRunner().invoke(app, ["secret", "list", "--names-only", "--output", "json"])

    assert invalid.exit_code != 0
    assert incompatible.exit_code != 0
    assert calls == 0
