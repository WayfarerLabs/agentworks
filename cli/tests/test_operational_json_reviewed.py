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


def _assert_terminal_safe(rendered: bytes) -> None:
    """Allow ordinary line framing and tabs, but no terminal controls."""
    text = rendered.decode()
    assert not any(ord(character) < 32 and character not in "\n\t" for character in text)
    assert not any(0x7F <= ord(character) <= 0x9F for character in text)


def _stub_full_doctor_environment(
    monkeypatch: pytest.MonkeyPatch,
    config: object,
) -> None:
    """Keep run_checks real while replacing host/config-dependent probes."""
    from agentworks import doctor
    from agentworks.resources import Registry

    def group(name: str) -> doctor.HealthGroup:
        result = doctor.HealthGroup(name)
        result.ok("Reviewed fixture")
        return result

    config_group = group("Configuration")
    registry = Registry.empty()
    monkeypatch.setattr(doctor, "_check_config", lambda **_kwargs: (config_group, config, registry))
    for function_name, group_name in (
        ("_check_python", "Python"),
        ("_check_required_tools", "Required tools"),
        ("_check_tailscale", "Tailscale"),
        ("_check_plugins", "System plugins"),
        ("_check_vm_platforms", "VM platforms"),
        ("_check_secret_backends", "Secret backends"),
        ("_check_secrets", "Secrets"),
        ("_check_completions", "Completions"),
    ):
        monkeypatch.setattr(doctor, function_name, lambda *_args, _name=group_name, **_kwargs: group(_name))


def test_doctor_stale_schema_is_inspection_only_in_human_and_json(
    tmp_path: Path,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real full report guards every DB read until migration consent."""
    import agentworks.db as db_module
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
    _stub_full_doctor_environment(monkeypatch, make_config())
    constructor_modes: list[bool] = []
    original_init = Database.__init__

    def recording_init(self: Database, *args: object, **kwargs: object) -> None:
        constructor_modes.append(cast("bool", kwargs.get("read_only", False)))
        original_init(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Database, "__init__", recording_init)
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
    by_name = {cast("str", group["name"]): group for group in groups}
    system_checks = cast("list[dict[str, object]]", by_name["System"]["checks"])
    site_checks = cast("list[dict[str, object]]", by_name["VM sites"]["checks"])
    database_checks = cast("list[dict[str, object]]", by_name["Database"]["checks"])
    assert system_checks == [{"name": "System slug", "status": "info", "message": None, "hint": None}]
    assert site_checks == [{"name": "VM sites", "status": "info", "message": None, "hint": None}]
    assert database_checks == [{"name": "Schema", "status": "warn", "message": None, "hint": None}]
    assert b"System slug: pending database migration" in human.stdout_bytes
    assert b"VM sites: pending database migration" in human.stdout_bytes
    assert db_path.read_bytes() == after_human == before
    assert Database.check_schema(db_path) == (True, stale_version, LATEST_VERSION)
    assert constructor_modes == []


def _database_files(db_path: Path) -> tuple[Path, Path, Path]:
    return db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")


def _file_bytes(paths: tuple[Path, ...]) -> tuple[bytes | None, ...]:
    return tuple(path.read_bytes() if path.exists() else None for path in paths)


def _doctor_database_checks(machine_stdout: bytes) -> dict[str, list[dict[str, object]]]:
    document = cast("dict[str, object]", json.loads(machine_stdout))
    data = cast("dict[str, object]", document["data"])
    groups = cast("list[dict[str, object]]", data["groups"])
    return {cast("str", group["name"]): cast("list[dict[str, object]]", group["checks"]) for group in groups}


def test_doctor_clean_closed_wal_database_is_sidecar_free_and_directory_read_only(
    tmp_path: Path,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Immutable inspection reads a clean WAL main file without sidecars."""
    import agentworks.db as db_module
    from agentworks.db import Database

    db_path = tmp_path / "current.db"
    database = Database(db_path)
    database.set_setting("system_slug", "closed-visible")
    database.close()
    paths = _database_files(db_path)
    assert _file_bytes(paths)[1:] == (None, None)
    before = _file_bytes(paths)

    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    _stub_full_doctor_environment(monkeypatch, make_config())
    db_path.chmod(0o444)
    tmp_path.chmod(0o555)
    try:
        human = CliRunner().invoke(app, ["doctor", "--output", "human"])
        machine = CliRunner().invoke(app, ["doctor", "--output", "json"])
        after = _file_bytes(paths)
    finally:
        tmp_path.chmod(0o755)
        db_path.chmod(0o644)

    assert human.exit_code == machine.exit_code == 0, human.output
    assert b"System slug: closed-visible" in human.stdout_bytes
    assert b"Contents: 0 VMs, 0 workspaces" in human.stdout_bytes
    checks = _doctor_database_checks(machine.stdout_bytes)
    assert checks["System"] == [{"name": "System slug", "status": "ok", "message": None, "hint": None}]
    assert checks["Database"] == [
        {"name": "Schema", "status": "ok", "message": None, "hint": None},
        {"name": "Contents", "status": "ok", "message": None, "hint": None},
    ]
    assert after == before


def test_doctor_active_wal_snapshot_preserves_sidecars_and_reads_uncheckpointed_row(
    tmp_path: Path,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Active WAL facts are read from disposable copies, never originals."""
    import agentworks.db as db_module
    from agentworks.db import Database

    db_path = tmp_path / "active.db"
    writer = Database(db_path)
    writer.set_setting("system_slug", "active-wal-visible")
    paths = _database_files(db_path)
    try:
        assert all(path.exists() for path in paths)
        before = _file_bytes(paths)
        monkeypatch.setattr(db_module, "DB_PATH", db_path)
        _stub_full_doctor_environment(monkeypatch, make_config())
        for path in paths:
            path.chmod(0o444)
        tmp_path.chmod(0o555)
        human = CliRunner().invoke(app, ["doctor", "--output", "human"])
        machine = CliRunner().invoke(app, ["doctor", "--output", "json"])
        after = _file_bytes(paths)
    finally:
        tmp_path.chmod(0o755)
        for path in paths:
            if path.exists():
                path.chmod(0o644)
        writer.close()

    assert human.exit_code == machine.exit_code == 0, human.output
    assert b"System slug: active-wal-visible" in human.stdout_bytes
    checks = _doctor_database_checks(machine.stdout_bytes)
    assert checks["System"] == [{"name": "System slug", "status": "ok", "message": None, "hint": None}]
    assert checks["Database"] == [
        {"name": "Schema", "status": "ok", "message": None, "hint": None},
        {"name": "Contents", "status": "ok", "message": None, "hint": None},
    ]
    assert after == before


def test_inspection_snapshot_retries_concurrent_checkpoint_after_main_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A checkpoint transition cannot produce a mixed or silently stale copy."""
    import agentworks.db.database as database_module
    from agentworks.db import Database

    db_path = tmp_path / "checkpoint.db"
    writer = Database(db_path)
    writer.set_setting("system_slug", "committed-before-checkpoint")
    paths = _database_files(db_path)
    original_fingerprint = database_module._fingerprint_stream
    checkpointed: tuple[bytes | None, ...] | None = None

    def checkpoint_after_main_copy(
        source: Path,
        destination: Path | None = None,
    ):  # noqa: ANN202
        nonlocal checkpointed
        fingerprint = original_fingerprint(source, destination)
        if source == db_path and destination is not None and checkpointed is None:
            writer.close()
            checkpointed = _file_bytes(paths)
        return fingerprint

    monkeypatch.setattr(database_module, "_fingerprint_stream", checkpoint_after_main_copy)

    with Database.inspection_snapshot(db_path) as (exists, current, latest, snapshot):
        assert exists and current == latest and snapshot is not None
        assert snapshot.get_setting("system_slug") == "committed-before-checkpoint"

    assert checkpointed is not None
    assert checkpointed[1:] == (None, None)
    assert _file_bytes(paths) == checkpointed


def test_inspection_snapshot_resolves_symlink_before_active_sidecars(
    tmp_path: Path,
) -> None:
    """A database alias discovers WAL state beside the real database identity."""
    from agentworks.db import Database

    real_directory = tmp_path / "real"
    alias_directory = tmp_path / "alias"
    real_directory.mkdir()
    alias_directory.mkdir()
    db_path = real_directory / "state.db"
    alias_path = alias_directory / "agentworks.db"
    writer = Database(db_path)
    writer.set_setting("system_slug", "resolved-active-wal")
    alias_path.symlink_to(db_path)
    paths = _database_files(db_path)
    before = _file_bytes(paths)
    try:
        with Database.inspection_snapshot(alias_path) as (exists, current, latest, snapshot):
            assert exists and current == latest and snapshot is not None
            assert snapshot.get_setting("system_slug") == "resolved-active-wal"
        after = _file_bytes(paths)
    finally:
        writer.close()

    assert after == before
    assert not Path(f"{alias_path}-wal").exists()
    assert not Path(f"{alias_path}-shm").exists()


def test_inspection_snapshot_retry_exhaustion_is_clean_and_path_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A continuously changing source fails closed without residue or paths."""
    import agentworks.db.database as database_module
    from agentworks.db import Database
    from agentworks.errors import StateError

    db_path = tmp_path / "operator-private-name.db"
    writer = Database(db_path)
    writer.set_setting("system_slug", "busy")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    real_temporary_directory = database_module.tempfile.TemporaryDirectory
    created: list[Path] = []
    attempts = 0

    def tracked_temporary_directory(*, prefix: str):  # noqa: ANN202
        temporary = real_temporary_directory(prefix=prefix, dir=scratch)
        created.append(Path(temporary.name))
        return temporary

    def always_changes(source_db: Path, snapshot_db: Path) -> None:
        nonlocal attempts
        del source_db, snapshot_db
        attempts += 1
        raise database_module._SnapshotChanged

    monkeypatch.setattr(database_module.tempfile, "TemporaryDirectory", tracked_temporary_directory)
    monkeypatch.setattr(database_module, "_copy_verified_file_set", always_changes)
    try:
        with pytest.raises(StateError) as raised, Database.inspection_snapshot(db_path):
            pytest.fail("an unstable snapshot must not be yielded")
    finally:
        writer.close()

    message = str(raised.value)
    assert message == "state database inspection snapshot could not be created"
    assert str(db_path) not in message and str(scratch) not in message
    assert attempts == database_module._INSPECTION_SNAPSHOT_ATTEMPTS
    assert created and not any(path.exists() for path in created)


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
    assert _plain_native_usage_for_machine_request(["--bogus", "vm", "list", "--output", "json"])
    assert _plain_native_usage_for_machine_request(["--debug=malformed", "vm", "list", "--output=json"])
    assert not _plain_native_usage_for_machine_request(["vm", "create", "box", "--output", "json"])
    assert not _plain_native_usage_for_machine_request(["--bogus", "vm", "create", "box", "--output=json"])
    assert not _plain_native_usage_for_machine_request(["vm", "exec", "box", "--", "--output", "json"])


@pytest.mark.parametrize(
    "arguments",
    [
        ["--bogus", "vm", "list", "--output", "json"],
        ["--debug=malformed", "vm", "list", "--output=json"],
        ["--bogus-\x1b[31m\x7f", "vm", "list", "--output=json"],
        ["vm", "list", "--output", "json", "--bad-\x1b[31m\x85"],
        ["vm", "describe", "box\x1b[31m", "extra", "--output=json"],
    ],
)
def test_supported_json_native_usage_sanitizes_untrusted_argv(
    arguments: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Native Click/Typer errors cannot replay terminal bytes from argv."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")

    code, stdout, stderr = _invoke_main_tty(monkeypatch, arguments)

    assert code == 2
    assert stdout == b""
    assert stderr
    _assert_terminal_safe(stderr)


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


def test_machine_config_and_domain_errors_sanitize_untrusted_terminal_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every machine error boundary strips controls while human mode is untouched."""
    from agentworks.cli.commands import vm
    from agentworks.errors import ConfigError, StateError
    from agentworks.vms import manager

    config_message = "bad\x1b[31m config\x00\x7f\x85"
    config_hint = "fix\x1b[2J this\x07\x9f"

    def fail_config(**_kwargs: object) -> object:
        raise ConfigError(config_message, hint=config_hint)

    monkeypatch.setattr("agentworks.config.load_config", fail_config)
    code, stdout, stderr = _invoke_main_tty(monkeypatch, ["vm", "describe", "box", "--output=json"])
    assert code == 1 and stdout == b""
    _assert_terminal_safe(stderr)
    assert stderr == b"Configuration error: bad[31m config\n  Hint: fix[2J this\n"

    monkeypatch.setattr("agentworks.config.load_config", lambda **_kwargs: object())
    monkeypatch.setattr(vm, "get_db", lambda: object())

    def fail_domain(*_args: object, **_kwargs: object) -> object:
        raise StateError("domain\x1b[35m failed\x00\x7f\x85", hint="retry\x1b[2J now\x07\x9f")

    monkeypatch.setattr(manager, "vm_description", fail_domain)
    code, stdout, stderr = _invoke_main_tty(monkeypatch, ["vm", "describe", "box", "--output=json"])
    assert code == 1 and stdout == b""
    _assert_terminal_safe(stderr)
    assert stderr == b"Error: domain[35m failed\n  Hint: retry[2J now\n"


def test_prompt_sanitization_is_machine_only_and_preserves_human_text() -> None:
    """Every shared prompt boundary sanitizes machine text and only it."""
    from agentworks import output
    from agentworks.machine_output import OutputFormat, select_request_output

    label = "label\x1b[31m\x00\x7f\x85"
    hint = "hint\x1b[2J\x07\x9f"
    handler = MagicMock()
    handler.confirm.return_value = True
    handler.choose.return_value = 1
    handler.prompt.return_value = "answer"
    handler.prompt_secret.return_value = "resolved"
    previous = output.get_handler()
    output.set_handler(handler)
    try:
        with output.request_output_state():
            select_request_output(OutputFormat.HUMAN)
            assert output.confirm(label, default=True)
            assert output.choose(label, [hint, label]) == 1
            output.pause(label)
            assert output.prompt(label, default=hint) == "answer"
            assert output.prompt_secret(label, hint) == "resolved"
        with output.request_output_state():
            select_request_output(OutputFormat.JSON)
            assert output.confirm(label, default=True)
            assert output.choose(label, [hint, label]) == 1
            output.pause(label)
            assert output.prompt(label, default=hint) == "answer"
            assert output.prompt_secret(label, hint) == "resolved"
    finally:
        output.set_handler(previous)

    assert handler.confirm.call_args_list == [call(label, 0, True), call("label[31m", 0, True)]
    assert handler.choose.call_args_list == [
        call(label, [hint, label], 0),
        call("label[31m", ["hint[2J", "label[31m"], 0),
    ]
    assert handler.pause.call_args_list == [call(label, 0), call("label[31m", 0)]
    assert handler.prompt.call_args_list == [
        call(label, 0, hint),
        call("label[31m", 0, "hint[2J"),
    ]
    assert handler.prompt_secret.call_args_list == [
        call(label, 0, hint),
        call("label[31m", 0, "hint[2J"),
    ]


def test_machine_confirm_on_tty_emits_no_mouse_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Machine prompt routing never writes terminal-control setup bytes."""
    import click

    from agentworks import output
    from agentworks.cli._typer_output import TyperHandler
    from agentworks.machine_output import OutputFormat, select_request_output

    stdout_bytes = io.BytesIO()
    stderr_bytes = io.BytesIO()
    stdout = _FakeTTY(stdout_bytes, encoding="utf-8", write_through=True)
    stderr = _FakeTTY(stderr_bytes, encoding="utf-8", write_through=True)
    confirm = MagicMock(return_value=True)
    monkeypatch.setattr(click, "confirm", confirm)
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)
    previous = output.get_handler()
    output.set_handler(TyperHandler())
    try:
        with output.request_output_state():
            select_request_output(OutputFormat.JSON)
            with output.suppress_presentation():
                assert output.confirm("confirm\x1b[31m\x7f")
    finally:
        output.set_handler(previous)

    stdout.flush()
    stderr.flush()
    assert stdout_bytes.getvalue() == stderr_bytes.getvalue() == b""
    confirm.assert_called_once_with("confirm[31m", default=False, err=True)


@pytest.mark.parametrize("debug_source", ["flag", "environment"])
@pytest.mark.parametrize("failure_kind", ["external", "unexpected"])
def test_machine_debug_traceback_is_full_but_terminal_safe(
    debug_source: str,
    failure_kind: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Machine debug preserves traceback evidence through the sanitizer."""
    from agentworks.cli.commands import vm
    from agentworks.errors import ExternalError
    from agentworks.vms import manager

    monkeypatch.setattr("agentworks.config.load_config", lambda **_kwargs: object())
    monkeypatch.setattr("agentworks.config.CONFIG_DIR", tmp_path)
    monkeypatch.setattr(vm, "get_db", lambda: object())
    record = MagicMock()
    monkeypatch.setattr("agentworks.cli._entry.record_unhandled_error", record)

    def fail_debug(*_args: object, **_kwargs: object) -> object:
        try:
            raise ValueError("DEBUG_CHAIN_MARKER")
        except ValueError as cause:
            error_type = ExternalError if failure_kind == "external" else RuntimeError
            raise error_type("DEBUG_FRAME\x1b[31m\x00\x7f\x85") from cause

    monkeypatch.setattr(manager, "vm_description", fail_debug)
    arguments = ["vm", "describe", "box", "--output=json"]
    if debug_source == "flag":
        arguments.insert(0, "--debug")
        monkeypatch.delenv("AGW_DEBUG", raising=False)
    else:
        monkeypatch.setenv("AGW_DEBUG", "1")

    code, stdout, stderr = _invoke_main_tty(monkeypatch, arguments)

    assert code == 1 and stdout == b""
    _assert_terminal_safe(stderr)
    assert b"Traceback (most recent call last)" in stderr
    assert b"fail_debug" in stderr
    assert b"DEBUG_CHAIN_MARKER" in stderr
    assert b"DEBUG_FRAME[31m" in stderr
    record.assert_not_called()


def test_human_debug_still_reraises_raw_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """The machine-safe debug renderer does not weaken human debugging."""
    from agentworks import cli
    from agentworks.cli.commands import vm

    monkeypatch.setattr("agentworks.config.load_config", lambda **_kwargs: object())
    monkeypatch.setattr(vm, "get_db", lambda: object())

    def fail(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("human debug stays raw\x1b[31m")

    monkeypatch.setattr("agentworks.vms.manager.describe_vm", fail)
    monkeypatch.setattr(sys, "argv", ["agw", "--debug", "vm", "describe", "box", "--output=human"])

    with pytest.raises(RuntimeError, match="human debug stays raw"):
        cli.main()


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


@pytest.mark.parametrize("stage", ["preflight", "secret_resolution"])
def test_vm_inspection_exact_stage_failures_use_closed_issue_source(
    stage: str,
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real boundary failures cannot be mislabeled by a neighboring stage."""
    from agentworks.cli.commands import vm
    from agentworks.errors import StateError
    from agentworks.plugins.proxmox.platform import ProxmoxPlatform
    from agentworks.secrets.resolver import Resolver
    from agentworks.vms import manager as vms

    config = make_config()
    db.insert_vm("box", site="proxmox", hostname="box")
    monkeypatch.setenv("AW_SECRET_PROXMOX_TOKEN", "resolved-for-stage-test")
    monkeypatch.setattr("agentworks.config.load_config", lambda **_kwargs: config)
    monkeypatch.setattr(vm, "get_db", lambda: db)
    monkeypatch.setattr(vms, "_query_live_resources", lambda *_args: None)
    marker = f"EXACT_{stage.upper()}_FAILURE"

    def fail_stage(*_args: object, **_kwargs: object) -> None:
        raise StateError(marker)

    if stage == "preflight":
        monkeypatch.setattr(ProxmoxPlatform, "preflight", fail_stage)
    else:
        monkeypatch.setattr(ProxmoxPlatform, "preflight", lambda self, ctx: None)
        monkeypatch.setattr(Resolver, "resolve", fail_stage)

    result = CliRunner().invoke(app, ["vm", "describe", "box", "--output", "json"])

    assert result.exit_code == 0, result.output
    assert result.stderr_bytes == b""
    document = cast("dict[str, object]", json.loads(result.stdout_bytes))
    data = cast("dict[str, object]", document["data"])
    assert data["issues"] == [{"source": stage, "code": "unavailable"}]
    assert marker.encode() not in result.stdout_bytes


def test_vm_issue_projection_rejects_free_form_source_and_code() -> None:
    """A bad internal fact fails closed instead of echoing free-form text."""
    from agentworks.vms.manager.boundary import VMInspectionIssueSource
    from agentworks.vms.manager.power import VMIssue, VMIssueCode, _project_vm_issue

    with pytest.raises(AssertionError, match="closed source and code"):
        _project_vm_issue(VMIssue(cast("VMInspectionIssueSource", "preflight")))
    with pytest.raises(AssertionError, match="closed source and code"):
        _project_vm_issue(
            VMIssue(
                VMInspectionIssueSource.PREFLIGHT,
                cast("VMIssueCode", "backend-authored-code"),
            )
        )


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
                description="Reviewed\x1b[31m Prox\x00 token\x7f\x85",
                spec={"hint": "enter\x1b[2J the\x07 hidden value\x9f", "backend_mappings": {"env-var": False}},
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
    _assert_terminal_safe(result.stderr_bytes)
    assert b"Secret 'proxmox-token': Reviewed[31m Prox token" in result.stderr_bytes
    assert b"enter[2J the hidden value" in result.stderr_bytes
    assert b"PROMPT_ANSWER_SENTINEL" not in result.stdout_bytes + result.stderr_bytes
