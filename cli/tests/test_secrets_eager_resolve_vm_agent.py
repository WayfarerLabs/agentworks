"""Tests for Phase 6 eager-prompting at ``vms.manager`` / ``agents.manager``
entry points, plus the hermetic-provisioning tripwires that guard the
boundary between build-time and runtime secret scopes.

Split out of the original ``test_secrets_eager_resolve.py`` (see
``_secrets_eager_support.py`` for the full background on FRD R4's
operator-facing guarantee). This file covers the vm/agent slice:

- Provisioning (``vm create``/``reinit``, ``agent create``/``reinit``) is
  hermetic: it must NOT walk operator-env SecretTarget scopes or call
  resolve_for_command. Verified by source inspection so the check
  survives refactors.
- Runtime roots that open a shell or run a remote command (``vm shell``,
  ``vm exec``, ``agent exec``, ``agent shell --workspace``) MUST
  eager-resolve BEFORE the SSH/streaming call, and their env-chain
  SecretTarget must join the operation's ONE resolver boundary.
- The provisioning runners themselves (VM init, agent setup) must never
  inject operator env into install commands, and must end with
  ``_ensure_agentworks_files_sourced`` so a dotfiles installer can't
  silently clobber the source lines that make identity/mise reachable.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agentworks.errors import SecretUnavailableError

from ._secrets_eager_support import _seed_basic_db, _stub_build_registry
from .conftest import stub_vm_gates

__all__ = ["_stub_build_registry"]


# ---------------------------------------------------------------------------
# provisioning is hermetic: no eager-resolve, no operator-env SecretTarget
# ---------------------------------------------------------------------------


def test_vm_create_does_not_eager_resolve_operator_env() -> None:
    """Provisioning is hermetic: operator [admin.env] / [vm_templates.*.env]
    secrets are NOT prompted at vm create. The create path registers
    ONLY the system secrets (tailscale key, git tokens, site config
    secrets) on the operation's resolver, a tight declaration set that
    never walks SecretTarget env scopes. Verify by source inspection
    that no ``SecretTarget(...)`` constructor appears in the vm-create
    call path (no env scope handed to the resolver).
    """
    import inspect

    from agentworks.secrets import resolver as secrets_resolver
    from agentworks.vms import initializer as vm_initializer
    from agentworks.vms import manager as vm_manager
    from agentworks.vms import nodes as vm_nodes

    # Walk the call chain explicitly so the check survives refactors.
    sources = [
        inspect.getsource(vm_manager.create_vm),
        inspect.getsource(vm_initializer.resolve_git_credential_providers),
        inspect.getsource(vm_nodes.VMTemplateNode),
        inspect.getsource(secrets_resolver.Resolver),
    ]
    joined = "\n".join(sources)
    assert "SecretTarget(" not in joined, (
        "found SecretTarget(...) constructed in the vm-create path; "
        "provisioning should not walk operator-env scopes. Operator env "
        "reaches runtime shells only; they get prompted at the use site."
    )


def test_vm_reinit_does_not_eager_resolve_operator_env() -> None:
    """Mirror of test_vm_create_does_not_eager_resolve_operator_env for
    vm reinit. Provisioning paths are hermetic; runtime paths are where
    operator-env secrets get prompted."""
    import inspect

    from agentworks.vms import manager as vm_manager

    src = inspect.getsource(vm_manager.reinit_vm)
    assert "SecretTarget(" not in src, (
        "found SecretTarget(...) constructed in reinit_vm; provisioning should not walk operator-env scopes."
    )


def test_agent_create_does_not_eager_resolve_operator_env() -> None:
    """Provisioning is hermetic: operator [agent.env] / [vm_templates.*.env]
    secrets are NOT prompted at agent create. They're prompted at the
    use site (agent shell, session create, etc.). git credentials remain
    prompted upfront via _collect_agent_credentials; they're a
    provisioning-time concern that lives outside the env-block system."""
    import inspect

    from agentworks.agents import manager as agent_manager

    src = inspect.getsource(agent_manager.create_agent)
    assert "resolve_for_command" not in src, (
        "found resolve_for_command in create_agent; provisioning should not prompt for operator-env secrets."
    )


def test_agent_reinit_does_not_eager_resolve_operator_env() -> None:
    """Mirror of test_agent_create_does_not_eager_resolve_operator_env
    for agent reinit."""
    import inspect

    from agentworks.agents import manager as agent_manager

    src = inspect.getsource(agent_manager.reinit_agent)
    assert "resolve_for_command" not in src, (
        "found resolve_for_command in reinit_agent; provisioning should not prompt for operator-env secrets."
    )


# ---------------------------------------------------------------------------
# vm/agent shell + exec: runtime roots must eager-resolve
# ---------------------------------------------------------------------------


def test_vm_shell_env_target_joins_the_bind_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one-prompt-session pin for the runtime roots: shell_vm
    registers its env-chain SecretTarget on the operation's ONE
    resolver (``register_targets``), so the env secrets ride the SAME
    boundary resolve as the site's config secrets; there is no
    separate env prompt session."""
    from agentworks.vms import manager as vm_manager

    db = _seed_basic_db(tmp_path)
    sentinel_target = object()

    monkeypatch.setattr(
        vm_manager,
        "_resolve_vm_admin_env_scopes",
        lambda *a, **k: vm_manager._VmAdminEnvScopes(vm={}, workspace=None, admin={}),
    )
    monkeypatch.setattr(vm_manager, "_vm_secret_target", lambda *a, **k: sentinel_target)
    # Node construction binds the site's platform before the target
    # registration this test spies on; keep it host-independent (the
    # real lima site is disabled where limactl isn't installed).
    monkeypatch.setattr(
        "agentworks.vms.sites.resolve_site",
        lambda name, registry: SimpleNamespace(),
    )

    class _Stop(Exception):
        pass

    bound_targets: list[list[object]] = []

    from agentworks.secrets.resolver import Resolver

    def _register_spy(self: Resolver, targets: object) -> None:
        bound_targets.append(list(targets))  # type: ignore[call-overload]
        raise _Stop

    monkeypatch.setattr(Resolver, "register_targets", _register_spy)

    config = SimpleNamespace(
        vm=SimpleNamespace(env={}),
        admin=SimpleNamespace(env={}),
    )
    with pytest.raises(_Stop):
        vm_manager.shell_vm(db, config, "vm1")  # type: ignore[arg-type]

    assert bound_targets == [[sentinel_target]]
    db.close()


def test_vm_shell_eager_resolve_fires_before_ssh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """shell_vm must call resolve_for_command BEFORE opening the SSH
    session. A failed eager-resolve produces no SSH call."""
    from agentworks.vms import manager as vm_manager

    db = _seed_basic_db(tmp_path)

    # The root preflights the platform before the env resolve; make
    # the lima tool check deterministic on any host. The activation
    # gate opens (fast path) before the boundary; keep its
    # reachability probe off the network.
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(vm_manager, "_is_tailscale_reachable", lambda host: True)

    monkeypatch.setattr(
        vm_manager,
        "_resolve_vm_admin_env_scopes",
        lambda *a, **k: vm_manager._VmAdminEnvScopes(vm={}, workspace=None, admin={}),
    )

    def _explode(*args: object, **kwargs: object) -> None:
        raise SecretUnavailableError(
            "no active backend could resolve secret(s): api-key",
            hint="api-key: tried env-var",
        )

    from agentworks.secrets.resolver import Resolver

    monkeypatch.setattr(Resolver, "resolve", _explode)

    ssh_called: list[bool] = []

    class _Target:
        def interactive(self, *args: object, **kwargs: object) -> int:
            ssh_called.append(True)
            return 0

    monkeypatch.setattr(
        "agentworks.transports.transport",
        lambda *a, **k: _Target(),
    )

    config = SimpleNamespace(
        vm=SimpleNamespace(env={}),
        admin=SimpleNamespace(env={}),
    )

    with pytest.raises(SecretUnavailableError, match="api-key"):
        vm_manager.shell_vm(db, config, "vm1")  # type: ignore[arg-type]

    assert ssh_called == [], "eager-resolve must precede the SSH session"
    db.close()


def test_vm_exec_eager_resolve_fires_before_ssh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """exec_vm must call resolve_for_command BEFORE running the remote
    command. A failed eager-resolve raises before call_streaming runs."""
    from agentworks.vms import manager as vm_manager

    db = _seed_basic_db(tmp_path)

    # The root preflights the platform before the env resolve; make
    # the lima tool check deterministic on any host. The activation
    # gate opens (fast path) before the boundary; keep its
    # reachability probe off the network.
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(vm_manager, "_is_tailscale_reachable", lambda host: True)

    monkeypatch.setattr(
        vm_manager,
        "_resolve_vm_admin_env_scopes",
        lambda *a, **k: vm_manager._VmAdminEnvScopes(vm={}, workspace=None, admin={}),
    )

    def _explode(*args: object, **kwargs: object) -> None:
        raise SecretUnavailableError(
            "no active backend could resolve secret(s): api-key",
            hint="api-key: tried env-var",
        )

    from agentworks.secrets.resolver import Resolver

    monkeypatch.setattr(Resolver, "resolve", _explode)

    streaming_calls: list[str] = []

    class _Target:
        def call_streaming(self, cmd: str, *, env: object = None) -> int:
            streaming_calls.append(cmd)
            return 0

    monkeypatch.setattr("agentworks.transports.transport", lambda *a, **k: _Target())

    config = SimpleNamespace(
        vm=SimpleNamespace(env={}),
        admin=SimpleNamespace(env={}),
    )

    with pytest.raises(SecretUnavailableError, match="api-key"):
        vm_manager.exec_vm(db, config, "vm1", ["echo", "hi"])  # type: ignore[arg-type]

    assert streaming_calls == [], "eager-resolve must precede call_streaming"
    db.close()


def test_agent_exec_eager_resolve_fires_before_ssh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """exec_agent must call resolve_for_command BEFORE running the
    remote command. A failed eager-resolve raises before call_streaming."""
    from agentworks.agents import manager as agent_manager

    db = _seed_basic_db(tmp_path)

    # The root preflights the platform before the env resolve; make
    # the lima tool check deterministic on any host. The activation
    # gate opens (fast path) before the boundary; keep its
    # reachability probe off the network.
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("agentworks.vms.manager._is_tailscale_reachable", lambda host: True)
    db.insert_agent("a1", "vm1", "agt-a1", template="default")

    monkeypatch.setattr(
        agent_manager,
        "_resolve_agent_direct_env_scopes",
        lambda *a, **k: agent_manager._AgentDirectEnvScopes(vm={}, workspace=None, agent={}),
    )

    def _explode(*args: object, **kwargs: object) -> None:
        raise SecretUnavailableError(
            "no active backend could resolve secret(s): api-key",
            hint="api-key: tried env-var",
        )

    from agentworks.secrets.resolver import Resolver

    monkeypatch.setattr(Resolver, "resolve", _explode)

    streaming_calls: list[str] = []

    class _Target:
        def call_streaming(self, cmd: str, *, env: object = None) -> int:
            streaming_calls.append(cmd)
            return 0

    monkeypatch.setattr("agentworks.transports.agent_transport", lambda *a, **k: _Target())

    config = SimpleNamespace()

    with pytest.raises(SecretUnavailableError, match="api-key"):
        agent_manager.exec_agent(
            db,
            config,
            name="a1",
            command=["echo", "hi"],  # type: ignore[arg-type]
        )

    assert streaming_calls == [], "eager-resolve must precede call_streaming"
    db.close()


def test_agent_exec_env_target_joins_the_bind_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one-prompt-session pin for the agent roots (the Phase 7
    round-2 ordering bug lived here, not in the vm twins): exec_agent
    registers its env-chain SecretTarget on the operation's ONE
    resolver (``register_targets``), so the env secrets ride the SAME
    boundary resolve as the site's config secrets."""
    from agentworks.agents import manager as agent_manager

    db = _seed_basic_db(tmp_path)
    db.insert_agent("a1", "vm1", "agt-a1", template="default")
    sentinel_target = object()

    monkeypatch.setattr(
        agent_manager,
        "_resolve_agent_direct_env_scopes",
        lambda *a, **k: agent_manager._AgentDirectEnvScopes(vm={}, workspace=None, agent={}),
    )
    monkeypatch.setattr(agent_manager, "_agent_direct_secret_target", lambda *a, **k: sentinel_target)
    # Node construction binds the site's platform before the target
    # registration this test spies on; keep it host-independent (the
    # real lima site is disabled where limactl isn't installed).
    monkeypatch.setattr(
        "agentworks.vms.sites.resolve_site",
        lambda name, registry: SimpleNamespace(),
    )

    class _Stop(Exception):
        pass

    bound_targets: list[list[object]] = []

    from agentworks.secrets.resolver import Resolver

    def _register_spy(self: Resolver, targets: object) -> None:
        bound_targets.append(list(targets))  # type: ignore[call-overload]
        raise _Stop

    monkeypatch.setattr(Resolver, "register_targets", _register_spy)

    with pytest.raises(_Stop):
        agent_manager.exec_agent(
            db,
            SimpleNamespace(),
            name="a1",
            command=["echo", "hi"],  # type: ignore[arg-type]
        )

    assert bound_targets == [[sentinel_target]]
    db.close()


def test_shell_agent_passes_workspace_scope_to_secret_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """shell_agent --workspace must include workspace-template env in
    the SecretTarget so workspace-scope secrets get eager-resolved.
    Regression test for the Phase 6.5 review's BLOCKING bug: workspace
    scope was silently dropped from agent shell --workspace."""
    from agentworks.agents import manager as agent_manager

    db = _seed_basic_db(tmp_path)
    db.insert_agent("a1", "vm1", "agt-a1", template="default")
    # Grant the agent access so the authz check passes.
    db.insert_agent_grant("a1", "ws1", "explicit")

    captured_scopes: dict[str, object] = {}

    def _spy_scopes(
        registry: object,
        vm: object,
        agent: object,
        *,
        ws: object = None,
    ) -> object:
        # Record the ws arg so the test can pin "shell_agent passes the
        # workspace row through to the scope resolver."
        captured_scopes["ws"] = ws
        return agent_manager._AgentDirectEnvScopes(vm={}, workspace=None, agent={})

    monkeypatch.setattr(agent_manager, "_resolve_agent_direct_env_scopes", _spy_scopes)
    monkeypatch.setattr(agent_manager, "_agent_direct_secret_target", lambda *a, **k: object())
    stub_vm_gates(monkeypatch)

    class _Sentinel(Exception):
        """Raised from the target registration (the seam that hosts the
        env chain now) so the test stops before SSH; the scopes are
        captured before it."""

    def _explode(*args: object, **kwargs: object) -> None:
        raise _Sentinel

    from agentworks.secrets.resolver import Resolver

    monkeypatch.setattr(Resolver, "register_targets", _explode)

    config = SimpleNamespace()

    with pytest.raises(_Sentinel):
        agent_manager.shell_agent(
            db,
            config,
            name="a1",
            workspace_name="ws1",  # type: ignore[arg-type]
        )

    # The scope resolver received the workspace row, not None. The
    # workspace template env will then flow into both the SecretTarget
    # and compose_env, satisfying FRD R2 for `agent shell --workspace`.
    ws_arg = captured_scopes.get("ws")
    assert ws_arg is not None
    # Verify it's the right workspace row.
    assert getattr(ws_arg, "name", None) == "ws1"
    db.close()


# ---------------------------------------------------------------------------
# provisioning runners: no operator-env injection; identity fragment recovery
# ---------------------------------------------------------------------------


def test_agent_setup_runners_have_no_env_injection() -> None:
    """Source-level tripwire: provisioning is hermetic. None of the agent
    setup runners (install commands, dotfiles install, mise, claude
    plugins) should pass ``env=`` from operator [agent.env] /
    [vm_templates.*.env] tables. Static identity (AGENTWORKS_AGENT)
    reaches them via the per-user ~/.agentworks-profile.sh, written
    EARLY in agent setup phase 2 before any install command runs.
    A future contributor adding ``env=agent_env`` (or any variant) to a
    runner re-introduces the coupling this rule exists to prevent."""
    import inspect

    from agentworks.agents import initializer as agent_init

    src = inspect.getsource(agent_init.create_agent_on_vm)
    assert "env=agent_env" not in src, (
        "found 'env=agent_env' in create_agent_on_vm; provisioning runners "
        "must not inject operator env. Identity comes via the per-user "
        "profile fragment, not SetEnv."
    )
    assert "agent_env = compose_env" not in src, (
        "found 'agent_env = compose_env' in create_agent_on_vm; the "
        "operator-env composition was removed because no provisioning "
        "runner consumes it."
    )


def test_vm_provisioning_runners_have_no_env_injection() -> None:
    """Source-level tripwire: provisioning is hermetic. None of the VM
    init user-facing runners (dotfiles install, mise, user_install_commands,
    claude plugins) should pass ``env=`` from operator [admin.env] /
    [vm_templates.*.env] tables. Static identity reaches
    them via the system-wide /etc/profile.d/agentworks-identity.sh
    written by VM init. Operator env only lands at RUNTIME shells.

    A future contributor adding ``env=admin_env`` to a provisioning
    runner re-introduces the build-time-config-coupling this rule
    exists to prevent."""
    import inspect

    from agentworks.vms import initializer as init

    src = inspect.getsource(init._phase_b_setup)
    assert "env=admin_env" not in src, (
        "found 'env=admin_env' in _phase_b_setup; provisioning runners "
        "must not inject operator env. Identity reaches them via the "
        "system-wide /etc/profile.d/ fragment, not SetEnv."
    )
    assert "admin_env = compose_env" not in src, (
        "found 'admin_env = compose_env' in _phase_b_setup; the "
        "operator-env composition was removed because no provisioning "
        "runner consumes it."
    )


def test_phase_b_setup_ends_with_ensure_files_sourced() -> None:
    """Defensive: ``_ensure_agentworks_files_sourced`` runs as the final
    step of admin VM init so that source lines in shell rc files survive
    a dotfiles installer that ships its own ``.zprofile`` / ``.bashrc`` /
    etc. The grep-or-append shape is idempotent; the rule is just that
    the call exists at the end."""
    import inspect

    from agentworks.vms import initializer as init

    src = inspect.getsource(init._phase_b_setup)
    assert "_ensure_agentworks_files_sourced" in src, (
        "expected _ensure_agentworks_files_sourced call in _phase_b_setup; "
        "without it, a dotfiles installer that overwrites a shell rc "
        "file can leave AGENTWORKS_AGENT and mise activation unreachable."
    )


def test_create_agent_on_vm_ends_with_ensure_files_sourced() -> None:
    """Mirror of test_phase_b_setup_ends_with_ensure_files_sourced for the
    agent path. Agent's dotfiles install runs after the early profile
    write; the final ensure step recovers if dotfiles clobbered our
    source lines."""
    import inspect

    from agentworks.agents import initializer as agent_init

    src = inspect.getsource(agent_init.create_agent_on_vm)
    assert "_ensure_agentworks_files_sourced" in src, (
        "expected _ensure_agentworks_files_sourced call in "
        "create_agent_on_vm; without it, dotfiles install can leave "
        "AGENTWORKS_AGENT and mise activation unreachable for the agent."
    )
