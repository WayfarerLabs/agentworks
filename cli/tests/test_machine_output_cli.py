"""End-to-end JSON v1 checks for resource, secret, and doctor commands."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import cast

import pytest
from click.testing import Result
from typer.testing import CliRunner

from agentworks.capabilities.secret_backend import PreviewAvailable
from agentworks.cli import app
from agentworks.doctor import HealthGroup, HealthReport
from agentworks.origin import Origin
from agentworks.resources.inspect import KindRow, ResourceListing, ResourceSummary
from agentworks.secrets.inspect import (
    SecretDescription,
    SecretRow,
    SecretSourceCell,
    SecretTable,
    SourceMapping,
    StaticResolution,
    StaticResolutionCategory,
)
from agentworks.secrets.preview import ResolutionPreview, SourcePreviewAttempt
from agentworks.secrets.sources import SourceProvenance


def test_operational_list_json_commands_are_closed_parseable_envelopes(monkeypatch) -> None:
    """Wire every new operational list command through its fact projection."""
    from agentworks.agents import manager as agents
    from agentworks.agents.manager.inspect import AgentListing
    from agentworks.cli.commands import agent, console, session, vm, workspace
    from agentworks.sessions import manager as sessions
    from agentworks.sessions import multi_console
    from agentworks.sessions.manager._queries import SessionListing
    from agentworks.sessions.multi_console.attach import ConsoleListing
    from agentworks.vms import manager as vms
    from agentworks.vms.manager.inspect import VMListing
    from agentworks.workspaces import manager as workspaces
    from agentworks.workspaces.manager.create import WorkspaceListing

    monkeypatch.setattr("agentworks.config.load_config", lambda **_kwargs: object())
    for module in (agent, console, session, vm, workspace):
        monkeypatch.setattr(module, "get_db", lambda: object())
    monkeypatch.setattr(vms, "vm_listing", lambda _db: VMListing(vms=()))
    monkeypatch.setattr(workspaces, "workspace_listing", lambda _db, **_kwargs: WorkspaceListing(workspaces=()))
    monkeypatch.setattr(agents, "agent_listing", lambda _db, **_kwargs: AgentListing(agents=()))
    monkeypatch.setattr(sessions, "session_listing", lambda _db, _config, **_kwargs: SessionListing(sessions=()))
    monkeypatch.setattr(multi_console, "console_listing", lambda _db, **_kwargs: ConsoleListing(consoles=()))

    for argv, command, collection in (
        (["vm", "list", "--output", "json"], "vm.list", "vms"),
        (["workspace", "list", "--output", "json"], "workspace.list", "workspaces"),
        (["agent", "list", "--output", "json"], "agent.list", "agents"),
        (["session", "list", "--output", "json", "--no-status"], "session.list", "sessions"),
        (["console", "list", "--output", "json"], "console.list", "consoles"),
    ):
        result = CliRunner().invoke(app, argv)
        assert result.exit_code == 0, result.output
        document = _json_document(result)
        assert document["command"] == command
        assert document["data"] == {collection: []}


def test_operational_json_usage_errors_have_empty_stdout_before_work(monkeypatch) -> None:
    """New local output options reject invalid or incompatible forms before services."""
    from agentworks.cli.commands import agent, console, session, vm, workspace

    for module in (agent, console, session, vm, workspace):
        monkeypatch.setattr(module, "get_db", lambda: (_ for _ in ()).throw(AssertionError("no database")))
    monkeypatch.setattr(
        "agentworks.config.load_config",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("no configuration")),
    )

    for argv in (
        ["vm", "list", "--output", "yaml"],
        ["vm", "list", "--names-only", "--output", "json"],
        ["workspace", "list", "--names-only", "--output", "json"],
        ["agent", "list", "--names-only", "--output", "json"],
        ["session", "list", "--names-only", "--output", "json"],
        ["console", "list", "--names-only", "--output", "json"],
    ):
        result = CliRunner().invoke(app, argv)
        assert result.exit_code != 0
        assert result.stdout_bytes == b""
        assert result.stderr_bytes


def test_operational_describe_json_commands_are_deterministic_and_exclude_opaque_state(monkeypatch) -> None:
    """Exercise every describe command through Typer with representative nullable facts."""
    from agentworks.agents import manager as agents
    from agentworks.agents.manager.inspect import AgentDescription, AgentSession
    from agentworks.cli.commands import agent, console, session, vm, workspace
    from agentworks.db import VMRow, WorkspaceRow
    from agentworks.sessions import manager as sessions
    from agentworks.sessions import multi_console
    from agentworks.sessions.manager._queries import SessionDescription
    from agentworks.sessions.multi_console.attach import ConsoleDescription, ConsoleMember, ConsoleShell
    from agentworks.vms import manager as vms
    from agentworks.vms.manager.inspect import VMDescription, VMDetailFacts, VMInspectionIssueSource, VMIssue
    from agentworks.workspaces import manager as workspaces
    from agentworks.workspaces.manager.create import WorkspaceDescription, WorkspaceDetailFacts, WorkspaceSession

    marker = "secret-value platform-metadata socket-path boot-id harness-state"
    vm_row = VMRow(
        name="box",
        site="site",
        template=None,
        admin_template=None,
        extra_packages=[],
        provisioning_status="complete",
        init_status="complete",
        tailscale_host=None,
        cpus=None,
        memory_gib=None,
        disk_gib=None,
        swap_gib=None,
        admin_username="admin",
        hostname="box",
        created_at="2026-01-01",
        last_seen_at=None,
        platform_metadata={"opaque": marker},
    )
    workspace_row = WorkspaceRow("ws", "box", None, "/work/ws", "2026-01-01", "ws-ws")
    monkeypatch.setattr("agentworks.config.load_config", lambda **_kwargs: object())
    for module in (agent, console, session, vm, workspace):
        monkeypatch.setattr(module, "get_db", lambda: object())
    monkeypatch.setattr(
        vms,
        "vm_description",
        lambda *_args, **_kwargs: VMDescription(
            vm=VMDetailFacts.from_row(vm_row),
            platform=None,
            backend=None,
            observed_status="stopped",
            status_disposition="idle",
            system_slug=None,
            system_slug_state="unset",
            live_resources=None,
            agents=(),
            workspaces=(),
            events=(),
            issues=(VMIssue(VMInspectionIssueSource.SECRET_RESOLUTION),),
            diagnostics=(),
        ),
    )
    monkeypatch.setattr(
        workspaces,
        "workspace_description",
        lambda *_args, **_kwargs: WorkspaceDescription(
            WorkspaceDetailFacts.from_row(workspace_row),
            (WorkspaceSession("s", "t", "admin", None),),
            (),
        ),
    )
    monkeypatch.setattr(
        agents,
        "agent_description",
        lambda *_args, **_kwargs: AgentDescription(
            "a", "box", "agent-a", None, False, "2026-01-01", (), (AgentSession("s", "t", "ws"),)
        ),
    )
    monkeypatch.setattr(
        sessions,
        "session_description",
        lambda *_args, **_kwargs: SessionDescription(
            "s", "ws", "box", "t", None, "admin", None, "stopped", None, "2026-01-01", "2026-01-02"
        ),
    )
    monkeypatch.setattr(
        multi_console,
        "console_description",
        lambda *_args, **_kwargs: ConsoleDescription(
            "c", "box", False, "2026-01-01", "2026-01-02", (ConsoleMember(0, "s", (ConsoleShell(None, False),)),)
        ),
    )

    for argv, command, root_key, fields in (
        (
            ["vm", "describe", "box", "--output", "json"],
            "vm.describe",
            "vm",
            [
                "name",
                "created_at",
                "site",
                "platform",
                "backend",
                "observed_status",
                "status_disposition",
                "operator_stopped",
                "hostname",
                "system_slug",
                "system_slug_state",
                "template",
                "admin_template",
                "admin_username",
                "provisioning_status",
                "initialization_status",
                "tailscale_host",
                "last_seen_at",
                "provisioned_resources",
                "live_resources",
                "agents",
                "workspaces",
                "events",
            ],
        ),
        (
            ["workspace", "describe", "ws", "--output", "json"],
            "workspace.describe",
            "workspace",
            ["name", "vm_name", "template", "path", "created_at", "sessions", "agents"],
        ),
        (
            ["agent", "describe", "a", "--output", "json"],
            "agent.describe",
            "agent",
            ["name", "vm_name", "linux_user", "template", "grant_all", "created_at", "explicit_grants", "sessions"],
        ),
        (
            ["session", "describe", "s", "--output", "json"],
            "session.describe",
            "session",
            [
                "name",
                "workspace_name",
                "vm_name",
                "template",
                "harness_integration",
                "mode",
                "agent_name",
                "status",
                "pid",
                "created_at",
                "updated_at",
            ],
        ),
        (
            ["console", "describe", "c", "--output", "json"],
            "console.describe",
            "console",
            ["name", "vm_name", "admin_shell", "created_at", "updated_at", "sessions"],
        ),
    ):
        first = CliRunner().invoke(app, argv)
        second = CliRunner().invoke(app, argv)
        assert first.exit_code == second.exit_code == 0, first.output
        assert first.stdout_bytes == second.stdout_bytes
        document = _json_document(first)
        assert document["command"] == command
        record = cast("dict[str, object]", cast("dict[str, object]", document["data"])[root_key])
        assert list(record) == fields
        assert marker.encode() not in first.stdout_bytes

    session_data = _json_document(CliRunner().invoke(app, ["session", "describe", "s", "--output", "json"]))["data"]
    session_record = cast("dict[str, object]", cast("dict[str, object]", session_data)["session"])
    assert session_record["pid"] is None


def test_operational_human_describe_commands_keep_literal_no_color_bytes(monkeypatch) -> None:
    """Pin default and explicit human streams for all new describe paths."""
    from agentworks.agents import manager as agents
    from agentworks.agents.manager.inspect import AgentDescription
    from agentworks.cli.commands import agent, console, session, vm, workspace
    from agentworks.db import VMRow, WorkspaceRow
    from agentworks.sessions import manager as sessions
    from agentworks.sessions import multi_console
    from agentworks.sessions.manager._queries import SessionDescription
    from agentworks.sessions.multi_console.attach import ConsoleDescription
    from agentworks.vms import manager as vms
    from agentworks.vms.manager.inspect import VMDescription, VMDetailFacts, render_vm_description
    from agentworks.workspaces import manager as workspaces
    from agentworks.workspaces.manager.create import WorkspaceDescription, WorkspaceDetailFacts

    vm_row = VMRow(
        "box",
        "site",
        None,
        None,
        [],
        "complete",
        "complete",
        None,
        None,
        None,
        None,
        None,
        "admin",
        "box",
        "2026-01-01",
        None,
    )
    ws_row = WorkspaceRow("ws", "box", None, "/work/ws", "2026-01-01", "ws-ws")
    monkeypatch.setattr("agentworks.config.load_config", lambda **_kwargs: object())
    for module in (agent, console, session, vm, workspace):
        monkeypatch.setattr(module, "get_db", lambda: object())
    monkeypatch.setattr(
        vms,
        "vm_description",
        lambda *_args, **_kwargs: VMDescription(
            VMDetailFacts.from_row(vm_row), None, None, None, None, None, "unset", None, (), (), (), (), ()
        ),
    )
    monkeypatch.setattr(
        workspaces,
        "workspace_description",
        lambda *_args, **_kwargs: WorkspaceDescription(WorkspaceDetailFacts.from_row(ws_row), (), ()),
    )
    monkeypatch.setattr(
        agents,
        "agent_description",
        lambda *_args, **_kwargs: AgentDescription("a", "box", "agent-a", None, False, "2026-01-01", (), ()),
    )
    monkeypatch.setattr(
        sessions,
        "session_description",
        lambda *_args, **_kwargs: SessionDescription(
            "s", "ws", "box", "t", None, "admin", None, "stopped", None, "2026-01-01", "2026-01-02"
        ),
    )
    monkeypatch.setattr(
        multi_console,
        "console_description",
        lambda *_args, **_kwargs: ConsoleDescription("c", "box", False, "2026-01-01", "2026-01-02", ()),
    )
    monkeypatch.setattr(
        vms,
        "describe_vm",
        lambda *_args, **_kwargs: render_vm_description(vms.vm_description(*_args, **_kwargs)),
    )
    monkeypatch.setattr(
        sessions,
        "describe_session",
        lambda *_args, **_kwargs: sessions.render_session_description(sessions.session_description(*_args, **_kwargs)),
    )

    expected = {
        (
            "vm",
            "describe",
            "box",
        ): b"Name:           box\nCreated:        2026-01-01\nSite:           site\nPlatform:       -\nBackend:        -\nStatus:         -\nHostname:       box\nSystem Slug:    -\nTemplate:       -\nAdmin User:     admin\nProvisioning:   complete\nInitialization: complete\nTailscale IP:   -\n\nAgents (0):\n  (none)\n\nWorkspaces (0):\n  (none)\n\nEvents (0):\n  (none)\n",  # noqa: E501
        (
            "workspace",
            "describe",
            "ws",
        ): b"Name:       ws\nVM:         box\nTemplate:   default\nPath:       /work/ws\nCreated:    2026-01-01\n\nSessions (0):\n  (none)\n\nAgents with access (0):\n  (none)\n",  # noqa: E501
        (
            "agent",
            "describe",
            "a",
        ): b"Name:       a\nVM:         box\nLinux user: agent-a\nTemplate:   -\nGrant all:  no\nCreated:    2026-01-01\n\nExplicit grants (0):\n  (none)\n\nSessions (0):\n  (none)\n",  # noqa: E501
        (
            "session",
            "describe",
            "s",
        ): b"Name:       s\nWorkspace:  ws\nVM:         box\nTemplate:   t\nHarness integration: -\nMode:       admin\nStatus:     stopped\nCreated:    2026-01-01\nUpdated:    2026-01-02\n",  # noqa: E501
        (
            "console",
            "describe",
            "c",
        ): b"Name:        c\nVM:          box\nAdmin shell: no\nCreated:     2026-01-01\nUpdated:     2026-01-02\n\nConfigured sessions: 0\n",  # noqa: E501
    }
    for command, expected_stdout in expected.items():
        default = CliRunner().invoke(app, ["--non-interactive", *command])
        explicit = CliRunner().invoke(app, ["--non-interactive", *command, "--output", "human"])
        assert default.exit_code == explicit.exit_code == 0
        assert default.stdout_bytes == explicit.stdout_bytes == expected_stdout
        assert default.stderr_bytes == explicit.stderr_bytes == b""


def test_operational_human_list_commands_keep_literal_empty_bytes(monkeypatch) -> None:
    """Pin the established human empty-state bytes beside the JSON empty arrays."""
    from agentworks.agents import manager as agents
    from agentworks.agents.manager.inspect import AgentListing
    from agentworks.cli.commands import agent, console, session, vm, workspace
    from agentworks.sessions import manager as sessions
    from agentworks.sessions import multi_console
    from agentworks.sessions.manager._queries import SessionListing
    from agentworks.sessions.multi_console.attach import ConsoleListing
    from agentworks.vms import manager as vms
    from agentworks.vms.manager.inspect import VMListing
    from agentworks.workspaces import manager as workspaces
    from agentworks.workspaces.manager.create import WorkspaceListing

    monkeypatch.setattr("agentworks.config.load_config", lambda **_kwargs: object())
    for module in (agent, console, session, vm, workspace):
        monkeypatch.setattr(module, "get_db", lambda: object())
    monkeypatch.setattr(vms, "vm_listing", lambda _db: VMListing(()))
    monkeypatch.setattr(workspaces, "workspace_listing", lambda _db, **_kwargs: WorkspaceListing(()))
    monkeypatch.setattr(agents, "agent_listing", lambda _db, **_kwargs: AgentListing(()))
    monkeypatch.setattr(sessions, "session_listing", lambda _db, _config, **_kwargs: SessionListing(()))
    monkeypatch.setattr(multi_console, "console_listing", lambda _db, **_kwargs: ConsoleListing(()))
    expected = {
        ("vm", "list"): b"No VMs registered.\n",
        ("workspace", "list"): b"No workspaces found.\n",
        ("agent", "list"): b"No agents found.\n",
        ("session", "list"): b"No sessions found.\n",
        ("console", "list"): b"No consoles found.\n",
    }
    for command, expected_stdout in expected.items():
        default = CliRunner().invoke(app, ["--non-interactive", *command])
        explicit = CliRunner().invoke(app, ["--non-interactive", *command, "--output", "human"])
        assert default.exit_code == explicit.exit_code == 0
        assert default.stdout_bytes == explicit.stdout_bytes == expected_stdout
        assert default.stderr_bytes == explicit.stderr_bytes == b""


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


def test_secret_list_json_preserves_source_precedence_without_values(monkeypatch) -> None:
    from agentworks import bootstrap, config
    from agentworks.secrets import inspect

    table = SecretTable(
        sources=("first", "second"),
        rows=(
            SecretRow(
                name="token",
                description="test token",
                cells=(
                    SecretSourceCell("first", True, "TOKEN", None),
                    SecretSourceCell("second", False, None, "not configured"),
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
    assert data["sources"] == ["first", "second"]
    assert [item["source"] for item in data["secrets"][0]["sources"]] == ["first", "second"]
    assert "value" not in result.stdout


def test_resource_kinds_json_uses_closed_data_shape(monkeypatch) -> None:
    from agentworks import bootstrap, config
    from agentworks.resources import inspect

    monkeypatch.setattr(config, "load_config", lambda **_kwargs: object())
    monkeypatch.setattr(bootstrap, "load_request_registry", lambda _config, **_kwargs: object())
    monkeypatch.setattr(
        inspect,
        "list_kinds",
        lambda _registry: [KindRow("secret", "declarable", 1, "secret configuration")],
    )

    kinds = CliRunner().invoke(app, ["resource", "kinds", "--output", "json"])
    _assert_human_baseline(
        ["resource", "kinds"],
        b"KIND    CATEGORY    RESOURCES  DESCRIPTION\nsecret  declarable  1          secret configuration\n",
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


def test_secret_describe_json_preserves_nulls_and_source_order(monkeypatch) -> None:
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
        source_mappings=(
            SourceMapping("work-op", "onepassword", SourceProvenance.DECLARED, True, "op://Work/token", None),
            SourceMapping("prompt-fallback", "prompt", SourceProvenance.DECLARED, True, None, "source unavailable"),
        ),
        resolution=StaticResolution(
            category=StaticResolutionCategory.ATTEMPTABLE,
            source="work-op",
            identifier="op://Work/token",
            skipped_not_ready=(),
        ),
        preview=ResolutionPreview(
            name="token",
            result=PreviewAvailable(),
            source="work-op",
            identifier="op://Work/token",
            attempts=(SourcePreviewAttempt("work-op", "op://Work/token", PreviewAvailable()),),
        ),
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
    assert [mapping["source"] for mapping in described["source_mappings"]] == ["work-op", "prompt-fallback"]
    assert [mapping["backend"] for mapping in described["source_mappings"]] == ["onepassword", "prompt"]


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


def test_malformed_config_doctor_json_and_human_share_structured_facts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both doctor formats consume the same typed config-load failure."""
    from agentworks import config, doctor

    config_path = tmp_path / "config.toml"
    config_path.write_text('broken = "unterminated\n')
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)

    def config_only_checks(
        *,
        completion_version: str | None = None,
    ) -> HealthReport:
        del completion_version
        group, _config, _registry = doctor._check_config()
        return HealthReport(groups=[group])

    monkeypatch.setattr(doctor, "run_checks", config_only_checks)

    json_result = CliRunner().invoke(app, ["doctor", "--output", "json"])
    human_default = CliRunner().invoke(app, ["doctor"])
    human_explicit = CliRunner().invoke(app, ["doctor", "--output", "human"])

    assert json_result.exit_code == 1, json_result.output
    assert json_result.stderr_bytes == b""
    json_data = _json_document(json_result)["data"]
    groups = cast("list[dict[str, object]]", cast("dict[str, object]", json_data)["groups"])
    checks = cast("list[dict[str, object]]", groups[0]["checks"])
    config_check = checks[1]
    assert config_check["status"] == "fail"
    message = cast("str", config_check["message"])
    assert "invalid config file" in message
    assert human_default.exit_code == human_explicit.exit_code == 1
    assert human_default.stdout_bytes == human_explicit.stdout_bytes
    assert human_default.stderr_bytes == human_explicit.stderr_bytes == b""
    assert message in human_default.stdout


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
    assert "\x1b" not in captured.err
    assert service_calls == 0
