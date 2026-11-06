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
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.sessions.tmux import capture_output
from agentworks.ssh import SSHResult
from tests.conftest import stub_build_registry, stub_vm_ssh_identity
from tests.sessions.test_create_start_restart_orchestrated import _create_stubs, _restart_fixture


@pytest.mark.parametrize("operation", ["create", "restart"])
def test_admin_session_launch_redacts_resolved_values_in_actual_command_log(tmp_path, monkeypatch, operation):
    from agentworks.db import SessionMode
    from agentworks.sessions import manager, tmux
    from agentworks.sessions.manager import _create

    events: list[str] = []
    if operation == "create":
        db = _create_stubs(tmp_path, monkeypatch, events)
    else:
        db, events = _restart_fixture(tmp_path, monkeypatch)
        db._conn.execute(
            "UPDATE sessions SET mode = ?, agent_name = NULL WHERE name = 's1'", (SessionMode.ADMIN.value,)
        )
        db._conn.commit()
    stub_build_registry(monkeypatch)
    stub_vm_ssh_identity(monkeypatch)
    log_root = tmp_path / "logs"
    monkeypatch.setattr("agentworks.ssh.LOG_DIR", log_root)
    secret = uuid4().hex
    original_resolve = _create._preflight_and_resolve

    def resolve(*args, **kwargs):
        values, agent = original_resolve(*args, **kwargs)
        return {**values, "fixture": secret}, agent

    monkeypatch.setattr(_create, "_preflight_and_resolve", resolve)
    monkeypatch.setattr("agentworks.secrets.resolve_for_command", lambda *args, **kwargs: {"fixture": secret})
    monkeypatch.setattr(manager, "_resolve_session_env", lambda *args, **kwargs: {"TOKEN": kwargs["values"]["fixture"]})

    def transport(*args, logger=None, **kwargs):
        def run(command, **options):
            result = SSHResult(0, "", "")
            if logger is not None:
                logger.log_command(command, result)
            return result

        return SimpleNamespace(run=run, write_file=lambda *args, **kwargs: None, logger=logger)

    monkeypatch.setattr(manager, "transport", transport)

    def create(*args, run_command, env, **kwargs):
        assert env["TOKEN"] == secret
        run_command(f"tmux new-session -e TOKEN={env['TOKEN']}")
        return "/run/agentworks/admin-tmux-sockets/admin/s1.sock", 4243

    monkeypatch.setattr(tmux, "create_session", create)
    try:
        config = SimpleNamespace(session=SimpleNamespace(history_limit=1))
        if operation == "create":
            manager.create_session(
                db, config, name="s1", workspace="ws1", admin=True, interaction=TtyInteractionPolicy.REFUSE
            )
        else:
            manager.restart_session(db, config, name="s1", yes=True, interaction=TtyInteractionPolicy.REFUSE)
        assert db.get_session("s1") is not None
        logs = [path.read_text() for path in log_root.glob("*.log")]
        assert logs and any("tmux new-session" in text for text in logs)
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
