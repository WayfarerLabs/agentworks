"""Tests for the factory functions in :mod:`agentworks.transports`.

Covers the three named factories (``transport``, ``agent_transport``,
``native_transport``), the low-level helper (``transport_for_user``),
the no-failover invariant (polymorphic-transports SDD R3), the Azure
``transient_route`` lifecycle, the None-return typed error, the
reachability-probe retry loop, the defensive empty-host guard, and
``wait_for_reconnect``.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agentworks.errors import StateError
from agentworks.ssh import SSHError, SSHResult
from agentworks.transports import (
    LimaTransport,
    RemoteLimaTransport,
    SSHTransport,
    Transport,
    WSL2Transport,
    agent_transport,
    native_transport,
    transport,
    transport_for_user,
    wait_for_reconnect,
)


def _mock_vm(
    *,
    name: str = "vm1",
    tailscale_host: str | None = "100.64.0.1",
    admin_username: str = "agentworks",
    site: str = "lima",
) -> MagicMock:
    vm = MagicMock()
    vm.name = name
    vm.tailscale_host = tailscale_host
    vm.admin_username = admin_username
    vm.site = site
    return vm


def _mock_config() -> MagicMock:
    config = MagicMock()
    config.operator.ssh_private_key = Path("/home/op/.ssh/agentworks_ed25519")
    return config


def _mock_agent(linux_user: str = "claude") -> MagicMock:
    a = MagicMock()
    a.linux_user = linux_user
    a.name = linux_user
    return a


# ---------------------------------------------------------------------------
# transport_for_user / transport / agent_transport
# ---------------------------------------------------------------------------


def test_transport_for_user_returns_ssh_transport_with_explicit_user() -> None:
    t = transport_for_user(_mock_vm(), _mock_config(), user="alice")
    assert isinstance(t, SSHTransport)
    assert t.host == "100.64.0.1"
    assert t.user == "alice"


def test_transport_for_user_always_uses_operator_key() -> None:
    """``transport_for_user`` always builds the SSHTransport with
    ``config.operator.ssh_private_key`` as the identity. There is no
    override -- the operator's key is the only credential the on-VM
    authorized_keys reconciler installs, so any other path would
    fail auth anyway. Pinning this prevents the regression class
    where a caller forgets to pass identity_file and SSH falls back
    to ``~/.ssh/id_*`` defaults (works on Linux by accident, fails
    on Windows / non-standard key names).
    """
    config = _mock_config()
    t = transport_for_user(_mock_vm(), config, user="alice")
    assert isinstance(t, SSHTransport)
    assert t.identity_file == config.operator.ssh_private_key


def test_transport_for_user_raises_state_error_without_tailscale_host() -> None:
    """The pre-refactor code asserted (disappears under ``python -O``);
    the new factory promotes this to a typed error per SDD R6.
    """
    vm = _mock_vm(tailscale_host=None)
    with pytest.raises(StateError, match="no Tailscale host"):
        transport_for_user(vm, _mock_config(), user="alice")


def test_transport_uses_admin_username() -> None:
    t = transport(_mock_vm(), _mock_config())
    assert isinstance(t, SSHTransport)
    assert t.user == "agentworks"
    assert t.identity_file == Path("/home/op/.ssh/agentworks_ed25519")


def test_agent_transport_uses_agent_linux_user() -> None:
    t = agent_transport(_mock_vm(), _mock_config(), _mock_agent("claude"))
    assert isinstance(t, SSHTransport)
    assert t.user == "claude"
    assert t.host == "100.64.0.1"


def test_admin_and_agent_transports_differ_only_in_user() -> None:
    vm = _mock_vm()
    config = _mock_config()
    admin = transport(vm, config)
    agent = agent_transport(vm, config, _mock_agent("claude"))
    assert isinstance(admin, SSHTransport)
    assert isinstance(agent, SSHTransport)
    assert admin.host == agent.host
    assert admin.identity_file == agent.identity_file
    assert admin.user == "agentworks"
    assert agent.user == "claude"


# ---------------------------------------------------------------------------
# R3: no automatic failover. The named factories never reach for the
# native transport, even when the canonical transport raises.
# ---------------------------------------------------------------------------


def test_transport_failure_does_not_invoke_native_transport() -> None:
    """SDD R3 invariant. ``transport()`` raising must not silently fall
    through to ``native_transport()``. The canonical path either
    works or fails; the operator opts in to the platform-native path.

    Covers two error paths: the no-tailscale-host case (typed
    ``StateError`` from the factory itself) and an arbitrary downstream
    failure inside ``transport_for_user`` (so the pin isn't tied to one
    specific code path).
    """
    vm = _mock_vm(tailscale_host=None)
    config = _mock_config()

    with (
        patch("agentworks.transports.native_transport") as mock_native,
        pytest.raises(StateError),
    ):
        transport(vm, config)
    mock_native.assert_not_called()

    vm = _mock_vm()  # has tailscale_host now
    with (
        patch("agentworks.transports.native_transport") as mock_native,
        patch("agentworks.transports.transport_for_user", side_effect=RuntimeError("synthetic")),
        pytest.raises(RuntimeError),
    ):
        transport(vm, config)
    mock_native.assert_not_called()


# ---------------------------------------------------------------------------
# native_transport: Lima happy path
# ---------------------------------------------------------------------------


def _fake_lima_platform() -> MagicMock:
    platform = MagicMock()
    platform.name = "lima"
    platform.transient_route.return_value = contextlib.nullcontext()
    platform.native_transport.return_value = LimaTransport(vm_name="vm1")
    return platform


def test_native_transport_invokes_transient_route_then_builder() -> None:
    """``transient_route`` is entered before the per-platform builder
    runs, so polymorphism replaces the old isinstance check."""
    vm = _mock_vm(site="lima")
    config = _mock_config()
    platform = _fake_lima_platform()
    fake_target = platform.native_transport.return_value

    with (
        patch.object(fake_target, "run") as mock_run,
        contextlib.ExitStack() as stack,
    ):
        mock_run.return_value = SSHResult(returncode=0, stdout="ok\n", stderr="")
        t = native_transport(vm, platform, config, stack=stack)

    platform.transient_route.assert_called_once_with(vm)
    platform.native_transport.assert_called_once_with(vm, config=config)
    assert t is fake_target


def test_native_transport_none_raises_typed_state_error() -> None:
    """A ``None`` from the platform (proxmox: one-shot guest-agent exec
    can't host a shell) becomes a ``StateError`` carrying the platform's
    own console hint (``no_native_transport_hint``)."""
    from agentworks.capabilities.vm_platform.proxmox import ProxmoxPlatform

    vm = _mock_vm(site="proxmox")
    config = _mock_config()
    platform = MagicMock()
    platform.name = "proxmox"
    platform.no_native_transport_hint = ProxmoxPlatform.no_native_transport_hint
    platform.transient_route.return_value = contextlib.nullcontext()
    platform.native_transport.return_value = None

    with (
        contextlib.ExitStack() as stack,
        pytest.raises(StateError) as exc_info,
    ):
        native_transport(vm, platform, config, stack=stack)
    assert exc_info.value.hint is not None
    assert "serial console" in exc_info.value.hint


def test_native_transport_empty_ssh_host_raises_typed_state_error() -> None:
    """The PR #118 defensive guard: an SSH transport with an empty host
    surfaces clearly rather than letting downstream calls hang on it."""
    vm = _mock_vm(site="azure")
    config = _mock_config()
    platform = MagicMock()
    platform.name = "azure-vm"
    platform.transient_route.return_value = contextlib.nullcontext()
    platform.native_transport.return_value = SSHTransport(host="", user="agentworks")

    with (
        contextlib.ExitStack() as stack,
        pytest.raises(StateError, match="no host"),
    ):
        native_transport(vm, platform, config, stack=stack)


def test_native_transport_reachability_probe_retries_then_succeeds() -> None:
    """The 6-attempt probe loop swallows up to 5 SSHErrors before
    succeeding; the 6th fail propagates."""
    vm = _mock_vm(site="lima")
    config = _mock_config()
    platform = _fake_lima_platform()
    fake_target = platform.native_transport.return_value

    call_count = 0

    def flaky_run(*_a: object, **_kw: object) -> SSHResult:
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise SSHError("not yet")
        return SSHResult(returncode=0, stdout="ok\n", stderr="")

    with (
        patch.object(fake_target, "run", side_effect=flaky_run),
        patch("agentworks.transports.time.sleep"),
        contextlib.ExitStack() as stack,
    ):
        t = native_transport(vm, platform, config, stack=stack)
    assert t is fake_target
    assert call_count == 3


def test_native_transport_reachability_probe_gives_up_after_six() -> None:
    vm = _mock_vm(site="lima")
    config = _mock_config()
    platform = _fake_lima_platform()
    fake_target = platform.native_transport.return_value

    with (
        patch.object(fake_target, "run", side_effect=SSHError("always")),
        patch("agentworks.transports.time.sleep"),
        contextlib.ExitStack() as stack,
        pytest.raises(SSHError),
    ):
        native_transport(vm, platform, config, stack=stack)


# ---------------------------------------------------------------------------
# native_transport: Azure transient_route lifecycle
# ---------------------------------------------------------------------------


def test_native_transport_azure_transient_route_attaches_and_detaches() -> None:
    """``transient_route`` calls attach on enter and detach on exit; both
    fire regardless of whether the downstream code raised."""
    vm = _mock_vm(site="azure")
    config = _mock_config()

    events: list[str] = []

    platform = MagicMock()
    platform.name = "azure-vm"

    @contextlib.contextmanager
    def fake_route(_vm):  # type: ignore[no-untyped-def] # noqa: ANN001, ANN202
        events.append("attach")
        try:
            yield
        finally:
            events.append("detach")

    platform.transient_route.side_effect = fake_route
    fake_target = SSHTransport(host="1.2.3.4", user="agentworks")
    platform.native_transport.return_value = fake_target

    with (
        patch.object(fake_target, "run", return_value=SSHResult(returncode=0, stdout="", stderr="")),
        contextlib.ExitStack() as stack,
    ):
        native_transport(vm, platform, config, stack=stack)
        # Inside the stack: attach has fired, detach has NOT.
        assert events == ["attach"]
    # ExitStack unwinds at end-of-with: detach fires deterministically.
    assert events == ["attach", "detach"]


def test_native_transport_azure_detach_fires_on_downstream_exception() -> None:
    """If the per-platform builder raises after ``transient_route``
    attaches, the detach still runs (context-manager cleanup is bounded
    by the caller's ExitStack)."""
    vm = _mock_vm(site="azure")
    config = _mock_config()

    events: list[str] = []

    platform = MagicMock()
    platform.name = "azure-vm"

    @contextlib.contextmanager
    def fake_route(_vm):  # type: ignore[no-untyped-def] # noqa: ANN001, ANN202
        events.append("attach")
        try:
            yield
        finally:
            events.append("detach")

    platform.transient_route.side_effect = fake_route
    platform.native_transport.side_effect = SSHError("kaboom")

    with (
        contextlib.ExitStack() as stack,
        pytest.raises(SSHError),
    ):
        native_transport(vm, platform, config, stack=stack)

    assert events == ["attach", "detach"]


# ---------------------------------------------------------------------------
# wait_for_reconnect
# ---------------------------------------------------------------------------


def test_wait_for_reconnect_returns_true_on_first_success() -> None:
    """Polymorphic over any Transport via ``run``. The double-check
    handles flapping but only fires after a prior failure."""
    t = SSHTransport(host="1.2.3.4", user="agentworks")
    with patch.object(t, "run", return_value=SSHResult(returncode=0, stdout="", stderr="")) as mock_run:
        assert wait_for_reconnect(t) is True
        mock_run.assert_called_once()


def test_wait_for_reconnect_double_checks_after_prior_failure() -> None:
    t = SSHTransport(host="1.2.3.4", user="agentworks")
    calls = 0

    def flaky(*_a: object, **_kw: object) -> SSHResult:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise SSHError("flap")
        return SSHResult(returncode=0, stdout="", stderr="")

    with (
        patch.object(t, "run", side_effect=flaky),
        patch("agentworks.transports.time.sleep"),
    ):
        assert wait_for_reconnect(t) is True
    # First call fails, second succeeds, third is the double-check.
    assert calls == 3


def test_wait_for_reconnect_returns_false_after_max_attempts() -> None:
    t = SSHTransport(host="1.2.3.4", user="agentworks")
    with (
        patch.object(t, "run", side_effect=SSHError("never")),
        patch("agentworks.transports.time.sleep"),
    ):
        assert wait_for_reconnect(t, max_attempts=2) is False


def test_wait_for_reconnect_accepts_any_transport() -> None:
    """``wait_for_reconnect`` is polymorphic over the Transport ABC, not
    SSH-specific. A LimaTransport / WSL2Transport / RemoteLimaTransport
    works equally well -- the contract is just ``target.run()``."""
    for t in [
        LimaTransport(vm_name="vm1"),
        WSL2Transport(distro_name="distro"),
        RemoteLimaTransport(vm_name="vm1", vm_host_ssh="h"),
    ]:
        assert isinstance(t, Transport)
        with patch.object(t, "run", return_value=SSHResult(returncode=0, stdout="", stderr="")):
            assert wait_for_reconnect(t) is True
