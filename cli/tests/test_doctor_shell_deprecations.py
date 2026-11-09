"""Read-only health checks for temporary Shell command placeholders."""

from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from agentworks import output
from agentworks.db import LATEST_VERSION, Database, SessionMode, VersionedPayload
from agentworks.doctor import HealthGroup, Status, _check_config
from agentworks.doctor_state import append_shell_template_deprecation_checks
from agentworks.env import EnvEntry
from agentworks.origin import Origin
from agentworks.schema import CapabilityBlock
from agentworks.sessions.template import SessionTemplate
from tests.conftest import ManifestDoc, registry_with_shell, write_cfg

if TYPE_CHECKING:
    from agentworks.resources.registry import Registry


def _registry(*templates: SessionTemplate) -> Registry:
    registry = registry_with_shell()
    for template in templates:
        registry.add("session-template", template.name, template, Origin.built_in(source="test"))
    return registry


def _shell(name: str, **config: object) -> SessionTemplate:
    return SessionTemplate(name=name, harness_integration=CapabilityBlock.of("shell", **config))


def test_doctor_reports_only_winning_shell_command_fields() -> None:
    parent = _shell("parent", command="{{session_name}}", resume_command="{{workspace_name}}")
    child = SessionTemplate(
        name="child",
        inherits=["parent"],
        harness_integration=CapabilityBlock.of("shell", command="${agw:session_name}"),
    )
    modern = SessionTemplate(
        name="modern",
        inherits=["parent"],
        harness_integration=CapabilityBlock.of(
            "shell", command="${agw:session_name}", resume_command="${agw:workspace_name}"
        ),
    )
    unrelated = SessionTemplate(
        name="unrelated",
        harness_integration=CapabilityBlock.of("claude-code", append_system_prompt="{{session_name}}"),
    )
    env_only = SessionTemplate(
        name="env-only",
        harness_integration=CapabilityBlock.of("shell", command="echo '${HOME} {{.Names}} {{end}}'"),
        env={"LITERAL": EnvEntry.model_validate("{{session_name}}")},
    )
    registry = _registry(parent, child, modern, unrelated, env_only)
    group = HealthGroup("Configuration")

    append_shell_template_deprecation_checks(group, registry)

    assert {(check.name, check.status) for check in group.checks} == {
        ("session-template/parent.command", Status.WARN),
        ("session-template/parent.resume_command", Status.WARN),
        ("session-template/child.resume_command", Status.WARN),
    }


def test_doctor_reads_stored_session_overlays_without_mutating_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentworks.db import DB_PATH

    registry = _registry(_shell("default", command="${agw:session_name}"))
    with closing(Database()) as database:
        database.insert_vm("box", site="lima-local", hostname="box")
        database.insert_workspace("work", "/work", "box", "work")
        database.insert_session("old", "work", "default", SessionMode.ADMIN)
        database.insert_session("modern", "work", "default", SessionMode.ADMIN)
        overlay = VersionedPayload(
            1,
            {
                "harness_integration": {
                    "name": "shell",
                    "command": "private-command {{session_name}}",
                    "resume_command": "{{workspace_name}}",
                }
            },
        )
        database.instance_state.put_desired_overlay("session", "old", overlay)
        original_overlay = database.instance_state.get_desired_overlay("session", "old")
        original_rows = database.list_sessions()
        original_bytes = DB_PATH.read_bytes()
        opened: list[bool] = []
        real_init = Database.__init__

        def observe_open(
            self: Database,
            path: Path | None = None,
            *,
            read_only: bool = False,
            timeout: float | None = None,
        ) -> None:
            opened.append(read_only)
            real_init(self, path, read_only=read_only, timeout=timeout)

        monkeypatch.setattr(Database, "__init__", observe_open)
        group = HealthGroup("Configuration")

        append_shell_template_deprecation_checks(group, registry)

        assert [(check.name, check.status) for check in group.checks] == [
            ("session/old.command", Status.WARN),
            ("session/old.resume_command", Status.WARN),
        ]
        assert opened == [True]
        assert DB_PATH.read_bytes() == original_bytes
        assert database.list_sessions() == original_rows
        assert database.instance_state.get_desired_overlay("session", "old") == original_overlay
        assert "private-command" not in json.dumps([check.__dict__ for check in group.checks], default=str)


def test_doctor_deprecation_health_is_not_suppressed() -> None:
    output.set_suppress_deprecations(True)
    group = HealthGroup("Configuration")

    append_shell_template_deprecation_checks(group, _registry(_shell("old", command="{{session_name}}")))

    assert [(check.name, check.status) for check in group.checks] == [("session-template/old.command", Status.WARN)]


def test_configuration_group_includes_shell_deprecation_health(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = write_cfg(
        tmp_path,
        ManifestDoc("vm-template", "default"),
        ManifestDoc("admin-template", "default"),
        ManifestDoc(
            "session-template",
            "old",
            {"harness_integration": {"name": "shell", "command": "{{session_name}}"}},
        ),
    )
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", config_path)
    output.set_suppress_deprecations(True)

    group, config, registry = _check_config()

    assert config is not None
    assert registry is not None
    assert [(check.name, check.status) for check in group.checks if check.name.startswith("session-template/")] == [
        ("session-template/old.command", Status.WARN)
    ]


def test_missing_database_is_not_created() -> None:
    from agentworks.db import DB_PATH

    assert not DB_PATH.exists()
    group = HealthGroup("Configuration")

    append_shell_template_deprecation_checks(group, _registry(_shell("old", command="{{session_name}}")))

    assert not DB_PATH.exists()
    assert [(check.name, check.status) for check in group.checks] == [("session-template/old.command", Status.WARN)]


def test_stale_database_preserves_declared_checks_and_reports_live_coverage() -> None:
    from agentworks.db import DB_PATH

    with closing(Database()) as database:
        database._conn.execute("UPDATE schema_version SET version = ?", (LATEST_VERSION - 1,))
        database._conn.commit()
    original_bytes = DB_PATH.read_bytes()
    group = HealthGroup("Configuration")

    append_shell_template_deprecation_checks(group, _registry(_shell("old", command="{{session_name}}")))

    assert [(check.name, check.status) for check in group.checks] == [
        ("session-template/old.command", Status.WARN),
        ("Session template compatibility coverage", Status.INFO),
    ]
    assert DB_PATH.read_bytes() == original_bytes


def test_malformed_database_preserves_declared_checks_and_reports_live_failure() -> None:
    from agentworks.db import DB_PATH

    DB_PATH.write_bytes(b"malformed fixture database")
    group = HealthGroup("Configuration")

    append_shell_template_deprecation_checks(group, _registry(_shell("old", command="{{session_name}}")))

    assert [(check.name, check.status) for check in group.checks] == [
        ("session-template/old.command", Status.WARN),
        ("Session template compatibility coverage", Status.FAIL),
    ]
    assert DB_PATH.read_bytes() == b"malformed fixture database"


def test_unreadable_overlay_preserves_other_session_health_checks() -> None:
    registry = _registry(_shell("default", command="${agw:session_name}"))
    with closing(Database()) as database:
        database.insert_vm("box", site="lima-local", hostname="box")
        database.insert_workspace("work", "/work", "box", "work")
        database.insert_session("broken", "work", "default", SessionMode.ADMIN)
        database.insert_session("old", "work", "default", SessionMode.ADMIN)
        database.instance_state.put_desired_overlay("session", "broken", VersionedPayload(999, {"private": "input"}))
        database.instance_state.put_desired_overlay(
            "session",
            "old",
            VersionedPayload(1, {"harness_integration": {"name": "shell", "command": "{{session_name}}"}}),
        )
        group = HealthGroup("Configuration")

        append_shell_template_deprecation_checks(group, registry)

        assert {(check.name, check.status) for check in group.checks} == {
            ("session/broken", Status.FAIL),
            ("session/old.command", Status.WARN),
        }
