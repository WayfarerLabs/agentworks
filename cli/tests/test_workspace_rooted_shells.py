"""Tests for the workspace-rooted shell/exec surfaces.

Pins the new ``--workspace`` plumbing on ``agw vm shell``, ``agw vm exec``,
and ``agw agent exec`` (and the cross-VM mismatch check that was
hoisted into ``agent shell``'s shared resolver as part of the same
refactor). Issue #125 / PR #140.

The tests stay hermetic by monkeypatching the eager-resolve, transport,
and VM-gate boundaries. The intent is to catch regressions in:

- The vm-mismatch ``ValidationError`` raised before any SSH work.
- The agent-side ``AuthorizationError`` on a missing grant.
- Workspace-template env actually flowing into the ``SecretTarget``.
- The remote command shape (``cd <quoted-path> && ...``).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agentworks.db import Database
from agentworks.errors import AuthorizationError, NotFoundError, ValidationError
from tests.conftest import stub_build_registry, stub_vm_gates

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _stub_build_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Serve Registry reads from the module's namespace configs."""
    stub_build_registry(monkeypatch)


class _NullCM:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *_a: object) -> None:
        return None


def _seed_db(tmp_path: Path) -> Database:
    """Two VMs, two workspaces (one per VM), one agent on vm1.

    The two-VM layout is what lets us exercise the cross-VM mismatch
    check: ``--workspace ws-on-vm2`` against a target on ``vm1`` must
    raise before any SSH work.
    """
    db = Database(tmp_path / "test.db")
    db._conn.execute(
        "INSERT INTO vms (name, site, hostname, admin_username, tailscale_host) "
        "VALUES ('vm1', 'lima', 'h', 'admin', '100.64.0.5')"
    )
    db._conn.execute(
        "INSERT INTO vms (name, site, hostname, admin_username, tailscale_host) "
        "VALUES ('vm2', 'lima', 'h', 'admin', '100.64.0.6')"
    )
    db._conn.execute(
        "INSERT INTO workspaces (name, vm_name, workspace_path, linux_group) "
        "VALUES ('ws1', 'vm1', '/opt/agentworks/workspaces/ws1', 'ws-ws1')"
    )
    db._conn.execute(
        "INSERT INTO workspaces (name, vm_name, workspace_path, linux_group) "
        "VALUES ('ws2', 'vm2', '/opt/agentworks/workspaces/ws2', 'ws-ws2')"
    )
    db._conn.commit()
    db.insert_agent("a1", "vm1", "agt-a1", template="default")
    return db


def _patch_vm_common(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the env / secret / transport / VM-gate boundaries for
    ``shell_vm`` and ``exec_vm`` so the tests exercise the manager's
    branching without invoking real SSH or secret resolution."""
    from agentworks.vms import manager as vm_manager

    monkeypatch.setattr(
        vm_manager, "_resolve_vm_admin_env_scopes",
        lambda *a, **k: vm_manager._VmAdminEnvScopes(
            vm={}, workspace=None, admin={}
        ),
    )
    monkeypatch.setattr(vm_manager, "_vm_secret_target", lambda *a, **k: object())
    monkeypatch.setattr("agentworks.secrets.resolve_for_command", lambda *a, **k: None)
    monkeypatch.setattr("agentworks.env.compose_env", lambda **k: {})
    stub_vm_gates(monkeypatch)


def _patch_agent_common(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the env / secret / transport / VM-gate boundaries for
    ``exec_agent``."""
    from agentworks.agents import manager as agent_manager

    monkeypatch.setattr(
        agent_manager, "_resolve_agent_direct_env_scopes",
        lambda *a, **k: agent_manager._AgentDirectEnvScopes(
            vm={}, workspace=None, agent={}
        ),
    )
    monkeypatch.setattr(
        agent_manager, "_agent_direct_secret_target", lambda *a, **k: object()
    )
    monkeypatch.setattr("agentworks.secrets.resolve_for_command", lambda *a, **k: None)
    monkeypatch.setattr("agentworks.env.compose_env", lambda **k: {})
    stub_vm_gates(monkeypatch)
    monkeypatch.setattr(
        agent_manager, "_assert_agent_ssh_works", lambda *a, **k: None
    )


# ---------------------------------------------------------------------------
# vm shell --workspace
# ---------------------------------------------------------------------------


def test_shell_vm_workspace_unknown_raises_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``vm shell vm1 --workspace nope`` must surface a NotFoundError
    before any SSH work."""
    from agentworks.vms import manager as vm_manager

    db = _seed_db(tmp_path)
    _patch_vm_common(monkeypatch)
    config = SimpleNamespace()

    with pytest.raises(NotFoundError, match="nope"):
        vm_manager.shell_vm(  # type: ignore[arg-type]
            db, config, "vm1", workspace_name="nope",
        )


def test_shell_vm_workspace_cross_vm_raises_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``vm shell vm1 --workspace ws2`` (ws2 belongs to vm2) raises
    ``ValidationError`` upfront. This is the load-bearing acceptance
    criterion from issue #125: failures surface BEFORE any SSH work."""
    from agentworks.vms import manager as vm_manager

    db = _seed_db(tmp_path)
    _patch_vm_common(monkeypatch)
    config = SimpleNamespace()

    interactive_calls: list[str] = []

    def _factory(*_a: object, **_k: object) -> object:
        return SimpleNamespace(
            interactive=lambda cmd, **_k: interactive_calls.append(cmd) or 0,
        )

    monkeypatch.setattr("agentworks.transports.transport", _factory)

    with pytest.raises(ValidationError, match="belongs to VM 'vm2', not 'vm1'"):
        vm_manager.shell_vm(  # type: ignore[arg-type]
            db, config, "vm1", workspace_name="ws2",
        )

    assert interactive_calls == [], (
        "the mismatch must be detected before any SSH work; "
        "interactive() must not have been called"
    )


def test_shell_vm_workspace_cds_into_workspace_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``--workspace`` is set, the interactive shell command must
    start with ``cd <quoted-workspace-path> && exec $SHELL -l``."""
    from agentworks.vms import manager as vm_manager

    db = _seed_db(tmp_path)
    _patch_vm_common(monkeypatch)
    config = SimpleNamespace()

    captured_cmd: list[str] = []

    def _factory(*_a: object, **_k: object) -> object:
        return SimpleNamespace(
            interactive=lambda cmd, **_k: captured_cmd.append(cmd) or 0,
        )

    monkeypatch.setattr("agentworks.transports.transport", _factory)

    with pytest.raises(SystemExit):
        vm_manager.shell_vm(  # type: ignore[arg-type]
            db, config, "vm1", workspace_name="ws1",
        )

    assert len(captured_cmd) == 1
    assert captured_cmd[0] == "cd /opt/agentworks/workspaces/ws1 && exec $SHELL -l"


def test_shell_vm_no_workspace_keeps_empty_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``vm shell`` without ``--workspace`` retains the existing
    behavior: ``interactive("")`` (no remote command, default login
    shell)."""
    from agentworks.vms import manager as vm_manager

    db = _seed_db(tmp_path)
    _patch_vm_common(monkeypatch)
    config = SimpleNamespace()

    captured_cmd: list[str] = []

    def _factory(*_a: object, **_k: object) -> object:
        return SimpleNamespace(
            interactive=lambda cmd, **_k: captured_cmd.append(cmd) or 0,
        )

    monkeypatch.setattr("agentworks.transports.transport", _factory)

    with pytest.raises(SystemExit):
        vm_manager.shell_vm(db, config, "vm1")  # type: ignore[arg-type]

    assert captured_cmd == [""]


# ---------------------------------------------------------------------------
# vm exec --workspace
# ---------------------------------------------------------------------------


def test_exec_vm_workspace_cross_vm_raises_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``vm exec vm1 --workspace ws2 ...`` raises ValidationError upfront."""
    from agentworks.vms import manager as vm_manager

    db = _seed_db(tmp_path)
    _patch_vm_common(monkeypatch)
    config = SimpleNamespace()

    streaming_calls: list[str] = []

    def _factory(*_a: object, **_k: object) -> object:
        return SimpleNamespace(
            call_streaming=lambda cmd, **_k: streaming_calls.append(cmd) or 0,
        )

    monkeypatch.setattr("agentworks.transports.transport", _factory)

    with pytest.raises(ValidationError, match="belongs to VM 'vm2', not 'vm1'"):
        vm_manager.exec_vm(  # type: ignore[arg-type]
            db, config, "vm1", ["echo", "hi"], workspace_name="ws2",
        )

    assert streaming_calls == []


def test_exec_vm_workspace_prefixes_cd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``vm exec --workspace`` runs ``cd <path> && <joined-command>``."""
    from agentworks.vms import manager as vm_manager

    db = _seed_db(tmp_path)
    _patch_vm_common(monkeypatch)
    config = SimpleNamespace()

    captured: list[str] = []

    def _factory(*_a: object, **_k: object) -> object:
        return SimpleNamespace(
            call_streaming=lambda cmd, **_k: captured.append(cmd) or 0,
        )

    monkeypatch.setattr("agentworks.transports.transport", _factory)

    rc = vm_manager.exec_vm(  # type: ignore[arg-type]
        db, config, "vm1", ["echo", "hi"], workspace_name="ws1",
    )

    assert rc == 0
    assert captured == ["cd /opt/agentworks/workspaces/ws1 && echo hi"]


def test_exec_vm_no_workspace_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``vm exec`` without ``--workspace`` keeps the original behavior:
    just the joined command, no ``cd`` prefix."""
    from agentworks.vms import manager as vm_manager

    db = _seed_db(tmp_path)
    _patch_vm_common(monkeypatch)
    config = SimpleNamespace()

    captured: list[str] = []

    def _factory(*_a: object, **_k: object) -> object:
        return SimpleNamespace(
            call_streaming=lambda cmd, **_k: captured.append(cmd) or 0,
        )

    monkeypatch.setattr("agentworks.transports.transport", _factory)

    vm_manager.exec_vm(db, config, "vm1", ["echo", "hi"])  # type: ignore[arg-type]

    assert captured == ["echo hi"]


# ---------------------------------------------------------------------------
# agent exec --workspace
# ---------------------------------------------------------------------------


def test_exec_agent_workspace_cross_vm_raises_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``agent exec a1 --workspace ws2`` (a1 lives on vm1, ws2 on vm2)
    raises ValidationError upfront. The agent's authz status is
    irrelevant here -- the vm-match check fires first."""
    from agentworks.agents import manager as agent_manager

    db = _seed_db(tmp_path)
    # Grant ws2 so the only thing that should fail is the vm-match.
    db.insert_agent_grant("a1", "ws2", "explicit")

    _patch_agent_common(monkeypatch)
    config = SimpleNamespace()

    streaming_calls: list[str] = []

    def _factory(*_a: object, **_k: object) -> object:
        return SimpleNamespace(
            call_streaming=lambda cmd, **_k: streaming_calls.append(cmd) or 0,
        )

    monkeypatch.setattr("agentworks.transports.agent_transport", _factory)

    with pytest.raises(ValidationError, match="belongs to VM 'vm2', not 'vm1'"):
        agent_manager.exec_agent(  # type: ignore[arg-type]
            db, config, name="a1", command=["echo", "hi"], workspace_name="ws2",
        )

    assert streaming_calls == []


def test_exec_agent_workspace_missing_grant_raises_authz(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``agent exec a1 --workspace ws1`` without an explicit grant
    raises ``AuthorizationError``. The agent and workspace are on the
    same VM, so vm-match passes -- the authz check is what kills it."""
    from agentworks.agents import manager as agent_manager

    db = _seed_db(tmp_path)
    # Intentionally no grant for ws1.

    _patch_agent_common(monkeypatch)
    config = SimpleNamespace()

    streaming_calls: list[str] = []

    def _factory(*_a: object, **_k: object) -> object:
        return SimpleNamespace(
            call_streaming=lambda cmd, **_k: streaming_calls.append(cmd) or 0,
        )

    monkeypatch.setattr("agentworks.transports.agent_transport", _factory)

    with pytest.raises(AuthorizationError, match="does not have access"):
        agent_manager.exec_agent(  # type: ignore[arg-type]
            db, config, name="a1", command=["echo", "hi"], workspace_name="ws1",
        )

    assert streaming_calls == []


def test_exec_agent_workspace_prefixes_cd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``agent exec --workspace`` wraps the command with the workspace
    ``cd``, then wraps the whole thing in ``$SHELL -lc`` so the agent's
    login profile is sourced."""
    from agentworks.agents import manager as agent_manager

    db = _seed_db(tmp_path)
    db.insert_agent_grant("a1", "ws1", "explicit")

    _patch_agent_common(monkeypatch)
    config = SimpleNamespace()

    captured: list[str] = []

    def _factory(*_a: object, **_k: object) -> object:
        return SimpleNamespace(
            call_streaming=lambda cmd, **_k: captured.append(cmd) or 0,
        )

    monkeypatch.setattr("agentworks.transports.agent_transport", _factory)

    rc = agent_manager.exec_agent(  # type: ignore[arg-type]
        db, config, name="a1", command=["echo", "hi"], workspace_name="ws1",
    )

    assert rc == 0
    assert len(captured) == 1
    # The login-shell wrap shell-quotes the inner command. The exact
    # quoting is shlex's choice; verify both halves are present.
    assert captured[0].startswith("$SHELL -lc ")
    assert "cd /opt/agentworks/workspaces/ws1" in captured[0]
    assert "echo hi" in captured[0]


# ---------------------------------------------------------------------------
# Workspace scope flows into the SecretTarget
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        # Misplaced agw long flag (the common case).
        ["--workspace", "ws1", "pwd"],
        # Misplaced agw flag with a remote command that also has its own
        # short flags downstream.
        ["--workspace", "ws1", "-x", "foo"],
        # Bare ``-``-prefixed remote command -- not an agw flag, but
        # still rejected because the remote shell would choke.
        ["-weird", "args"],
        # Lone short flag.
        ["-x"],
    ],
)
def test_exec_vm_rejects_dash_prefixed_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: list[str],
) -> None:
    """Any ``vm exec`` whose remote-command argv starts with ``-`` is
    rejected before any SSH work. The hint is the same regardless of
    whether the leading token is an agw flag or some other ``-``-
    prefixed token; the operator decides whether the hint applies."""
    from agentworks.vms import manager as vm_manager

    db = _seed_db(tmp_path)
    _patch_vm_common(monkeypatch)
    config = SimpleNamespace()

    with pytest.raises(ValidationError, match="cannot start with '-'") as exc_info:
        vm_manager.exec_vm(db, config, "vm1", command)  # type: ignore[arg-type]
    hint = exc_info.value.hint or ""
    assert "agentworks args must come before the first positional argument" in hint


def test_exec_agent_rejects_dash_prefixed_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``agent exec`` mirrors the same rejection and hint shape as
    ``vm exec``."""
    from agentworks.agents import manager as agent_manager

    db = _seed_db(tmp_path)
    _patch_agent_common(monkeypatch)
    config = SimpleNamespace()

    with pytest.raises(ValidationError, match="cannot start with '-'") as exc_info:
        agent_manager.exec_agent(  # type: ignore[arg-type]
            db, config, name="a1", command=["--workspace", "ws1", "pwd"],
        )
    hint = exc_info.value.hint or ""
    assert "agentworks args must come before the first positional argument" in hint


# ---------------------------------------------------------------------------
# Shell commands DO NOT have the misplaced-flag constraint
# ---------------------------------------------------------------------------
#
# Only the exec CLI commands set ``allow_interspersed_args=False``. The shell
# commands use Click's default parsing, which accepts ``--workspace`` either
# before or after the positional. The tests below pin that contract so a
# future refactor that flips ``allow_interspersed_args`` on the shell
# commands -- and silently breaks both invocation orders -- has to update a
# test.


def _shell_command_params(resource: str, argv: list[str]) -> dict[str, object]:
    """Parse ``argv`` against the ``<resource> shell`` Click command and
    return its bound params dict, without invoking the command body.

    Skips ``get_db`` / ``load_config`` / the SSH transport entirely --
    we're only asserting what Click produces from argv parsing, which
    is the only thing the test cares about.
    """
    import typer

    from agentworks.cli import app

    # ``typer.main.get_command()`` returns real click classes on typer < 0.26
    # and typer-vendored ``typer._click`` classes on typer >= 0.26. Walk the
    # ``.commands`` dict directly instead of isinstance-checking against
    # ``click.Group``, which fails against the vendored classes. Duck-type
    # guard mirrors the ``.commands`` check in ``agentworks.completions.spec``
    # so a shape drift (e.g. typer returning something other than a group)
    # fails with a diagnostic instead of a bare AttributeError.
    click_app = typer.main.get_command(app)
    app_commands = getattr(click_app, "commands", None)
    assert isinstance(app_commands, dict), (
        f"expected typer.main.get_command() to return a group, got {type(click_app).__name__}"
    )
    group = app_commands[resource]
    group_commands = getattr(group, "commands", None)
    assert isinstance(group_commands, dict), (
        f"expected {resource!r} to be a subgroup, got {type(group).__name__}"
    )
    shell_cmd = group_commands["shell"]
    ctx = shell_cmd.make_context(f"{resource} shell", list(argv))
    params: dict[str, object] = ctx.params
    return params


def test_vm_shell_accepts_workspace_flag_in_either_position() -> None:
    """``vm shell`` must accept ``--workspace`` whether it comes before
    or after the VM positional. Default Click parsing handles this; the
    test pins the contract so a future flip of
    ``allow_interspersed_args`` on ``vm shell`` -- which would silently
    break both invocation orders -- has to update a test."""
    before = _shell_command_params("vm", ["--workspace", "ws1", "vm1"])
    after = _shell_command_params("vm", ["vm1", "--workspace", "ws1"])

    assert before.get("name") == "vm1"
    assert before.get("workspace") == "ws1"
    assert after.get("name") == "vm1"
    assert after.get("workspace") == "ws1"


def test_agent_shell_accepts_workspace_flag_in_either_position() -> None:
    """``agent shell`` mirrors ``vm shell``: ``--workspace`` is
    accepted in either position."""
    before = _shell_command_params("agent", ["--workspace", "ws1", "a1"])
    after = _shell_command_params("agent", ["a1", "--workspace", "ws1"])

    assert before.get("name") == "a1"
    assert before.get("workspace") == "ws1"
    assert after.get("name") == "a1"
    assert after.get("workspace") == "ws1"


def test_shell_vm_passes_workspace_scope_to_secret_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``vm shell --workspace`` must include workspace-template env in
    the ``SecretTarget`` so workspace-scope secrets get eager-resolved
    before SSH. Mirrors ``test_shell_agent_passes_workspace_scope_to_
    secret_target`` (in test_secrets_eager_resolve.py) on the admin
    side."""
    from agentworks.vms import manager as vm_manager

    db = _seed_db(tmp_path)
    captured_scopes: dict[str, object] = {}

    def _spy_scopes(
        registry: object, vm: object, *, ws: object = None,
    ) -> object:
        captured_scopes["ws"] = ws
        return vm_manager._VmAdminEnvScopes(vm={}, workspace=None, admin={})

    monkeypatch.setattr(vm_manager, "_resolve_vm_admin_env_scopes", _spy_scopes)
    monkeypatch.setattr(vm_manager, "_vm_secret_target", lambda *a, **k: object())
    monkeypatch.setattr("agentworks.secrets.resolve_for_command", lambda *a, **k: None)
    monkeypatch.setattr("agentworks.env.compose_env", lambda **k: {})
    stub_vm_gates(monkeypatch)

    def _factory(*_a: object, **_k: object) -> object:
        return SimpleNamespace(interactive=lambda *_a, **_k: 0)

    monkeypatch.setattr("agentworks.transports.transport", _factory)

    config = SimpleNamespace()

    with pytest.raises(SystemExit):
        vm_manager.shell_vm(  # type: ignore[arg-type]
            db, config, "vm1", workspace_name="ws1",
        )

    ws_arg = captured_scopes.get("ws")
    assert ws_arg is not None, (
        "workspace row must reach the scope resolver so the workspace "
        "template env enters both the SecretTarget and compose_env"
    )
    assert getattr(ws_arg, "name", None) == "ws1"
