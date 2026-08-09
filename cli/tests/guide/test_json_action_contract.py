"""Guide action contracts consume the operational JSON v1 envelope."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest
from click.testing import Result
from typer.testing import CliRunner

from agentworks.cli import app
from agentworks.doctor import HealthGroup, HealthReport
from agentworks.guide import ActionList, GuideAction, onboarding_actions
from agentworks.guide.contributions import guide_contributions
from agentworks.origin import Origin
from agentworks.resources.inspect import ResourceListing, ResourceSummary

_COVERED_OPERATIONS = {
    ("agw", "resource", "list"): "resource.list",
    ("agw", "resource", "kinds"): "resource.kinds",
    ("agw", "resource", "describe"): "resource.describe",
    ("agw", "vm", "list"): "vm.list",
    ("agw", "vm", "describe"): "vm.describe",
    ("agw", "workspace", "list"): "workspace.list",
    ("agw", "workspace", "describe"): "workspace.describe",
    ("agw", "agent", "list"): "agent.list",
    ("agw", "agent", "describe"): "agent.describe",
    ("agw", "session", "list"): "session.list",
    ("agw", "session", "describe"): "session.describe",
    ("agw", "console", "list"): "console.list",
    ("agw", "console", "describe"): "console.describe",
    ("agw", "secret", "list"): "secret.list",
    ("agw", "secret", "describe"): "secret.describe",
}


def _migration_actions() -> dict[str, GuideAction]:
    topic = next(topic for topic in guide_contributions() if topic.topic == "concept-migration")
    block = next(block for block in topic.blocks if isinstance(block, ActionList))
    return {str(action.id): action for action in block.actions}


def _covered_command(command: tuple[str, ...] | None) -> str | None:
    if command is None:
        return None
    if command[:2] == ("agw", "doctor"):
        return "doctor"
    return _COVERED_OPERATIONS.get(command[:3])


def _parse_exact_v1(result: Result, expected_command: str, *, exit_code: int = 0) -> dict[str, object]:
    assert result.exit_code == exit_code, result.output
    document_bytes = result.stdout_bytes
    assert document_bytes.endswith(b"\n")
    assert b"\n" not in document_bytes[:-1]
    document = cast("dict[str, object]", json.loads(document_bytes))
    assert type(document) is dict
    assert list(document) == ["schema_version", "command", "data"]
    assert type(document["schema_version"]) is int
    assert document["schema_version"] == 1
    assert document["command"] == expected_command
    assert type(document["data"]) is dict
    return document


def _assert_action_requires_exact_v1(action: GuideAction, command: str) -> None:
    assert action.command is not None
    assert action.command[-2:] == ("--output", "json")
    assert "Before recording VERIFIED, parse exactly one JSON document" in action.expected_state
    assert "schema_version is the integer 1" in action.expected_state
    assert f"command is exactly {command}" in action.expected_state
    assert "data is an object" in action.expected_state


def test_every_covered_guide_action_requests_and_requires_exact_json_v1() -> None:
    actions = onboarding_actions() + tuple(
        action
        for topic in guide_contributions()
        for block in topic.blocks
        if isinstance(block, ActionList)
        for action in block.actions
    )
    covered = tuple(
        (action, command) for action in actions if (command := _covered_command(action.command)) is not None
    )

    assert [str(action.id) for action, _command in covered] == [
        "run-doctor",
        "validate-manifest-set",
        "compare-operator-inventory",
        "finish-doctor",
    ]

    for action, command in covered:
        _assert_action_requires_exact_v1(action, command)


def test_guide_resource_inventory_action_emits_parseable_resource_list_v1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentworks import bootstrap, config
    from agentworks.cli import _helpers
    from agentworks.resources import inspect

    marker = "operator-resource-marker"
    listing = ResourceListing(
        rows=(
            ResourceSummary(
                kind="vm-template",
                name="worker",
                origin=Origin.operator_declared(file=Path("resources/worker.yaml"), line=7),
                reference_count=0,
                used_by_count=None,
                description=marker,
            ),
        ),
        operator_count=1,
        auto_count=0,
        code_count=0,
        plugin_count=0,
    )

    def list_resources(*_args: object, **kwargs: object) -> ResourceListing:
        assert kwargs["origin_filter"] == "operator"
        return listing

    monkeypatch.setattr(config, "load_config", lambda **_kwargs: object())
    monkeypatch.setattr(bootstrap, "load_request_registry", lambda _config, **_kwargs: object())
    monkeypatch.setattr(_helpers, "get_db", lambda: None)
    monkeypatch.setattr(inspect, "list_resources", list_resources)
    action = _migration_actions()["compare-operator-inventory"]
    assert action.command is not None

    result = CliRunner().invoke(app, list(action.command[1:]))

    document = _parse_exact_v1(result, "resource.list")
    data = cast("dict[str, object]", document["data"])
    resources = cast("list[dict[str, object]]", data["resources"])
    assert resources[0]["name"] == "worker"
    assert resources[0]["description"] == marker


def test_guide_doctor_actions_emit_one_parseable_report_without_sensitive_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentworks import doctor

    sensitive = "secret-value-must-not-be-echoed"
    report = HealthReport()
    group = HealthGroup("Configuration")
    group.fail("Config", sensitive, hint=sensitive)
    report.groups.append(group)
    monkeypatch.setattr(doctor, "run_checks", lambda **_kwargs: report)
    action = onboarding_actions()[0]
    assert action.command is not None

    result = CliRunner().invoke(app, list(action.command[1:]))

    document = _parse_exact_v1(result, "doctor", exit_code=1)
    assert sensitive not in result.stdout
    data = cast("dict[str, object]", document["data"])
    assert data["counts"] == {"ok": 0, "info": 0, "warn": 0, "fail": 1}
