"""Packet-log observation refuses syntax drift and separates offers from acceptance."""

from __future__ import annotations

import pytest

from tests.execution.carriers.ssh.auth_offer_fixtures import KeyOffer, key_offers


def test_offer_observation_preserves_phase_algorithm_and_exact_wire_blob() -> None:
    log = (
        b"debug2: userauth_pubkey: valid user fixture querying public key ssh-ed25519 AAAA11== [preauth]\n"
        b"debug2: userauth_pubkey: valid user fixture attempting public key ssh-ed25519 AAAA11== [preauth]\n"
        b"debug2: userauth_pubkey: invalid user fixture querying public key ssh-ed25519-cert-v01@openssh.com BBBB22==\n"
        b"Accepted publickey for fixture from 127.0.0.1 port 1234 ssh2: ED25519 SHA256:unrelated\n"
    )
    assert key_offers(log) == [
        KeyOffer("querying", "ssh-ed25519", "AAAA11=="),
        KeyOffer("attempting", "ssh-ed25519", "AAAA11=="),
        KeyOffer("querying", "ssh-ed25519-cert-v01@openssh.com", "BBBB22=="),
    ]


@pytest.mark.parametrize("changed", [b"new-phase public key ssh-ed25519 AAAA", b"querying public key ssh-ed25519 @@@"])
def test_unknown_packet_log_syntax_does_not_become_absent_offers(changed: bytes) -> None:
    with pytest.raises(AssertionError):
        key_offers(b"debug2: userauth_pubkey: valid user fixture " + changed + b"\n")
