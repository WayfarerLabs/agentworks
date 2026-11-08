"""Provisioning's join, address read and settle over a fake root channel."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from agentworks.capabilities.vm_platform.base import parse_tailnet_address
from agentworks.capabilities.vm_platform.public_ssh import KeyRefused, PublicSSH
from agentworks.capabilities.vm_platform.tailscale_join import (
    ADDRESS_READS,
    CLOUD_INIT_RECOVERABLE_STATUS,
    DEFAULT_READINESS_COMMAND,
    JOIN_FRAGMENT,
    LOGOUT_SCRIPT,
    READINESS_WAIT_SECONDS,
    complete_cloud_bootstrap,
    join_tailscale,
    read_tailnet_address,
    settle_tailnet_address,
)
from agentworks.errors import ConnectivityError, ProvisioningError
from agentworks.execution.errors import TrustMismatch
from agentworks.execution.root_script import ChannelUnreachable, RawRun
from agentworks.execution.trust import TrustBinding, TrustMode
from tests._root_channel_fakes import ADDRESS, Answer, FakeGuest, answer_by_payload

if TYPE_CHECKING:
    from collections.abc import Callable

    from tests.conftest import CapturedOutput

KEY = "tskey-auth-join-'swordfish"
NODE = "lab-vm1"
READ = "tailscale ip -4"
LOGOUT = LOGOUT_SCRIPT
HINT = "Run it again."


class _Clock:
    """The cloud wait's clock: it moves only when the wait sleeps."""

    def __init__(self) -> None:
        self.now = 0.0

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    monkeypatch.setattr("agentworks.capabilities.vm_platform.tailscale_join.time.sleep", lambda seconds: None)
    clock = _Clock()
    monkeypatch.setattr("agentworks.capabilities.vm_platform.public_ssh.time.monotonic", lambda: clock.now)
    monkeypatch.setattr("agentworks.capabilities.vm_platform.public_ssh.time.sleep", clock.sleep)
    return clock


@pytest.fixture
def cloud(monkeypatch: pytest.MonkeyPatch) -> Callable[[FakeGuest], PublicSSH]:
    """A cloud VM's channel whose scripts the fake guest answers."""

    def channel(guest: FakeGuest) -> PublicSSH:
        target = PublicSSH("203.0.113.7", "agw", Path("/k"), TrustBinding(TrustMode.VM, "vm1"))
        monkeypatch.setattr(target, "run_as_root", guest.run_as_root)
        return target

    return channel


def reads_failing(failure: Answer, *, logout: Answer | None = None, succeed_on: int | None = None) -> FakeGuest:
    """A guest whose address reads fail with ``failure``, until read number ``succeed_on`` if given."""

    def respond(payload: str, seen: int) -> Answer:
        if payload == READ:
            return Answer(stdout=f"{ADDRESS}\n".encode()) if seen + 1 == succeed_on else failure
        if payload == LOGOUT and logout is not None:
            return logout
        return Answer()

    return FakeGuest(respond)


def holds_key(script: bytes) -> bool:
    return KEY.encode() in script or shlex.quote(KEY).encode() in script


def assert_key_free(error: BaseException) -> None:
    current: BaseException | None = error
    while current is not None:
        assert KEY not in str(current)
        assert KEY not in repr(current)
        current = current.__cause__ or current.__context__


# -- parse_tailnet_address ---------------------------------------------------------------


@pytest.mark.parametrize("text", [" 100.64.0.9\n", "10.20.30.40"])
def test_an_ipv4_address_parses_to_its_canonical_text(text: str) -> None:
    assert parse_tailnet_address(text) == text.strip()


@pytest.mark.parametrize(
    "value",
    [
        "",
        "not-an-address",
        "100.64.0.9\n100.64.0.10",
        "100.64.0.9 rm -rf",
        1684275209,
        None,
        "fd7a:115c:a1e0::1",  # IPv6: the read is tailscale ip -4
        "fe80::1%x y",  # a scoped IPv6 address with a space in its scope
        "127.0.0.1",  # loopback
        "0.0.0.0",  # unspecified
        "224.0.0.1",  # multicast
        "169.254.10.1",  # link-local
        "240.0.0.1",  # reserved
    ],
)
def test_anything_but_one_assignable_ipv4_address_is_refused(value: object) -> None:
    with pytest.raises(ValueError):
        parse_tailnet_address(value)


# -- The read --------------------------------------------------------------------------------


def test_the_first_read_that_parses_is_the_address() -> None:
    guest = FakeGuest()

    assert read_tailnet_address(guest.run_as_root, node=NODE) == ADDRESS
    assert guest.payloads == [READ]


def test_the_last_read_can_still_succeed() -> None:
    guest = reads_failing(Answer(status=1), succeed_on=ADDRESS_READS)

    assert read_tailnet_address(guest.run_as_root, node=NODE) == ADDRESS
    assert guest.payloads == [READ] * ADDRESS_READS


@pytest.mark.parametrize(
    "failure",
    [
        Answer(stdout=b"Tailscale is stopped.\n"),
        Answer(status=1),
        Answer(status=None),
        Answer(raises=ConnectivityError("the channel dropped")),
    ],
    ids=["unparseable", "non-zero", "unknown", "channel-raised"],
)
def test_each_failed_read_is_retried_then_exhaustion_names_the_node(failure: Answer) -> None:
    guest = reads_failing(failure)

    with pytest.raises(ConnectivityError, match=NODE):
        read_tailnet_address(guest.run_as_root, node=NODE)

    assert guest.payloads == [READ] * ADDRESS_READS  # never a logout


def test_a_host_key_mismatch_propagates_from_the_first_read() -> None:
    guest = reads_failing(Answer(raises=TrustMismatch("vm1")))

    with pytest.raises(TrustMismatch):
        read_tailnet_address(guest.run_as_root, node=NODE)

    assert guest.payloads == [READ]


# -- The settle ------------------------------------------------------------------------------


def test_a_settle_that_reads_the_address_never_logs_out() -> None:
    guest = reads_failing(Answer(status=1), succeed_on=2)

    assert settle_tailnet_address(guest.run_as_root, node=NODE, rerun_hint=HINT) == ADDRESS
    assert guest.count(LOGOUT) == 0


def test_exhaustion_logs_out_once_and_gives_the_rerun_hint() -> None:
    guest = reads_failing(Answer(status=1))

    with pytest.raises(ConnectivityError) as caught:
        settle_tailnet_address(guest.run_as_root, node=NODE, rerun_hint=HINT)

    assert guest.payloads == [READ] * ADDRESS_READS + [LOGOUT]
    assert caught.value.hint == HINT
    assert isinstance(caught.value.__cause__, ConnectivityError)


def test_the_caller_s_log_records_every_read_and_the_logout() -> None:
    guest = reads_failing(Answer(status=1))
    records: list[object] = []

    with pytest.raises(ConnectivityError):
        settle_tailnet_address(
            guest.run_as_root, node=NODE, rerun_hint=HINT, log=lambda request, result: records.append(result)
        )

    assert len(records) == ADDRESS_READS + 1 == len(guest.scripts)


def test_a_logout_that_ran_and_failed_names_the_node_for_the_admin_console() -> None:
    guest = reads_failing(Answer(status=1), logout=Answer(status=1))

    with pytest.raises(ConnectivityError) as caught:
        settle_tailnet_address(guest.run_as_root, node=NODE, rerun_hint=HINT)

    assert guest.count(LOGOUT) == 1
    assert caught.value.hint != HINT
    assert NODE in (caught.value.hint or "")


@pytest.mark.parametrize(
    "logout", [Answer(status=None), Answer(raises=ConnectivityError("dropped"))], ids=["unknown", "channel-raised"]
)
def test_a_logout_that_did_not_run_to_an_outcome_does_not_advise_removing_the_node(logout: Answer) -> None:
    """Nothing says the node is stuck: removing it could take a healthy node off the tailnet."""
    guest = reads_failing(Answer(status=1), logout=logout)

    with pytest.raises(ConnectivityError) as caught:
        settle_tailnet_address(guest.run_as_root, node=NODE, rerun_hint=HINT)

    assert guest.count(LOGOUT) == 1
    assert caught.value.hint == HINT


def test_an_unreachable_channel_ends_the_reads_at_once() -> None:
    """The channel already waited for the VM; reading again would wait again for nothing."""
    guest = reads_failing(Answer(raises=ChannelUnreachable("VM 'vm1' did not answer")))

    with pytest.raises(ChannelUnreachable):
        read_tailnet_address(guest.run_as_root, node=NODE)

    assert guest.payloads == [READ]


def test_a_settle_over_an_unreachable_channel_neither_logs_out_nor_advises_removing_the_node() -> None:
    guest = reads_failing(Answer(raises=ChannelUnreachable("VM 'vm1' did not answer")))

    with pytest.raises(ChannelUnreachable) as caught:
        settle_tailnet_address(guest.run_as_root, node=NODE, rerun_hint=HINT)

    assert guest.payloads == [READ]
    assert caught.value.hint == HINT


# -- The join --------------------------------------------------------------------------------


def test_a_join_whose_channel_never_reached_the_vm_did_not_join() -> None:
    """Nothing was dispatched, so there is no node to remove: the channel's own guidance stands."""
    unreachable = ChannelUnreachable("the VM did not answer, so no script was dispatched", hint="Start the VM.")
    guest = FakeGuest(lambda payload, seen: Answer(raises=unreachable))

    with pytest.raises(ProvisioningError) as caught:
        join_tailscale(guest.run_as_root, KEY, node=NODE)

    assert caught.value.__cause__ is unreachable
    assert caught.value.hint == unreachable.hint
    assert_key_free(caught.value)


def test_a_join_whose_channel_failed_after_dispatch_may_have_joined() -> None:
    guest = FakeGuest(lambda payload, seen: Answer(raises=ConnectivityError("the connection dropped")))

    with pytest.raises(ProvisioningError) as caught:
        join_tailscale(guest.run_as_root, KEY, node=NODE)

    assert NODE in (caught.value.hint or "")
    assert_key_free(caught.value)


def test_the_key_reaches_only_the_join_script() -> None:
    guest = FakeGuest()

    join_tailscale(guest.run_as_root, KEY, node=NODE)

    (script,) = guest.scripts
    assert guest.payloads == [JOIN_FRAGMENT]
    assert f"AGW_SECRET={shlex.quote(KEY)}".encode() in script


@pytest.mark.parametrize(
    "answer",
    [Answer(status=1), Answer(status=None), Answer(raises=ConnectivityError("dispatch failed"))],
    ids=["non-zero", "unknown", "channel-raised"],
)
def test_a_failed_join_raises_once_without_the_key(answer: Answer) -> None:
    guest = FakeGuest(lambda payload, seen: answer)

    with pytest.raises(ProvisioningError) as caught:
        join_tailscale(guest.run_as_root, KEY, node=NODE)

    assert guest.payloads == [JOIN_FRAGMENT]
    assert NODE in str(caught.value)
    assert_key_free(caught.value)


def test_a_refused_join_quotes_tailscale_s_reason_with_the_key_redacted() -> None:
    """An invalid auth key said only 'exited with status 1' before; Tailscale's own stderr is the reason."""
    reason = f"backend error: invalid key: unable to validate API key {KEY}"
    guest = FakeGuest(lambda payload, seen: Answer(status=1, stderr=f"\x1b[31m{reason}\n".encode()))

    with pytest.raises(ProvisioningError) as caught:
        join_tailscale(guest.run_as_root, KEY, node=NODE)

    message = str(caught.value)
    assert "backend error: invalid key: unable to validate API key [REDACTED]" in message
    assert "\x1b" not in message
    assert_key_free(caught.value)


# -- The cloud bootstrap ---------------------------------------------------------------------


def test_the_bootstrap_waits_joins_once_and_settles(cloud: Callable[[FakeGuest], PublicSSH]) -> None:
    guest = FakeGuest()

    address = complete_cloud_bootstrap(cloud(guest), KEY, node=NODE, readiness_command="ready")

    assert address == ADDRESS
    assert guest.payloads == ["true", "ready", JOIN_FRAGMENT, READ]
    assert [holds_key(script) for script in guest.scripts] == [False, False, True, False]


def test_the_readiness_wait_retries_a_refused_key_and_any_probe_that_does_not_complete(
    cloud: Callable[[FakeGuest], PublicSSH],
) -> None:
    refusals = [Answer(raises=KeyRefused("VM 'vm1' refused the configured SSH key")), Answer(status=None)]

    def respond(payload: str, seen: int) -> Answer:
        if payload == "true" and seen < len(refusals):
            return refusals[seen]
        return Answer(stdout=f"{ADDRESS}\n".encode()) if payload == READ else Answer()

    guest = FakeGuest(respond)

    complete_cloud_bootstrap(cloud(guest), KEY, node=NODE)

    assert guest.count("true") == len(refusals) + 1
    assert guest.joins == 1


def test_readiness_stops_at_its_bound_without_sending_the_key_and_keeps_the_refusal_hint(
    cloud: Callable[[FakeGuest], PublicSSH], clock: _Clock
) -> None:
    refused = KeyRefused("VM 'vm1' refused the configured SSH key", hint="Load the key into the agent.")
    guest = FakeGuest(lambda payload, seen: Answer(raises=refused))

    with pytest.raises(ProvisioningError) as caught:
        complete_cloud_bootstrap(cloud(guest), KEY, node=NODE)

    assert set(guest.payloads) == {"true"}
    assert clock.now >= READINESS_WAIT_SECONDS
    assert caught.value.hint == refused.hint


def test_a_readiness_command_whose_channel_fails_keeps_the_channel_hint(
    cloud: Callable[[FakeGuest], PublicSSH],
) -> None:
    dropped = ConnectivityError("the channel dropped", hint="Check the route.")
    guest = FakeGuest(lambda payload, seen: Answer(raises=dropped) if payload == "ready" else Answer())

    with pytest.raises(ProvisioningError) as caught:
        complete_cloud_bootstrap(cloud(guest), KEY, node=NODE, readiness_command="ready")

    assert caught.value.hint == dropped.hint
    assert guest.joins == 0


def test_an_admin_without_passwordless_sudo_fails_the_create_with_create_s_own_guidance(
    cloud: Callable[[FakeGuest], PublicSSH], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rollback removes the VM, so the refusal's own advice (fix sudo on the VM, or recreate it) must not show."""
    target = cloud(FakeGuest())
    probes: list[bytes] = []

    def refused(script: bytes, deadline: float) -> RawRun:
        probes.append(script)
        return RawRun(b"", b"sudo: a password is required\n")

    monkeypatch.setattr(target, "run_as_root", refused)

    with pytest.raises(ProvisioningError) as caught:
        complete_cloud_bootstrap(target, KEY, node=NODE)

    assert len(probes) == 1
    assert caught.value.__cause__ is None
    assert caught.value.hint


def test_a_host_key_mismatch_ends_readiness_at_once(cloud: Callable[[FakeGuest], PublicSSH]) -> None:
    guest = FakeGuest(lambda payload, seen: Answer(raises=TrustMismatch("vm1")))

    with pytest.raises(TrustMismatch):
        complete_cloud_bootstrap(cloud(guest), KEY, node=NODE)

    assert guest.payloads == ["true"]


@pytest.mark.parametrize("answer", [Answer(status=1), Answer(status=None), Answer(raises=ConnectivityError("x"))])
def test_a_failed_readiness_command_raises_before_the_join(
    cloud: Callable[[FakeGuest], PublicSSH], answer: Answer
) -> None:
    guest = FakeGuest(lambda payload, seen: answer if payload == "ready" else Answer())

    with pytest.raises(ProvisioningError):
        complete_cloud_bootstrap(cloud(guest), KEY, node=NODE, readiness_command="ready")

    assert guest.payloads == ["true", "ready"]


def test_a_failed_cloud_init_s_status_and_reason_reach_the_error(cloud: Callable[[FakeGuest], PublicSSH]) -> None:
    """The create's rollback removes the VM, so what cloud-init said is kept in the error before it goes."""
    reported = b"status: error\ndetail: DataSourceEc2Local\nerrors:\n\t- (scripts_user) failed\n"
    failed = Answer(status=1, stdout=reported)
    guest = FakeGuest(answer_by_payload(answers={DEFAULT_READINESS_COMMAND: failed}))

    with pytest.raises(ProvisioningError) as caught:
        complete_cloud_bootstrap(cloud(guest), KEY, node=NODE)

    assert "exit 1" in str(caught.value)
    assert "(scripts_user) failed" in str(caught.value)
    assert JOIN_FRAGMENT not in guest.payloads


def test_cloud_init_s_recoverable_status_warns_and_the_bootstrap_carries_on(
    cloud: Callable[[FakeGuest], PublicSSH], captured_output: CapturedOutput
) -> None:
    """cloud-init finished with recoverable errors, so the VM is up: warn and join, as Proxmox's wait does."""
    recoverable = Answer(status=CLOUD_INIT_RECOVERABLE_STATUS)
    guest = FakeGuest(answer_by_payload(answers={DEFAULT_READINESS_COMMAND: recoverable}))

    address = complete_cloud_bootstrap(cloud(guest), KEY, node=NODE)

    assert address == ADDRESS
    assert guest.payloads == ["true", DEFAULT_READINESS_COMMAND, JOIN_FRAGMENT, READ]
    assert len(captured_output.warnings) == 1


def test_another_readiness_command_s_status_2_is_a_failure(cloud: Callable[[FakeGuest], PublicSSH]) -> None:
    """Status 2 means recoverable errors only for cloud-init; GCE's marker wait, say, did not complete."""
    guest = FakeGuest(
        lambda payload, seen: Answer(status=CLOUD_INIT_RECOVERABLE_STATUS) if payload == "ready" else Answer()
    )

    with pytest.raises(ProvisioningError):
        complete_cloud_bootstrap(cloud(guest), KEY, node=NODE, readiness_command="ready")

    assert guest.payloads == ["true", "ready"]


def test_exhausted_reads_after_the_join_log_out_without_joining_again(
    cloud: Callable[[FakeGuest], PublicSSH],
) -> None:
    guest = reads_failing(Answer(status=None))

    with pytest.raises(ConnectivityError) as caught:
        complete_cloud_bootstrap(cloud(guest), KEY, node=NODE)

    assert guest.joins == 1
    assert guest.count(LOGOUT) == 1
    assert_key_free(caught.value)
