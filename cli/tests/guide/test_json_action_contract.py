"""Guide action contracts consume the operational JSON v1 envelope."""

from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent
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
from tests.conftest import ManifestDoc, write_manifests

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


def _write_doctor_config(tmp_path: Path, settings: str) -> Path:
    public_key = tmp_path / "id.pub"
    private_key = tmp_path / "id"
    public_key.write_text("ssh-ed25519 AAAA...")
    private_key.write_text("-----BEGIN OPENSSH PRIVATE KEY-----")
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        dedent(f"""\
        [operator]
        ssh_public_key = "{public_key.as_posix()}"
        ssh_private_key = "{private_key.as_posix()}"

        """)
        + dedent(settings)
    )
    return config_path


def _configure_config_only_doctor(monkeypatch: pytest.MonkeyPatch, config_path: Path) -> None:
    from agentworks import config, doctor

    monkeypatch.setattr(config, "CONFIG_PATH", config_path)

    def config_checks(
        *,
        completion_version: str | None = None,
        machine_safe_config_load: bool = False,
    ) -> HealthReport:
        del completion_version
        group, _config, _registry = doctor._check_config(raise_errors=machine_safe_config_load)
        return HealthReport(groups=[group])

    monkeypatch.setattr(doctor, "run_checks", config_checks)


def _configuration_checks(document: dict[str, object]) -> list[dict[str, object]]:
    data = cast("dict[str, object]", document["data"])
    groups = cast("list[dict[str, object]]", data["groups"])
    configuration = next(group for group in groups if group["name"] == "Configuration")
    return cast("list[dict[str, object]]", configuration["checks"])


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


def _assert_action_requires_exact_v1(action: GuideAction, operation: tuple[str, ...], command: str) -> None:
    assert operation[-2:] == ("--output", "json")
    assert "Before recording VERIFIED" in action.expected_state
    assert "one JSON document" in action.expected_state or "one document" in action.expected_state
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
        (action, field, operation, command)
        for action in actions
        for field, operation in (("command", action.command), ("verification", action.verification))
        if (command := _covered_command(operation)) is not None
    )

    assert [(str(action.id), field, command) for action, field, _operation, command in covered] == [
        ("run-doctor", "command", "doctor"),
        ("validate-manifest-set", "command", "doctor"),
        ("validate-manifest-set", "verification", "doctor"),
        ("compare-operator-inventory", "command", "resource.list"),
        ("finish-doctor", "command", "doctor"),
    ]

    validation = _migration_actions()["validate-manifest-set"]
    assert validation.command == ("agw", "doctor")
    assert validation.verification == ("agw", "doctor", "--output", "json")
    for action, field, operation, command in covered:
        if str(action.id) == "validate-manifest-set" and field == "command":
            continue
        assert operation is not None
        _assert_action_requires_exact_v1(action, operation, command)


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


def test_validate_manifest_action_keeps_human_detail_and_verifies_redacted_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _write_doctor_config(
        tmp_path,
        """
        [vm_templates.default]

        [admin.config]
        shell = "zsh"
        """,
    )
    sensitive = "sensitive-invalid-manifest-marker"
    resources = tmp_path / "resources"
    resources.mkdir()
    (resources / f"{sensitive}.yaml").write_text("apiVersion: [\n")
    _configure_config_only_doctor(monkeypatch, config_path)
    action = _migration_actions()["validate-manifest-set"]
    assert action.command is not None
    assert action.verification is not None

    human = CliRunner().invoke(app, list(action.command[1:]))
    machine = CliRunner().invoke(app, list(action.verification[1:]))

    assert human.exit_code == 1
    assert human.stderr == ""
    assert "[FAIL] Config:" in human.stdout
    assert "config.toml declares resources" in human.stdout
    assert "config.toml no longer supports" in human.stdout
    assert "settings only now" in human.stdout
    assert "[vm_templates.*]" in human.stdout
    assert "[admin.config]" in human.stdout
    assert "[FAIL] Manifest:" in human.stdout
    assert sensitive in human.stdout
    document = _parse_exact_v1(machine, "doctor", exit_code=1)
    assert machine.stderr == ""
    assert sensitive not in machine.stdout
    data = cast("dict[str, object]", document["data"])
    checks = _configuration_checks(document)
    assert next(check for check in checks if check["name"] == "Config file")["status"] == "ok"
    assert next(check for check in checks if check["name"] == "Config")["status"] == "fail"
    manifest = next(check for check in checks if check["name"] == "Manifest")
    assert manifest == {
        "name": "Manifest",
        "status": "fail",
        "message": "resource manifests did not load",
        "hint": None,
    }
    assert cast("dict[str, int]", data["counts"])["fail"] == 2


def test_validate_manifest_action_rejects_registry_failure_after_decode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _write_doctor_config(
        tmp_path,
        """
        [vm_templates.default]

        [admin.config]
        shell = "zsh"
        """,
    )
    missing = "unknown-parent-marker"
    write_manifests(tmp_path, ManifestDoc("vm-template", "child", {"inherits": [missing]}))
    _configure_config_only_doctor(monkeypatch, config_path)
    action = _migration_actions()["validate-manifest-set"]
    assert action.command is not None
    assert action.verification is not None

    human = CliRunner().invoke(app, list(action.command[1:]))
    machine = CliRunner().invoke(app, list(action.verification[1:]))

    assert human.exit_code == 1
    assert "[FAIL] Config:" in human.stdout
    assert "config.toml declares resources" in human.stdout
    assert "settings only now" in human.stdout
    assert "[vm_templates.*]" in human.stdout
    assert "[admin.config]" in human.stdout
    assert "[FAIL] Resource registry:" in human.stdout
    assert missing in human.stdout
    document = _parse_exact_v1(machine, "doctor", exit_code=1)
    assert missing not in machine.stdout
    checks = _configuration_checks(document)
    assert not any(check["name"] == "Manifest" and check["status"] in {"warn", "fail"} for check in checks)
    registry = next(check for check in checks if check["name"] == "Resource registry")
    assert registry == {
        "name": "Resource registry",
        "status": "fail",
        "message": "resource registry did not build",
        "hint": None,
    }
    assert "no check named Manifest or Resource registry" in action.expected_state


def test_validate_manifest_action_rejects_settings_error_before_manifest_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _write_doctor_config(tmp_path, "[unexpected-settings]\nvalue = true\n")
    _configure_config_only_doctor(monkeypatch, config_path)
    action = _migration_actions()["validate-manifest-set"]
    assert action.command is not None
    assert action.verification is not None

    human = CliRunner().invoke(app, list(action.command[1:]))
    machine = CliRunner().invoke(app, list(action.verification[1:]))

    assert human.exit_code == 1
    assert "[FAIL] Config:" in human.stdout
    assert "unexpected-settings" in human.stdout
    assert "config.toml declares resources" not in human.stdout
    document = _parse_exact_v1(machine, "doctor", exit_code=1)
    checks = _configuration_checks(document)
    assert [check["name"] for check in checks] == ["Config file", "Config"]
    assert checks[1] == {
        "name": "Config",
        "status": "fail",
        "message": "configuration did not load",
        "hint": None,
    }
    assert "Config failure must be the expected migration hard error" in action.expected_state


def test_validate_manifest_action_reports_stable_retained_checkpoint_structure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _write_doctor_config(
        tmp_path,
        """
        [vm_templates.default]

        [admin.config]
        shell = "zsh"
        """,
    )
    _configure_config_only_doctor(monkeypatch, config_path)
    action = _migration_actions()["validate-manifest-set"]
    assert action.command is not None
    assert action.verification is not None

    human = CliRunner().invoke(app, list(action.command[1:]))
    machine = CliRunner().invoke(app, list(action.verification[1:]))

    assert human.exit_code == 1
    assert "config.toml declares resources" in human.stdout
    document = _parse_exact_v1(machine, "doctor", exit_code=1)
    checks = _configuration_checks(document)
    assert next(check for check in checks if check["name"] == "Config file")["status"] == "ok"
    assert next(check for check in checks if check["name"] == "Config")["status"] == "fail"
    assert not any(
        check["name"] in {"Manifest", "Resource registry"} and check["status"] in {"warn", "fail"} for check in checks
    )


def test_validate_manifest_action_rejects_config_becoming_clean_between_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _write_doctor_config(
        tmp_path,
        """
        [vm_templates.default]

        [admin.config]
        shell = "zsh"
        """,
    )
    _configure_config_only_doctor(monkeypatch, config_path)
    action = _migration_actions()["validate-manifest-set"]
    assert action.command is not None
    assert action.verification is not None

    human = CliRunner().invoke(app, list(action.command[1:]))
    _write_doctor_config(tmp_path, "")
    machine = CliRunner().invoke(app, list(action.verification[1:]))

    assert human.exit_code == 1
    assert "config.toml declares resources" in human.stdout
    document = _parse_exact_v1(machine, "doctor", exit_code=0)
    checks = _configuration_checks(document)
    assert next(check for check in checks if check["name"] == "Config file")["status"] == "ok"
    assert any(check["name"] == "Config is valid" and check["status"] == "ok" for check in checks)
    assert not any(check["name"] == "Config" and check["status"] == "fail" for check in checks)
    assert "Both the human command and JSON verification must exit 1" in action.expected_state


def test_validate_manifest_action_rejects_config_disappearing_between_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _write_doctor_config(
        tmp_path,
        """
        [vm_templates.default]

        [admin.config]
        shell = "zsh"
        """,
    )
    _configure_config_only_doctor(monkeypatch, config_path)
    action = _migration_actions()["validate-manifest-set"]
    assert action.command is not None
    assert action.verification is not None

    human = CliRunner().invoke(app, list(action.command[1:]))
    config_path.unlink()
    machine = CliRunner().invoke(app, list(action.verification[1:]))

    assert human.exit_code == 1
    assert "config.toml declares resources" in human.stdout
    document = _parse_exact_v1(machine, "doctor", exit_code=1)
    checks = _configuration_checks(document)
    assert checks == [
        {
            "name": "Config file",
            "status": "fail",
            "message": "configuration file is not available",
            "hint": "run agw config init",
        }
    ]
    assert "Config file with status ok" in action.expected_state
    assert "Config with status fail" in action.expected_state


def test_finish_doctor_requires_zero_failures_and_exit_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentworks import doctor

    report = HealthReport()
    group = HealthGroup("Configuration")
    group.ok("Config is valid")
    report.groups.append(group)
    monkeypatch.setattr(doctor, "run_checks", lambda **_kwargs: report)
    action = _migration_actions()["finish-doctor"]
    assert action.command is not None

    result = CliRunner().invoke(app, list(action.command[1:]))

    document = _parse_exact_v1(result, "doctor", exit_code=0)
    data = cast("dict[str, object]", document["data"])
    assert cast("dict[str, int]", data["counts"])["fail"] == 0
    assert "data.counts.fail equals 0" in action.expected_state
    assert "command exits 0" in action.expected_state
