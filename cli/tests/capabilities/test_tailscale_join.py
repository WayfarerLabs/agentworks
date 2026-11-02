"""Provisioning's join, address read and settle over a fake root channel."""

from __future__ import annotations

import shlex

import pytest

from agentworks.capabilities.vm_platform.base import parse_tailnet_address
from agentworks.capabilities.vm_platform.tailscale_join import (
    ADDRESS_READS,
    JOIN_FRAGMENT,
    READINESS_PROBES,
    EphemeralTailscaleBootstrap,
    join_tailscale,
    read_tailnet_address,
    settle_tailnet_address,
)
from agentworks.errors import ConnectivityError, ProvisioningError
from agentworks.execution.errors import TrustMismatch
from tests._root_channel_fakes import ADDRESS, Answer, FakeGuest

KEY = "tskey-auth-join-'swordfish"
NODE = "lab-vm1"
READ = "tailscale ip -4"
LOGOUT = "tailscale logout"
HINT = "Run it again."


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agentworks.capabilities.vm_platform.tailscale_join.time.sleep", lambda seconds: None)


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


@pytest.mark.parametrize(("text", "expected"), [(" 100.64.0.9\n", "100.64.0.9"), ("fd7a::0001", "fd7a::1")])
def test_an_address_parses_to_its_canonical_text(text: str, expected: str) -> None:
    assert parse_tailnet_address(text) == expected


@pytest.mark.parametrize("value", ["", "not-an-address", "100.64.0.9\n100.64.0.10", 1684275209, None])
def test_anything_but_one_address_is_refused(value: object) -> None:
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
    assert HINT in str(caught.value)
    assert isinstance(caught.value.__cause__, ConnectivityError)


@pytest.mark.parametrize("logout", [Answer(status=1), Answer(status=None), Answer(raises=ConnectivityError("dropped"))])
def test_a_failed_logout_names_the_node_for_the_admin_console(logout: Answer) -> None:
    guest = reads_failing(Answer(status=1), logout=logout)

    with pytest.raises(ConnectivityError) as caught:
        settle_tailnet_address(guest.run_as_root, node=NODE, rerun_hint=HINT)

    assert guest.count(LOGOUT) == 1
    assert HINT not in str(caught.value)
    assert f"'{NODE}'" in str(caught.value)


# -- The join --------------------------------------------------------------------------------


def test_the_key_reaches_only_the_join_script() -> None:
    guest = FakeGuest()

    join_tailscale(guest.run_as_root, KEY, node=NODE)

    (script,) = guest.scripts
    assert guest.payloads == [JOIN_FRAGMENT]
    assert f"AGW_SECRET={shlex.quote(KEY)}".encode() in script


@pytest.mark.parametrize(
    ("answer", "match"),
    [
        (Answer(status=1), "status 1"),
        (Answer(status=None), "remove it there"),
        (Answer(raises=ConnectivityError("dispatch failed")), "remove it there"),
    ],
    ids=["non-zero", "unknown", "channel-raised"],
)
def test_a_failed_join_raises_once_without_the_key(answer: Answer, match: str) -> None:
    guest = FakeGuest(lambda payload, seen: answer)

    with pytest.raises(ProvisioningError, match=match) as caught:
        join_tailscale(guest.run_as_root, KEY, node=NODE)

    assert guest.joins == 1
    assert_key_free(caught.value)


# -- The cloud bootstrap ---------------------------------------------------------------------


def test_the_bootstrap_waits_joins_once_and_settles() -> None:
    guest = FakeGuest()

    address = EphemeralTailscaleBootstrap(guest.run_as_root, readiness_command="ready").complete(KEY, node=NODE)

    assert address == ADDRESS
    assert guest.payloads == ["true", "ready", JOIN_FRAGMENT, READ]
    assert [holds_key(script) for script in guest.scripts] == [False, False, True, False]


def test_the_readiness_loop_retries_a_refused_key_and_any_probe_that_does_not_complete() -> None:
    refusals = [
        Answer(raises=ConnectivityError("VM 'vm1' refused the configured SSH key")),
        Answer(status=None),
        Answer(status=1),
    ]

    def respond(payload: str, seen: int) -> Answer:
        if payload == "true" and seen < len(refusals):
            return refusals[seen]
        return Answer(stdout=f"{ADDRESS}\n".encode()) if payload == READ else Answer()

    guest = FakeGuest(respond)

    EphemeralTailscaleBootstrap(guest.run_as_root).complete(KEY, node=NODE)

    assert guest.count("true") == len(refusals) + 1
    assert guest.joins == 1


def test_readiness_stops_at_its_bound_without_sending_the_key() -> None:
    guest = FakeGuest(lambda payload, seen: Answer(raises=ConnectivityError("refused")))

    with pytest.raises(ProvisioningError, match="did not answer"):
        EphemeralTailscaleBootstrap(guest.run_as_root).complete(KEY, node=NODE)

    assert guest.payloads == ["true"] * READINESS_PROBES


def test_a_host_key_mismatch_ends_readiness_at_once() -> None:
    guest = FakeGuest(lambda payload, seen: Answer(raises=TrustMismatch("vm1")))

    with pytest.raises(TrustMismatch):
        EphemeralTailscaleBootstrap(guest.run_as_root).complete(KEY, node=NODE)

    assert guest.payloads == ["true"]


@pytest.mark.parametrize("answer", [Answer(status=1), Answer(status=None), Answer(raises=ConnectivityError("x"))])
def test_a_failed_readiness_command_raises_before_the_join(answer: Answer) -> None:
    guest = FakeGuest(lambda payload, seen: answer if payload == "ready" else Answer())

    with pytest.raises(ProvisioningError, match="the label did not complete"):
        EphemeralTailscaleBootstrap(guest.run_as_root, readiness_command="ready", readiness_label="the label").complete(
            KEY, node=NODE
        )

    assert guest.payloads == ["true", "ready"]


def test_exhausted_reads_after_the_join_log_out_without_joining_again() -> None:
    guest = reads_failing(Answer(status=None))

    with pytest.raises(ConnectivityError) as caught:
        EphemeralTailscaleBootstrap(guest.run_as_root).complete(KEY, node=NODE)

    assert guest.joins == 1
    assert guest.count(LOGOUT) == 1
    assert_key_free(caught.value)
