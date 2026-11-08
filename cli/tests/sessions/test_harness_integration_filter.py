"""Harness-integration selection across session inventory and lifecycle."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from typer.testing import CliRunner

from agentworks import output
from agentworks.cli import app
from agentworks.db import SessionMode
from agentworks.errors import NotFoundError
from agentworks.output import Role
from agentworks.resources.graph import Enablement, Readiness
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.sessions import manager as session_manager
from agentworks.sessions.manager import _scope as session_scope
from tests.conftest import ManifestDoc

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from agentworks.config import Config
    from agentworks.db import Database


CLAUDE_TEMPLATE = ManifestDoc(
    "session-template",
    "claude",
    {"harness_integration": {"name": "claude-code"}},
)


def _seed_workspace(db: Database, *, vm: str, workspace: str) -> None:
    db.insert_vm(vm, site="proxmox", hostname=vm)
    db._conn.execute(
        "INSERT INTO workspaces (name, vm_name, workspace_path, linux_group) VALUES (?, ?, ?, ?)",
        (workspace, vm, f"/srv/{workspace}", f"ws-{workspace}"),
    )
    db._conn.commit()


def _seed_session(db: Database, *, name: str, workspace: str, template: str) -> None:
    db.insert_session(name, workspace, template, SessionMode.ADMIN, socket_path=f"/tmp/{name}.sock")


@pytest.mark.parametrize(
    ("arguments", "service"),
    [
        (["list", "--harness-integration", "shell,claude-code"], "list_sessions"),
        (["stop", "--all", "--harness-integration", "shell,claude-code"], "stop_all_sessions"),
        (["start", "--all", "--harness-integration", "shell,claude-code"], "start_all_sessions"),
        (["restart", "--all", "--harness-integration", "shell,claude-code"], "restart_all_sessions"),
    ],
)
def test_cli_parses_and_forwards_csv_harness_integration_filter(
    arguments: list[str],
    service: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def record(*_args: object, **kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(session_manager, service, record)
    monkeypatch.setattr("agentworks.cli.commands.session.get_db", lambda: object())
    monkeypatch.setattr("agentworks.config.load_config", lambda **_kwargs: object())

    result = CliRunner().invoke(app, ["session", *arguments])

    assert result.exit_code == 0, result.output
    assert captured["harness_integration_name"] == ["shell", "claude-code"]


@pytest.mark.parametrize("operation", ["stop", "start", "restart"])
def test_lifecycle_harness_integration_filter_requires_all(
    operation: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def unexpected(*_args: object, **_kwargs: object) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(session_manager, f"{operation}_session", unexpected)

    result = CliRunner().invoke(app, ["session", operation, "s1", "--harness-integration", "shell"])

    assert result.exit_code == 2
    assert called is False


def test_filter_ors_integrations_and_ands_other_filters(
    db: Database,
    tmp_path: Path,
) -> None:
    from tests.orchestrated_fixtures import write_operator_config

    config = write_operator_config(
        tmp_path,
        '[plugins]\nsystem = ["claude"]\n',
        manifests=[CLAUDE_TEMPLATE],
    )
    _seed_workspace(db, vm="vm1", workspace="ws1")
    _seed_workspace(db, vm="vm2", workspace="ws2")
    _seed_session(db, name="shell-one", workspace="ws1", template="default")
    _seed_session(db, name="claude-one", workspace="ws1", template="claude")
    _seed_session(db, name="shell-two", workspace="ws2", template="default")

    selected = session_manager.filter_sessions(
        db,
        config=config,
        workspace_name="ws1",
        harness_integration_name=["shell", "claude-code"],
    )

    assert {session.name for session in selected} == {"shell-one", "claude-one"}


def test_filter_matches_the_desired_instance_overlay(
    db: Database,
    tmp_path: Path,
) -> None:
    from agentworks.instance_specs import parse_instance_spec
    from tests.orchestrated_fixtures import write_operator_config

    config = write_operator_config(
        tmp_path,
        '[plugins]\nsystem = ["claude"]\n',
        manifests=[CLAUDE_TEMPLATE],
    )
    _seed_workspace(db, vm="vm1", workspace="ws1")
    _seed_session(db, name="overlaid", workspace="ws1", template="claude")
    overlay = parse_instance_spec("session", '{"harness_integration": {"name": "shell"}}')
    db.instance_state.put_desired_overlay("session", "overlaid", overlay.payload)

    shell = session_manager.filter_sessions(db, config=config, harness_integration_name="shell")
    claude = session_manager.filter_sessions(db, config=config, harness_integration_name="claude-code")

    assert [session.name for session in shell] == ["overlaid"]
    assert claude == []


def test_unresolvable_session_cannot_positively_match(
    db: Database,
    make_config: Callable[..., Config],
    captured_output,  # noqa: ANN001
) -> None:
    config = make_config()
    _seed_workspace(db, vm="vm1", workspace="ws1")
    _seed_session(db, name="good", workspace="ws1", template="default")
    _seed_session(db, name="dangling", workspace="ws1", template="missing")

    selected = session_manager.filter_sessions(db, config=config, harness_integration_name="shell")

    assert [session.name for session in selected] == ["good"]
    assert [role for role, _level, _message in captured_output.lines] == [Role.WARNING]


class _RecordingGraph:
    def __init__(
        self,
        calls: list[tuple[str, str]],
        *,
        enablement: dict[str, Enablement],
        readiness: dict[str, Readiness],
    ) -> None:
        self._calls = calls
        self._enablement = enablement
        self._readiness = readiness

    def enablement_of(self, kind: str, name: str) -> Enablement:
        assert kind == "harness-integration"
        self._calls.append(("enablement", name))
        return self._enablement[name]

    def readiness_of(self, kind: str, name: str) -> Readiness:
        assert kind == "harness-integration"
        self._calls.append(("readiness", name))
        return self._readiness[name]


class _RecordingRegistry:
    def __init__(
        self,
        names: set[str],
        calls: list[tuple[str, str]],
        *,
        enablement: dict[str, Enablement] | None = None,
        readiness: dict[str, Readiness] | None = None,
    ) -> None:
        self._names = names
        self._calls = calls
        self.graph = _RecordingGraph(
            calls,
            enablement=enablement or {name: Enablement.enabled for name in names},
            readiness=readiness or {name: Readiness.ready() for name in names},
        )

    def lookup(self, kind: str, name: str) -> object:
        assert kind == "harness-integration"
        self._calls.append(("lookup", name))
        if name not in self._names:
            raise KeyError(name)
        return object()

    def iter_kind_items(self, kind: str):  # noqa: ANN201
        assert kind == "harness-integration"
        return iter((name, object()) for name in self._names)


def test_availability_is_checked_in_phases_and_warnings_are_deduplicated(
    captured_output,  # noqa: ANN001
) -> None:
    calls: list[tuple[str, str]] = []
    registry = _RecordingRegistry(
        {"ready", "disabled", "blocked"},
        calls,
        enablement={
            "ready": Enablement.enabled,
            "disabled": Enablement.disabled,
            "blocked": Enablement.enabled,
        },
        readiness={
            "ready": Readiness.ready(),
            "disabled": Readiness.ready(),
            "blocked": Readiness.blocked("fixture limitation"),
        },
    )

    selected = session_scope._available_harness_integrations(  # type: ignore[arg-type]
        registry,
        ["disabled", "disabled", "blocked", "ready"],
    )

    assert selected == frozenset({"ready"})
    assert calls == [
        ("lookup", "disabled"),
        ("lookup", "blocked"),
        ("lookup", "ready"),
        ("enablement", "disabled"),
        ("enablement", "blocked"),
        ("enablement", "ready"),
        ("readiness", "blocked"),
        ("readiness", "ready"),
    ]
    assert [role for role, _level, _message in captured_output.lines] == [Role.WARNING, Role.WARNING]


def test_mixed_unknown_filter_errors_before_availability_or_database_work(
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    calls: list[tuple[str, str]] = []
    registry = _RecordingRegistry({"ready"}, calls)
    monkeypatch.setattr(
        "agentworks.bootstrap.load_request_registry",
        lambda *_args, **_kwargs: registry,
    )

    class UntouchedDatabase:
        def __getattribute__(self, name: str) -> object:
            if name.startswith("get_") or name.startswith("list_"):
                raise AssertionError("database work must follow harness filter validation")
            return super().__getattribute__(name)

    with pytest.raises(NotFoundError) as caught:
        session_manager.filter_sessions(  # type: ignore[arg-type]
            UntouchedDatabase(),
            config=object(),  # type: ignore[arg-type]
            harness_integration_name=["ready", "missing"],
        )

    assert caught.value.entity_kind == "harness-integration"
    assert caught.value.entity_name == "missing"
    assert calls == [("lookup", "ready"), ("lookup", "missing")]
    assert captured_output.lines == []


def test_all_excluded_filter_stops_before_batch_runtime_work(
    db: Database,
    make_config: Callable[..., Config],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = make_config(manifests=[CLAUDE_TEMPLATE])
    _seed_workspace(db, vm="vm1", workspace="ws1")
    _seed_session(db, name="disabled", workspace="ws1", template="claude")

    def unexpected(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("an empty explicit filter must not enter the VM boundary")

    monkeypatch.setattr(session_manager, "_batch_vm_boundary", unexpected)

    session_manager.stop_all_sessions(
        db,
        config,
        harness_integration_name="claude-code",
        interaction=TtyInteractionPolicy.REFUSE,
    )


def test_list_filter_builds_one_offline_registry(
    db: Database,
    make_config: Callable[..., Config],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agentworks.bootstrap as bootstrap

    config = make_config()
    _seed_workspace(db, vm="vm1", workspace="ws1")
    _seed_session(db, name="shell", workspace="ws1", template="default")
    real = bootstrap.load_request_registry
    calls: list[dict[str, object]] = []

    def recording_loader(*args: object, **kwargs: object):  # noqa: ANN202
        calls.append(dict(kwargs))
        return real(*args, **kwargs)

    monkeypatch.setattr(bootstrap, "load_request_registry", recording_loader)

    listing = session_manager.session_listing(db, config, harness_integration_name="shell")

    assert [session.name for session in listing.sessions] == ["shell"]
    assert calls == [{"include_live_resources": False}]


def test_suppressed_presentation_hides_filter_warnings(
    db: Database,
    make_config: Callable[..., Config],
    captured_output,  # noqa: ANN001
) -> None:
    config = make_config(manifests=[CLAUDE_TEMPLATE])
    _seed_workspace(db, vm="vm1", workspace="ws1")
    _seed_session(db, name="disabled", workspace="ws1", template="claude")

    with output.suppress_presentation():
        listing = session_manager.session_listing(
            db,
            config,
            harness_integration_name="claude-code",
            require_vm_names=True,
        )

    assert listing.sessions == ()
    assert captured_output.lines == []
