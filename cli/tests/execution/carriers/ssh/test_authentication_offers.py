"""Actual wire-key offers from owned installed SSH clients, servers and agents."""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from agentworks.execution.carrier import Capture, CarrierIO, Deadline, Dispatch, ExitStatus, PreparedInvocation
from agentworks.execution.carriers.ssh.client import SSHCarrier
from tests.execution.carriers.ssh.auth_offer_fixtures import key_offers, owned_agent, public_key
from tests.execution.carriers.ssh.enrollment_server import LocalSSH, enrollment_server

pytestmark = pytest.mark.integration


@pytest.fixture
def observed_server(tmp_path: Path) -> Iterator[tuple[LocalSSH, Path]]:
    log = tmp_path.resolve() / "server.log"
    with enrollment_server(tmp_path, "matching", log_path=log, log_level="DEBUG2") as server:
        yield server, log


def _execute(server: LocalSSH, **selection: object):
    connection = replace(server.connection, **selection)  # type: ignore[arg-type]
    return SSHCarrier(connection).execute(
        PreparedInvocation(("/bin/sh", "-c", "printf authenticated")),
        io=CarrierIO(output=Capture(1024)),
        deadline=Deadline.after(10),
    )


def _only_selected(log: Path, selected: tuple[str, str], offset: int = 0):
    offers = key_offers(log.read_bytes()[offset:])
    assert offers, "Owned server supplied no packet-level key observations"
    assert {(offer.algorithm, offer.blob) for offer in offers} == {selected}
    return offers


def _success(result) -> None:
    assert result.dispatch == Dispatch.SENT and result.completion == ExitStatus(0)
    assert result.failure is None and result.stdout.data == b"authenticated"


@pytest.mark.parametrize("authorized", [False, True])
def test_inherited_agent_cannot_offer_or_sign_without_explicit_selection(
    observed_server: tuple[LocalSSH, Path], monkeypatch: pytest.MonkeyPatch, authorized: bool
) -> None:
    server, log = observed_server
    identity = server.connection.identity_file
    other = identity.parent / "other-key"
    # An accidental fallback would authenticate successfully and is therefore visible.
    if not authorized:
        server.authorized.write_bytes(other.with_suffix(".pub").read_bytes())
    with owned_agent((other, identity)) as endpoint:
        monkeypatch.setenv("SSH_AUTH_SOCK", endpoint)
        result = _execute(server, identity_file=identity.with_suffix(".pub") if authorized else identity)
        assert result.local_status == 255 and result.completion is None
        offers = _only_selected(log, public_key(identity.with_suffix(".pub")))
        assert all(offer.phase == "querying" for offer in offers)


@pytest.mark.parametrize("authorized", [False, True])
def test_explicit_multikey_agent_can_sign_only_the_configured_public_identity(
    observed_server: tuple[LocalSSH, Path], authorized: bool
) -> None:
    server, log = observed_server
    identity = server.connection.identity_file
    other = identity.parent / "other-key"
    if not authorized:
        server.authorized.write_bytes(other.with_suffix(".pub").read_bytes())
    # A public-only IdentityFile cannot sign locally; successful signed authentication
    # requires the explicit agent. Its other authorized key still cannot be offered.
    with owned_agent((other, identity)) as endpoint:
        result = _execute(server, identity_file=identity.with_suffix(".pub"), agent_socket=endpoint)
        offers = _only_selected(log, public_key(identity.with_suffix(".pub")))
        if authorized:
            _success(result)
            assert any(offer.phase == "attempting" for offer in offers)
        else:
            assert result.local_status == 255 and result.completion is None
            assert all(offer.phase == "querying" for offer in offers)


def test_matching_sibling_is_offered_but_stale_sibling_refuses_before_authentication(
    observed_server: tuple[LocalSSH, Path],
) -> None:
    server, log = observed_server
    identity = server.connection.identity_file
    other = identity.parent / "other-key"
    with owned_agent((other, identity)) as endpoint:
        _success(_execute(server, agent_socket=endpoint))
        _only_selected(log, public_key(identity.with_suffix(".pub")))
        offset = log.stat().st_size
        identity.with_suffix(".pub").write_bytes(other.with_suffix(".pub").read_bytes())
        result = _execute(server, agent_socket=endpoint)
        assert result.dispatch == Dispatch.NOT_SENT and result.local_status is None
        assert result.completion is None and result.failure is not None
        assert key_offers(log.read_bytes()[offset:]) == []


def test_automatic_sibling_certificate_is_never_offered(
    observed_server: tuple[LocalSSH, Path],
) -> None:
    server, log = observed_server
    identity = server.connection.identity_file
    other = identity.parent / "other-key"
    with owned_agent((other, identity)) as endpoint:
        subprocess.run(
            [
                "ssh-keygen",
                "-q",
                "-s",
                str(other),
                "-I",
                "owned-user-certificate",
                "-n",
                server.connection.user,
                "-V",
                "-1m:+5m",
                str(identity.with_suffix(".pub")),
            ],
            check=True,
            timeout=10,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        certificate = public_key(identity.with_name(identity.name + "-cert.pub"))
        assert "-cert-" in certificate[0]
        _success(_execute(server, agent_socket=endpoint))
        _only_selected(log, public_key(identity.with_suffix(".pub")))
