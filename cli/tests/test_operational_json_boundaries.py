"""Actual-boundary JSON coverage for resolver and degraded VM behavior."""

from __future__ import annotations

import contextlib
import json
import threading
from contextvars import copy_context
from dataclasses import replace
from typing import TYPE_CHECKING, cast

import pytest
from typer.testing import CliRunner

from agentworks.capabilities.secret_backend import InteractionBroker, TtyInteractionAccess
from agentworks.cli import app
from agentworks.db import PID_STOPPED, SessionMode, SessionStatus, VMStatus
from agentworks.resources.graph import Readiness
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.secrets.resolve import ActiveSource, ResolutionBatch
from tests.conftest import stub_vm_ssh_identity
from tests.instance_state_support import stub_instance_state

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from agentworks.config import Config
    from agentworks.db import Database
    from agentworks.resources.registry import Registry
    from agentworks.secrets.base import SecretDecl


@pytest.fixture(autouse=True)
def _stub_ssh_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_vm_ssh_identity(monkeypatch)


def _install_skipped_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentworks.secrets import resolve

    real_active_sources = resolve.active_sources

    def active_sources(config: Config, registry: Registry) -> tuple[ActiveSource, ...]:
        sources = real_active_sources(config, registry)
        return (replace(sources[0], readiness=Readiness.blocked("source offline")), *sources)

    monkeypatch.setattr(resolve, "active_sources", active_sources)


def _seed_vm(db: Database) -> None:
    db.insert_vm("box", site="proxmox", hostname="box")
    db.update_vm_tailscale("box", "100.64.0.9")
    db.update_vm_platform_metadata("box", {"node": "pve1", "vmid": "101", "opaque": "PLATFORM_SECRET"})


def _seed_session(db: Database) -> None:
    _seed_vm(db)
    db.insert_workspace("ws", "/srv/ws", "box", "ws-ws")
    db.insert_session(
        "session-a",
        "ws",
        "default",
        SessionMode.ADMIN,
        socket_path="/tmp/SECRET_SOCKET",
        harness_integration_state={"SECRET_HARNESS_STATE": True},
    )
    db.update_session_runtime(
        "session-a",
        socket_path="/tmp/SECRET_SOCKET",
        pid=PID_STOPPED,
        boot_id=None,
        tmux_server_start_ticks=None,
    )


def _wire_cli(monkeypatch: pytest.MonkeyPatch, db: Database, config: Config) -> None:
    from agentworks.cli.commands import session, vm

    monkeypatch.setattr("agentworks.config.load_config", lambda **_kwargs: config)
    monkeypatch.setattr(vm, "get_db", lambda: db)
    monkeypatch.setattr(session, "get_db", lambda: db)


def _platform_fast_path(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentworks.plugins.proxmox.platform import ProxmoxPlatform
    from agentworks.vms import manager

    monkeypatch.setattr(manager, "_is_tailscale_reachable", lambda _host: True)
    monkeypatch.setattr(ProxmoxPlatform, "display_backend_name", lambda self, vm: "pve1/101")
    monkeypatch.setattr(ProxmoxPlatform, "status", lambda self, vm, ctx: VMStatus.RUNNING)
    monkeypatch.setattr(manager, "_query_live_resources", lambda db, vm, config: None)


def _resolution_spy(monkeypatch: pytest.MonkeyPatch) -> list[tuple[tuple[str, ...], tuple[str, ...], str, bool]]:
    from agentworks.secrets import resolve

    real_resolve_batch = resolve.resolve_batch
    calls: list[tuple[tuple[str, ...], tuple[str, ...], str, bool]] = []

    def resolve_batch(
        secrets: Sequence[SecretDecl],
        sources: Sequence[ActiveSource],
        *,
        tty_access: TtyInteractionAccess,
        interaction_broker: InteractionBroker | None,
    ) -> ResolutionBatch:
        calls.append(
            (
                tuple(secret.name for secret in secrets),
                tuple(source.name for source in sources),
                tty_access.value,
                interaction_broker is not None,
            )
        )
        return real_resolve_batch(
            secrets,
            sources,
            tty_access=tty_access,
            interaction_broker=interaction_broker,
        )

    monkeypatch.setattr(resolve, "resolve_batch", resolve_batch)
    return calls


def _assert_json_envelope_only(result: object, command: str) -> dict[str, object]:
    stdout = result.stdout_bytes  # type: ignore[attr-defined]
    assert stdout.endswith(b"\n") and not stdout.endswith(b"\n\n")
    assert stdout.count(b"\n") == 1
    document = cast("dict[str, object]", json.loads(stdout))
    assert list(document) == ["schema_version", "command", "data"]
    assert document["schema_version"] == 1
    assert document["command"] == command
    assert b"Resolved " not in stdout
    assert b"skipping " not in stdout
    return document


def test_vm_describe_json_suppresses_the_ordinary_resolver_presentation(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both formats use the same resolver while JSON suppresses its presentation."""
    config = make_config()
    _seed_vm(db)
    _wire_cli(monkeypatch, db, config)
    _platform_fast_path(monkeypatch)
    _install_skipped_backend(monkeypatch)
    calls = _resolution_spy(monkeypatch)

    human = CliRunner().invoke(app, ["vm", "describe", "box", "--output", "human"])
    machine = CliRunner().invoke(app, ["vm", "describe", "box", "--output", "json"])

    assert human.exit_code == machine.exit_code == 0, machine.output
    assert human.stdout_bytes
    assert human.stderr_bytes == b""
    document = _assert_json_envelope_only(machine, "vm.describe")
    assert machine.stderr_bytes == b""
    assert b"PLATFORM_SECRET" not in machine.stdout_bytes
    assert document["data"]
    assert calls == [
        (
            ("proxmox-token",),
            ("env-var", "env-var", "prompt"),
            "unavailable",
            False,
        ),
        (
            ("proxmox-token",),
            ("env-var", "env-var", "prompt"),
            "unavailable",
            False,
        ),
    ]


def test_session_describe_bypasses_activation_for_both_formats(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Session observation never enters the activation composition root."""
    from agentworks.sessions import manager as sessions

    config = make_config()
    _seed_session(db)
    _wire_cli(monkeypatch, db, config)
    monkeypatch.setattr("agentworks.vms.manager.require_vm_ssh_boundary", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sessions, "check_session_status", lambda *_args, **_kwargs: SessionStatus.STOPPED)

    chain: list[str] = []

    @contextlib.contextmanager
    def prepare(*args: object, **kwargs: object) -> Iterator[object]:
        chain.append("_prepare_vm")
        yield object()

    monkeypatch.setattr(sessions, "_prepare_vm", prepare)

    human = CliRunner().invoke(app, ["session", "describe", "session-a", "--output", "human"])
    machine = CliRunner().invoke(app, ["session", "describe", "session-a", "--output", "json"])

    assert human.exit_code == machine.exit_code == 0, machine.output
    assert human.stdout_bytes
    assert human.stderr_bytes == b""
    _assert_json_envelope_only(machine, "session.describe")
    assert machine.stderr_bytes == b""
    assert chain == []
    for excluded in (b"SECRET_HARNESS_STATE", b"SECRET_SOCKET"):
        assert excluded not in machine.stdout_bytes


def test_session_list_status_bypasses_provider_resolution_and_activation(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit session status uses only its non-activating observer."""
    from agentworks.sessions import manager as sessions

    config = make_config()
    _seed_session(db)
    db.update_session_runtime(
        "session-a", socket_path="/tmp/SECRET_SOCKET", pid=None, boot_id=None, tmux_server_start_ticks=None
    )
    _wire_cli(monkeypatch, db, config)
    calls = _resolution_spy(monkeypatch)
    monkeypatch.setattr(
        sessions,
        "ensure_pids_batch",
        lambda *args, **kwargs: pytest.fail("session list performed durable runtime repair"),
    )
    monkeypatch.setattr(
        sessions,
        "observe_session_statuses",
        lambda rows, *, db, config: {"session-a": SessionStatus.RUNNING},
    )

    machine = CliRunner().invoke(app, ["session", "list", "--status", "--output", "json"])
    unchanged = db.get_session("session-a")

    assert machine.exit_code == 0, machine.output
    _assert_json_envelope_only(machine, "session.list")
    assert machine.stderr_bytes == b""
    assert unchanged is not None and (unchanged.pid, unchanged.boot_id) == (None, None)
    assert calls == []

    calls.clear()
    db.update_session_runtime(
        "session-a", socket_path="/tmp/SECRET_SOCKET", pid=None, boot_id=None, tmux_server_start_ticks=None
    )
    human = CliRunner().invoke(app, ["session", "list", "--status", "--output", "human"])
    assert human.exit_code == 0, human.output
    assert b"STATUS" in human.stdout_bytes
    assert human.stderr_bytes == b""
    assert calls == []


def test_session_json_never_enters_activation_around_the_envelope(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Describe JSON reaches only the non-activating session observer."""
    from agentworks import output
    from agentworks.plugins.proxmox.platform import ProxmoxPlatform
    from agentworks.sessions import manager as sessions
    from agentworks.vms import manager as vms

    config = make_config()
    _seed_session(db)
    _wire_cli(monkeypatch, db, config)
    monkeypatch.setattr(vms, "_tailscale_rejoin_required", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(ProxmoxPlatform, "status", lambda self, row, ctx: VMStatus.STOPPED)
    monkeypatch.setattr(ProxmoxPlatform, "start", lambda self, row, ctx: output.info("ACTIVATION_OUTPUT_SENTINEL"))
    monkeypatch.setattr(vms, "_ensure_tailscale", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("agentworks.vms.manager.require_vm_ssh_boundary", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sessions, "check_session_status", lambda *_args, **_kwargs: SessionStatus.STOPPED)

    result = CliRunner().invoke(app, ["session", "describe", "session-a", "--output", "json"])

    assert result.exit_code == 0, result.output
    _assert_json_envelope_only(result, "session.describe")
    assert b"ACTIVATION_OUTPUT_SENTINEL" not in result.stdout_bytes


def test_vm_event_detail_cannot_expose_secret_command_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stored event diagnostics are not a trusted JSON-safe channel."""
    from agentworks.cli.commands import vm
    from agentworks.db import VMRow
    from agentworks.vms import manager
    from agentworks.vms.manager.inspect import VMDescription, VMDetailEvent, VMDetailFacts

    marker = "SECRET_TOKEN=do-not-expose command --password do-not-expose"
    raw_event = f"historical-{marker}"
    row = VMRow(
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
    description = VMDescription(
        VMDetailFacts.from_row(row),
        None,
        None,
        None,
        None,
        None,
        "unset",
        None,
        (),
        (),
        (VMDetailEvent("2026-01-02", raw_event, marker),),
        (),
        (),
        stub_instance_state("vm", "admin"),
    )
    monkeypatch.setattr("agentworks.config.load_config", lambda **_kwargs: object())
    monkeypatch.setattr(vm, "get_db", lambda: object())
    monkeypatch.setattr(manager, "vm_description", lambda *_args, **_kwargs: description)

    result = CliRunner().invoke(app, ["vm", "describe", "box", "--output", "json"])

    assert result.exit_code == 0, result.output
    assert marker.encode() not in result.stdout_bytes
    document = cast("dict[str, object]", json.loads(result.stdout_bytes))
    data = cast("dict[str, object]", document["data"])
    vm_data = cast("dict[str, object]", data["vm"])
    assert vm_data["events"] == [{"created_at": "2026-01-02", "event": "unknown", "detail": None}]


def test_vm_platform_status_issue_retains_successful_bounded_live_resources(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Independent live SSH facts survive a degraded platform-status read."""
    from agentworks.errors import ExternalError
    from agentworks.plugins.proxmox.platform import ProxmoxPlatform
    from agentworks.vms import manager

    config = make_config()
    _seed_vm(db)
    _wire_cli(monkeypatch, db, config)
    monkeypatch.setattr(ProxmoxPlatform, "display_backend_name", lambda self, vm: "pve1/101")
    monkeypatch.setattr(
        ProxmoxPlatform,
        "status",
        lambda self, vm, ctx: (_ for _ in ()).throw(ExternalError("platform status unavailable")),
    )
    live = {
        "cpus": "8",
        "load_avg": "0.1",
        "mem_total": "32 GiB",
        "mem_used": "4 GiB",
        "mem_pct": "12%",
        "swap_total": "0 B",
        "swap_used": "0 B",
        "swap_pct": "0%",
        "disk_total": "256 GiB",
        "disk_used": "64 GiB",
        "disk_pct": "25%",
    }
    monkeypatch.setattr(manager, "_query_live_resources", lambda db, vm, config: live)

    result = CliRunner().invoke(app, ["vm", "describe", "box", "--output", "json"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout_bytes)["data"]
    assert data["issues"] == [{"source": "platform_status", "code": "unavailable"}]
    assert data["vm"]["live_resources"] == {
        "cpus": "8",
        "load_average": "0.1",
        "memory_total": "32 GiB",
        "memory_used": "4 GiB",
        "memory_percent": "12%",
        "swap_total": "0 B",
        "swap_used": "0 B",
        "swap_percent": "0%",
        "disk_total": "256 GiB",
        "disk_used": "64 GiB",
        "disk_percent": "25%",
    }


def test_machine_presentation_suppression_keeps_prompts_interactive_on_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every presentation role is quiet while machine prompts retain answers."""
    from agentworks import output
    from agentworks.cli._typer_output import TyperHandler
    from agentworks.cli.commands import vm
    from agentworks.vms import manager
    from agentworks.vms.manager.inspect import VMDescription, VMDetailFacts

    facts = VMDetailFacts(
        "box",
        "site",
        None,
        None,
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
        False,
    )
    description = VMDescription(
        facts,
        None,
        None,
        None,
        None,
        None,
        "unset",
        None,
        (),
        (),
        (),
        (),
        (),
        stub_instance_state("vm", "admin"),
    )
    answers: list[str] = []

    def presentation_op() -> None:
        output.warn("REGISTRY_WARNING_SENTINEL")
        with output.section("SECTION_SENTINEL"):
            output.info("INFO_SENTINEL")
            output.detail("DETAIL_SENTINEL")
            progress = output.progress("PROGRESS_SENTINEL", total=2)
            progress.update(1, "half")
            progress.done("ready")
            output.result("RESULT_SENTINEL")
            thread = threading.Thread(
                target=copy_context().run,
                args=(output.info, "THREAD_PRESENTATION_SENTINEL"),
            )
        thread.start()
        thread.join()
        answers.append(output.prompt("PROMPT_SENTINEL"))

    def collect(*_args: object, **_kwargs: object) -> VMDescription:
        presentation_op()
        return description

    def describe(*_args: object, **_kwargs: object) -> None:
        presentation_op()
        output.info("HUMAN_COMPLETE")

    monkeypatch.setattr(output, "_handler", TyperHandler())
    monkeypatch.setattr("agentworks.config.load_config", lambda **_kwargs: object())
    monkeypatch.setattr(vm, "get_db", lambda: object())
    monkeypatch.setattr(manager, "vm_description", collect)
    monkeypatch.setattr(manager, "describe_vm", describe)

    human = CliRunner().invoke(app, ["vm", "describe", "box", "--output", "human"], input="human-answer\n")
    machine = CliRunner().invoke(app, ["vm", "describe", "box", "--output", "json"], input="json-answer\n")

    assert human.exit_code == machine.exit_code == 0, machine.output
    assert answers == ["human-answer", "json-answer"]
    assert human.stderr_bytes == b"Warning: REGISTRY_WARNING_SENTINEL\n"
    for marker in (
        b"SECTION_SENTINEL",
        b"INFO_SENTINEL",
        b"DETAIL_SENTINEL",
        b"PROGRESS_SENTINEL",
        b"RESULT_SENTINEL",
        b"THREAD_PRESENTATION_SENTINEL",
        b"PROMPT_SENTINEL",
        b"HUMAN_COMPLETE",
    ):
        assert marker in human.stdout_bytes
    _assert_json_envelope_only(machine, "vm.describe")
    for marker in (
        b"REGISTRY_WARNING_SENTINEL",
        b"SECTION_SENTINEL",
        b"INFO_SENTINEL",
        b"DETAIL_SENTINEL",
        b"PROGRESS_SENTINEL",
        b"RESULT_SENTINEL",
        b"THREAD_PRESENTATION_SENTINEL",
    ):
        assert marker not in machine.stdout_bytes + machine.stderr_bytes
    assert b"PROMPT_SENTINEL" in machine.stderr_bytes


def test_vm_site_construction_error_is_exact_site_lookup_issue(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed live-node construction stays classified at the site boundary."""
    from agentworks.errors import ConfigError
    from agentworks.vms import manager

    config = make_config()
    _seed_vm(db)
    _wire_cli(monkeypatch, db, config)
    marker = "SITE_LOOKUP_DIAGNOSTIC_SENTINEL"
    monkeypatch.setattr(
        "agentworks.vms.sites.lookup_site",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ConfigError(marker)),
    )
    monkeypatch.setattr(manager, "_query_live_resources", lambda *_args, **_kwargs: None)

    result = CliRunner().invoke(app, ["vm", "describe", "box", "--output", "json"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout_bytes)["data"]
    assert data["issues"] == [{"source": "site_lookup", "code": "unavailable"}]
    assert marker.encode() not in result.stdout_bytes + result.stderr_bytes


@pytest.mark.parametrize("stage", ["preflight", "secret_resolution"])
def test_vm_describe_propagates_operator_abort_in_service_and_both_cli_formats(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    """Inspection degradation never turns a declined prompt into success."""
    from agentworks.errors import UserAbort
    from agentworks.vms import manager

    config = make_config()
    _seed_vm(db)
    _wire_cli(monkeypatch, db, config)
    _platform_fast_path(monkeypatch)

    def abort(*_args: object, **_kwargs: object) -> None:
        raise UserAbort("operator declined")

    if stage == "preflight":
        monkeypatch.setattr("agentworks.orchestration.readiness.preflight_all", abort)
    else:
        monkeypatch.setattr("agentworks.orchestration.readiness.preflight_all", lambda *_args, **_kwargs: None)
        monkeypatch.setattr("agentworks.secrets.resolver.Resolver.resolve", abort)

    with pytest.raises(UserAbort, match="operator declined"):
        manager.vm_description(db, config, "box", interaction=TtyInteractionPolicy.ALLOW)

    for output_format in ("human", "json"):
        result = CliRunner().invoke(app, ["vm", "describe", "box", "--output", output_format])
        assert result.exit_code == 1
        assert isinstance(result.exception, UserAbort)
        if output_format == "json":
            assert result.stdout_bytes == b""
