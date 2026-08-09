"""Reviewed request-boundary regressions for operational JSON v1."""

from __future__ import annotations

import ast
import io
import json
import sqlite3
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import MagicMock, call

import pytest
from typer.testing import CliRunner

from agentworks.cli import app

if TYPE_CHECKING:
    from agentworks.db import Database


class _FakeTTY(io.TextIOWrapper):
    def isatty(self) -> bool:
        return True


def _invoke_main_tty(monkeypatch: pytest.MonkeyPatch, arguments: list[str]) -> tuple[int, bytes, bytes]:
    """Invoke the real entry wrapper against isolated TTY byte streams."""
    from agentworks import cli

    stdout_bytes = io.BytesIO()
    stderr_bytes = io.BytesIO()
    stdout = _FakeTTY(stdout_bytes, encoding="utf-8", write_through=True)
    stderr = _FakeTTY(stderr_bytes, encoding="utf-8", write_through=True)
    with monkeypatch.context() as invocation:
        invocation.setattr(sys, "stdout", stdout)
        invocation.setattr(sys, "stderr", stderr)
        invocation.setattr(sys, "argv", ["agw", *arguments])
        with pytest.raises(SystemExit) as raised:
            cli.main()
        stdout.flush()
        stderr.flush()
        result = (
            cast("int", raised.value.code),
            stdout_bytes.getvalue(),
            stderr_bytes.getvalue(),
        )
    return result


def test_doctor_stale_schema_is_inspection_only_in_human_and_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Doctor reports migration consent without opening the migrating DB."""
    import agentworks.db as db_module
    from agentworks import doctor
    from agentworks.db import LATEST_VERSION, Database

    db_path = tmp_path / "stale.db"
    database = Database(db_path)
    database.close()
    connection = sqlite3.connect(db_path)
    connection.execute("DELETE FROM schema_version WHERE version = ?", (LATEST_VERSION,))
    connection.commit()
    connection.close()
    stale_version = LATEST_VERSION - 1
    assert Database.check_schema(db_path) == (True, stale_version, LATEST_VERSION)

    monkeypatch.setattr(db_module, "DB_PATH", db_path)

    def database_only_report(**_kwargs: object) -> doctor.HealthReport:
        return doctor.HealthReport(groups=[doctor._check_database()])

    monkeypatch.setattr(doctor, "run_checks", database_only_report)
    before = db_path.read_bytes()

    human = CliRunner().invoke(app, ["doctor", "--output", "human"])
    after_human = db_path.read_bytes()
    machine = CliRunner().invoke(app, ["doctor", "--output", "json"])

    assert human.exit_code == machine.exit_code == 0
    expected = (
        f"at version {stale_version}, latest is {LATEST_VERSION}; "
        "a normal Agentworks command that opens state will migrate it"
    )
    assert expected.encode() in human.stdout_bytes
    document = cast("dict[str, object]", json.loads(machine.stdout_bytes))
    data = cast("dict[str, object]", document["data"])
    groups = cast("list[dict[str, object]]", data["groups"])
    assert groups[0]["checks"] == [{"name": "Schema", "status": "warn", "message": None, "hint": None}]
    assert db_path.read_bytes() == after_human == before
    assert Database.check_schema(db_path) == (True, stale_version, LATEST_VERSION)


def test_vm_event_enum_covers_every_literal_producer() -> None:
    """A new production event producer must update the closed v1 vocabulary."""
    import agentworks
    from agentworks.vms.manager.power import VMEventName

    produced: set[str] = set()
    package_root = Path(agentworks.__file__).parent
    for source_path in (package_root / "vms").rglob("*.py"):
        tree = ast.parse(source_path.read_text(), filename=str(source_path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr != "insert_vm_event":
                continue
            assert len(node.args) >= 2 and isinstance(node.args[1], ast.Constant)
            assert isinstance(node.args[1].value, str)
            produced.add(node.args[1].value)

    assert produced == {event.value for event in VMEventName if event is not VMEventName.UNKNOWN}


def test_vm_and_agent_names_only_skip_enrichment(captured_output, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
    """Completion paths stop before count and grant enrichment."""
    from agentworks.agents.manager import list_agents
    from agentworks.vms.manager import list_vms

    vm_db = MagicMock()
    vm_db.list_vms.return_value = [SimpleNamespace(name="box")]
    vm_db.count_workspaces_on_vm.side_effect = AssertionError("count enrichment ran")
    vm_db.count_agents_on_vm.side_effect = AssertionError("count enrichment ran")
    vm_db.count_sessions_on_vm.side_effect = AssertionError("count enrichment ran")
    list_vms(vm_db, names_only=True)

    agent_db = MagicMock()
    agent_db.list_agents.return_value = [SimpleNamespace(name="worker")]
    agent_db.list_granted_workspaces_with_types.side_effect = AssertionError("grant enrichment ran")
    list_agents(agent_db, names_only=True)

    assert captured_output.info == ["box", "worker"]


def test_overlapping_human_and_json_threads_isolate_request_state() -> None:
    """Machine suppression never hides an overlapping human transcript."""
    from agentworks import output
    from agentworks.machine_output import OutputFormat, select_request_output

    machine_active = threading.Event()
    human_emitted = threading.Event()
    observed: dict[str, tuple[bool, bool]] = {}
    handler = MagicMock()

    def machine_request() -> None:
        with output.request_output_state():
            select_request_output(OutputFormat.JSON)
            with output.suppress_presentation():
                machine_active.set()
                output.info("MACHINE_INFO_MUST_BE_QUIET")
                output.result("MACHINE_RESULT_MUST_BE_QUIET")
                assert human_emitted.wait(timeout=5)
                observed["machine"] = (output.machine_readable(), output.presentation_suppressed())

    def human_request() -> None:
        assert machine_active.wait(timeout=5)
        with output.request_output_state():
            select_request_output(OutputFormat.HUMAN)
            output.info("HUMAN_INFO_VISIBLE")
            output.result("HUMAN_RESULT_VISIBLE")
            observed["human"] = (output.machine_readable(), output.presentation_suppressed())
            human_emitted.set()

    previous = output.get_handler()
    output.set_handler(handler)
    try:
        machine = threading.Thread(target=machine_request)
        human = threading.Thread(target=human_request)
        machine.start()
        human.start()
        machine.join()
        human.join()
    finally:
        output.set_handler(previous)

    assert observed == {"machine": (True, True), "human": (False, False)}
    assert handler.emit.call_args_list == [
        call(output.Role.BODY, "HUMAN_INFO_VISIBLE", 0),
        call(output.Role.RESULT, "HUMAN_RESULT_VISIBLE", 0),
    ]


def test_session_status_workers_receive_presentation_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The session executor explicitly propagates suppression to each worker."""
    from agentworks import output
    from agentworks.db import SessionStatus
    from agentworks.sessions import manager

    session = SimpleNamespace(name="session-a", workspace_name="ws")
    db = MagicMock()
    db.get_workspace.return_value = SimpleNamespace(vm_name="box")
    db.get_vm.return_value = SimpleNamespace(name="box", tailscale_host="100.64.0.9")
    observed: list[bool] = []
    warnings: list[str] = []
    monkeypatch.setattr(manager, "transport", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(output, "warn", warnings.append)

    def check(rows: object, *, target: object) -> dict[str, SessionStatus]:
        del rows, target
        observed.append(output.presentation_suppressed())
        return {"session-a": SessionStatus.UNKNOWN}

    monkeypatch.setattr("agentworks.sessions.manager._status.batch_check_status", check)
    with output.suppress_presentation():
        result = manager.batch_check_all_sessions([session], db=db, config=object())

    assert result == {"session-a": SessionStatus.UNKNOWN}
    assert observed == [True]
    assert warnings == []


@pytest.mark.parametrize(
    "arguments",
    [
        ["vm", "describe", "--output", "json"],
        ["vm", "list", "--output=json", "--unknown"],
        ["vm", "list", "--names-only", "--output", "json"],
    ],
)
def test_supported_json_native_usage_is_plain_on_a_tty(
    arguments: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing args, unknown options, and callback usage errors carry no ANSI."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")

    code, stdout, stderr = _invoke_main_tty(monkeypatch, arguments)

    assert code == 2
    assert stdout == b""
    assert stderr
    assert b"\x1b[" not in stderr


def test_preparse_detector_is_closed_and_ignores_passthrough() -> None:
    from agentworks.cli._entry import _MACHINE_OUTPUT_PATHS, _plain_native_usage_for_machine_request

    assert len(_MACHINE_OUTPUT_PATHS) == 16
    assert _plain_native_usage_for_machine_request(["doctor", "--output=json"])
    assert _plain_native_usage_for_machine_request(["--debug", "session", "list", "--output", "json"])
    assert not _plain_native_usage_for_machine_request(["vm", "create", "box", "--output", "json"])
    assert not _plain_native_usage_for_machine_request(["vm", "exec", "box", "--", "--output", "json"])


def test_unsupported_mutation_and_passthrough_do_not_select_machine_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """JSON-looking mutation and passthrough tokens retain human TTY styling."""
    from agentworks.cli.commands import vm
    from agentworks.errors import StateError
    from agentworks.vms import manager

    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    mutation = _invoke_main_tty(monkeypatch, ["vm", "create", "box", "--output", "json"])
    assert mutation[0] == 2 and b"\x1b[" in mutation[2]

    monkeypatch.setattr("agentworks.config.load_config", lambda **_kwargs: object())
    monkeypatch.setattr(vm, "get_db", lambda: object())

    def fail(*_args: object, **_kwargs: object) -> int:
        raise StateError("passthrough stayed human")

    monkeypatch.setattr(manager, "exec_vm", fail)
    passthrough = _invoke_main_tty(monkeypatch, ["vm", "exec", "box", "--", "--output", "json"])
    assert passthrough[0] == 1
    assert passthrough[1] == b""
    assert b"\x1b[" in passthrough[2]


def test_covered_json_config_failure_is_plain_on_a_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentworks.errors import ConfigError

    def fail(**_kwargs: object) -> object:
        raise ConfigError("reviewed config failure", hint="review the config")

    monkeypatch.setattr("agentworks.config.load_config", fail)
    code, stdout, stderr = _invoke_main_tty(monkeypatch, ["vm", "describe", "box", "--output=json"])

    assert code == 1
    assert stdout == b""
    assert b"\x1b[" not in stderr
    assert stderr == b"Configuration error: reviewed config failure\n  Hint: review the config\n"


@pytest.mark.parametrize("stage", ["walk", "secret_union"])
def test_vm_residual_graph_failure_propagates_without_preflight_issue(
    stage: str,
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only exact inspection stages degrade; graph-walk failures stay domain errors."""
    from agentworks.cli.commands import vm
    from agentworks.errors import StateError

    config = make_config()
    db.insert_vm("box", site="proxmox", hostname="box")
    monkeypatch.setattr("agentworks.config.load_config", lambda **_kwargs: config)
    monkeypatch.setattr(vm, "get_db", lambda: db)

    marker = f"GRAPH_SECRET_UNION_SENTINEL_{stage}"

    def fail_stage(*_args: object, **_kwargs: object) -> object:
        raise StateError(marker)

    monkeypatch.setattr(
        f"agentworks.orchestration.{'walk.walk' if stage == 'walk' else 'secrets.secret_union'}", fail_stage
    )
    code, stdout, stderr = _invoke_main_tty(monkeypatch, ["vm", "describe", "box", "--output", "json"])

    assert code == 1
    assert stdout == b""
    assert stderr == f"Error: {marker}\n".encode()
    assert b"\x1b[" not in stderr


@pytest.mark.parametrize("surface", ["vm", "session"])
def test_json_boundaries_use_real_hidden_prompt_backend(
    surface: str,
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Interactive prompt resolution writes only prompt prose to stderr."""
    from agentworks import output
    from agentworks.cli._typer_output import TyperHandler
    from agentworks.cli.commands import session, vm
    from agentworks.db import PID_STOPPED, SessionMode, SessionStatus, VMStatus
    from agentworks.plugins.proxmox.platform import ProxmoxPlatform
    from agentworks.sessions import manager as sessions
    from agentworks.vms import manager as vms
    from tests.conftest import ManifestDoc

    config = make_config(
        manifests=[
            ManifestDoc(
                kind="secret",
                name="proxmox-token",
                description="Reviewed Proxmox token",
                spec={"hint": "enter the reviewed hidden value", "backend_mappings": {"env-var": False}},
            )
        ]
    )
    monkeypatch.delenv("AW_SECRET_PROXMOX_TOKEN", raising=False)
    db.insert_vm("box", site="proxmox", hostname="box")
    db.update_vm_tailscale("box", "100.64.0.9")
    db.update_vm_platform_metadata("box", {"node": "pve1", "vmid": "101"})
    monkeypatch.setattr("agentworks.config.load_config", lambda **_kwargs: config)
    monkeypatch.setattr(vm, "get_db", lambda: db)
    monkeypatch.setattr(session, "get_db", lambda: db)
    monkeypatch.setattr(output, "is_interactive", lambda: True)
    monkeypatch.setattr(vms, "_is_tailscale_reachable", lambda _host: True)
    monkeypatch.setattr(ProxmoxPlatform, "display_backend_name", lambda self, row: "pve1/101")
    monkeypatch.setattr(ProxmoxPlatform, "status", lambda self, row, ctx: VMStatus.RUNNING)
    monkeypatch.setattr(vms, "_query_live_resources", lambda *_args: None)

    if surface == "session":
        db.insert_workspace("ws", "/srv/ws", "box", "ws-ws")
        db.insert_session(
            "session-a",
            "ws",
            "default",
            SessionMode.ADMIN,
            socket_path="/tmp/reviewed",
        )
        db.update_session_pid("session-a", PID_STOPPED)
        monkeypatch.setattr(sessions, "_ensure_pid", lambda row, *, target, db: row)
        monkeypatch.setattr(sessions, "check_session_status", lambda row, *, target: SessionStatus.STOPPED)
        arguments = ["session", "describe", "session-a", "--output", "json"]
        command = "session.describe"
    else:
        arguments = ["vm", "describe", "box", "--output", "json"]
        command = "vm.describe"

    previous = output.get_handler()
    output.set_handler(TyperHandler())
    try:
        result = CliRunner().invoke(app, arguments, input="PROMPT_ANSWER_SENTINEL\n")
    finally:
        output.set_handler(previous)

    assert result.exit_code == 0, result.output
    assert result.stdout_bytes.count(b"\n") == 1
    document = cast("dict[str, object]", json.loads(result.stdout_bytes))
    assert document["command"] == command
    data = cast("dict[str, object]", document["data"])
    if surface == "session":
        session_data = cast("dict[str, object]", data["session"])
        assert session_data["status"] == "stopped"
    else:
        vm_data = cast("dict[str, object]", data["vm"])
        assert vm_data["observed_status"] == "running"
    assert b"Secret 'proxmox-token': Reviewed Proxmox token" in result.stderr_bytes
    assert b"enter the reviewed hidden value" in result.stderr_bytes
    assert b"PROMPT_ANSWER_SENTINEL" not in result.stdout_bytes + result.stderr_bytes
