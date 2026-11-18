"""Session logging preserves secrets and captures the actual pane."""

import os
import shutil
import subprocess
import tempfile
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agentworks.errors import ExternalError
from agentworks.execution.results import Completed, Result
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.sessions.tmux import capture_output
from agentworks.ssh import SSHResult
from tests.conftest import stub_build_registry, stub_vm_ssh_identity
from tests.sessions._workload_secrets import register_workload_secret
from tests.sessions.test_create_start_restart_orchestrated import _create_stubs, _restart_fixture


@pytest.mark.parametrize("operation", ["create", "restart"])
def test_admin_session_launch_redacts_resolved_values_in_actual_command_log(
    tmp_path, monkeypatch, operation, make_config, resolve_counter
):
    from agentworks.db import SessionMode
    from agentworks.sessions import manager, tmux
    from agentworks.sessions.manager import _create

    events: list[str] = []
    if operation == "create":
        db = _create_stubs(tmp_path, monkeypatch, events)
    else:
        db, events = _restart_fixture(tmp_path, monkeypatch)
        db._conn.execute(
            "UPDATE sessions SET mode = ?, agent_name = NULL, socket_path = ? WHERE name = 's1'",
            (SessionMode.ADMIN.value, "/run/agentworks/admin-tmux-sockets/admin/s1.sock"),
        )
        db._conn.commit()
    stub_build_registry(monkeypatch)
    stub_vm_ssh_identity(monkeypatch)
    log_root = tmp_path / "logs"
    monkeypatch.setattr("agentworks.execution.oplog.LOG_DIR", log_root)
    secret = uuid4().hex
    original_resolve = _create._preflight_and_resolve

    def resolve(*args, **kwargs):
        return {**original_resolve(*args, **kwargs), "fixture": secret}

    monkeypatch.setattr(_create, "_preflight_and_resolve", resolve)
    if operation == "restart":
        register_workload_secret(db, monkeypatch, name="fixture", value=secret)
    monkeypatch.setattr(manager, "_resolve_session_env", lambda *args, **kwargs: {"TOKEN": kwargs["values"]["fixture"]})

    import agentworks.execution as execution

    routed = execution.vm_access

    def logged_access(vm, config, identity, *, log=None):
        """The routed access, its runs written to the operation's log as a real access writes them.

        The launch, the one request carrying the session's environment, gets
        a guest that echoes its value back on stderr.
        """
        access = routed(vm, config, identity, log=log)
        run = access.run

        def run_and_log(request, **options):
            result = echoed if "TOKEN" in getattr(request, "env", {}) else run(request, **options)
            if log is not None:
                log(request, result)
            return result

        access.run = run_and_log  # type: ignore[method-assign]
        return access

    echoed = Result(Completed(0), b"", f"launch env TOKEN={secret}".encode())
    monkeypatch.setattr("agentworks.execution.vm_access", logged_access)

    def create(*args, run_command, env, **kwargs):
        kwargs["before_spawn"]()
        assert env["TOKEN"] == secret
        if operation == "restart":
            from agentworks.db import SessionStatus

            monkeypatch.setattr(manager, "check_session_status", lambda *a, **k: SessionStatus.RUNNING)
        launched = run_command("tmux new-session", env=env)
        assert launched.stderr == echoed.stderr.decode()
        return "/run/agentworks/admin-tmux-sockets/admin/s1.sock", 4243

    monkeypatch.setattr(tmux, "create_session", create)
    try:
        config = SimpleNamespace(session=SimpleNamespace(history_limit=1))
        if operation == "create":
            manager.create_session(
                db, config, name="s1", workspace="ws1", admin=True, interaction=TtyInteractionPolicy.REFUSE
            )
        else:
            manager.restart_session(db, make_config(), name="s1", yes=True, interaction=TtyInteractionPolicy.REFUSE)
            assert sum("fixture" in names for names in resolve_counter) == 1
        assert db.get_session("s1") is not None
        logs = [path.read_text() for path in log_root.glob("*.log")]
        assert any("env [TOKEN]" in text and "launch env TOKEN=" in text for text in logs)
        assert all(secret not in text for text in logs)
    finally:
        db.close()


@pytest.mark.skipif(os.name != "posix" or shutil.which("tmux") is None, reason="requires Unix tmux")
def test_real_tmux_capture_targets_the_exact_session_pane():
    with tempfile.TemporaryDirectory(prefix="agw-r8-sessions-capture-", dir="/tmp") as directory:
        socket = os.path.join(directory, "capture.sock")
        marker = uuid4().hex
        environment = {"HOME": directory, "PATH": os.defpath, "SHELL": "/bin/sh", "TERM": "xterm"}

        def run(command, **kwargs):
            completed = subprocess.run(
                ["/bin/sh", "-c", command], capture_output=True, text=True, timeout=10, env=environment
            )
            return SSHResult(completed.returncode, completed.stdout, completed.stderr)

        try:
            subprocess.run(
                ["tmux", "-f", "/dev/null", "-S", socket, "new-session", "-d", "-s", "capture", "/bin/sh -i"],
                check=True,
                timeout=10,
                env=environment,
            )
            subprocess.run(["tmux", "-S", socket, "set-buffer", marker], check=True, timeout=10, env=environment)
            subprocess.run(
                ["tmux", "-S", socket, "paste-buffer", "-t", "=capture:"], check=True, timeout=10, env=environment
            )
            deadline = time.monotonic() + 5
            captured = ""
            while marker not in captured and time.monotonic() < deadline:
                captured = capture_output("capture", run_command=run, socket_path=socket)
                if marker not in captured:
                    time.sleep(0.02)
            assert marker in captured
            with pytest.raises(ExternalError):
                capture_output("missing", run_command=run, socket_path=socket)
        finally:
            subprocess.run(["tmux", "-S", socket, "kill-server"], capture_output=True, timeout=10, env=environment)
            probe = subprocess.run(
                ["tmux", "-S", socket, "has-session"], capture_output=True, timeout=10, env=environment
            )
            assert probe.returncode != 0
