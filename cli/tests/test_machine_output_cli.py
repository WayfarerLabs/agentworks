"""End-to-end JSON v1 checks for resource, secret, and doctor commands."""

from __future__ import annotations

import json
import sys
from typing import cast

import pytest
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


def _assert_human_baseline(command: list[str], expected_stdout: bytes, *, exit_code: int = 0) -> None:
    """Pin existing no-color human bytes before exercising JSON beside them."""
    default = CliRunner().invoke(app, ["--non-interactive", *command])
    explicit_human = CliRunner().invoke(app, ["--non-interactive", *command, "--output", "human"])

    assert default.exit_code == explicit_human.exit_code == exit_code
    assert default.stdout_bytes == expected_stdout
    assert explicit_human.stdout_bytes == expected_stdout
    assert default.stderr_bytes == explicit_human.stderr_bytes == b""


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
    _assert_human_baseline(
        ["resource", "list"],
        b"1 resource (1 built-in)\n\n"
        b"KIND    NAME   ORIGIN                      REFS  USED BY  DESCRIPTION           \n"
        b"------  -----  --------------------------  ----  -------  ----------------------\n"
        b"secret  token  built-in (agentworks.test)  2     -        (not ready) test token\n",
    )

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
    _assert_human_baseline(
        ["secret", "list"],
        b"1 secret (1 operator-declared)\n\n"
        b"NAME   DESCRIPTION  first  second       \n"
        b"-----  -----------  -----  -------------\n"
        b"token  test token   TOKEN  won't attempt\n",
    )

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
    _assert_human_baseline(
        ["resource", "kinds"],
        b"KIND    CATEGORY    RESOURCES  DESCRIPTION\nsecret  declarable  1          secret configuration\n",
    )
    _assert_human_baseline(
        ["resource", "describe", "secret/token"],
        b"Resource: secret/token\n"
        b"  Description: test token\n"
        b"  Origin: unknown\n"
        b"  Disabled: disabled\n\n"
        b"Referenced by:\n"
        b"  - vm-template/default: first reference\n\n"
        b"Used by (per current config):\n"
        b"  - session/one\n",
    )

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
    _assert_human_baseline(
        ["secret", "describe", "token"],
        b"Secret: token\n"
        b"  Kind: secret\n"
        b"  Description: test token\n"
        b"  Origin: unknown\n\n"
        b"Referenced by:\n"
        b"  (none recorded)\n\n"
        b"Backend mappings:\n"
        b"  - first: TOKEN\n"
        b"  - second: (prompt at resolution time) (not ready: backend unavailable)\n\n"
        b"Resolution preview:\n"
        b"  - skipped second: not ready: backend unavailable\n"
        b"  would resolve via first\n",
    )

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
    _assert_human_baseline(
        ["doctor"],
        b"Checking environment...\n\n"
        b"Configuration:\n"
        b"  [FAIL] Config: missing config\n"
        b"         hint: run agw config init\n\n"
        b"Results: 0 ok, 0 info, 0 warn, 1 fail\n",
        exit_code=1,
    )


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
    assert invalid.stdout_bytes == b""
    assert incompatible.stdout_bytes == b""
    assert invalid.stderr_bytes
    assert incompatible.stderr_bytes
    assert calls == 0


def test_unknown_name_json_uses_existing_stderr_error_route(monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    from agentworks import bootstrap, config
    from agentworks import cli as cli_mod
    from agentworks.cli.commands import resource
    from agentworks.errors import NotFoundError
    from agentworks.resources import inspect

    monkeypatch.setattr(config, "load_config", lambda **_kwargs: object())
    monkeypatch.setattr(bootstrap, "load_request_registry", lambda _config, **_kwargs: object())
    monkeypatch.setattr(resource, "get_db", lambda: None)

    def missing_resource(*_args: object, **_kwargs: object) -> ResourceDescription:
        raise NotFoundError("resource secret/missing does not exist")

    monkeypatch.setattr(inspect, "describe_resource", missing_resource)
    monkeypatch.setattr(sys, "argv", ["agw", "resource", "describe", "secret/missing", "--output", "json"])

    with pytest.raises(SystemExit) as exit_info:
        cli_mod.main()

    captured = capsys.readouterr()
    assert exit_info.value.code == 1
    assert captured.out == ""
    assert "Error: resource secret/missing does not exist" in captured.err


def test_config_failure_json_writes_no_stdout_before_service_work(
    monkeypatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from agentworks import cli as cli_mod
    from agentworks import config
    from agentworks.errors import ConfigError
    from agentworks.secrets import inspect

    service_calls = 0

    def fail_load_config(**_kwargs: object) -> object:
        raise ConfigError("configuration is invalid")

    def unexpected_service(*_args: object) -> SecretTable:
        nonlocal service_calls
        service_calls += 1
        raise AssertionError("must not build secret facts")

    monkeypatch.setattr(config, "load_config", fail_load_config)
    monkeypatch.setattr(inspect, "build_secret_table", unexpected_service)
    monkeypatch.setattr(sys, "argv", ["agw", "secret", "list", "--output", "json"])

    with pytest.raises(SystemExit) as exit_info:
        cli_mod.main()

    captured = capsys.readouterr()
    assert exit_info.value.code == 1
    assert captured.out == ""
    assert "Configuration error: configuration is invalid" in captured.err
    assert service_calls == 0
