"""Closed-schema checks for private destination account resolution."""

from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace

import pytest

from agentworks.execution import _account_guest
from agentworks.execution._account_protocol import (
    MAX_ACCOUNT_MESSAGE_BYTES,
    AccountFailure,
    AccountRequest,
    AccountRequestError,
    AccountResponseError,
    decode_account_request,
    decode_account_response,
    encode_account_failure,
    encode_account_identity,
    encode_account_request,
)
from agentworks.execution._helper_identity import IdentityExpectation

NONCE = "0123456789abcdef0123456789abcdef"


def _json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("ascii")


def _request(account: str = "workload") -> bytes:
    return encode_account_request(AccountRequest(NONCE, account))


def _run_guest(monkeypatch: pytest.MonkeyPatch, request: bytes) -> bytes:
    chunks = [request, b""]
    output = bytearray()

    def read(_descriptor: int, maximum: int) -> bytes:
        value = chunks.pop(0)
        return value[:maximum]

    def write(_descriptor: int, data: bytes) -> int:
        output.extend(data)
        return len(data)

    monkeypatch.setattr(os, "read", read)
    monkeypatch.setattr(os, "write", write)
    assert _account_guest.main(NONCE) == 0
    return bytes(output)


def test_request_is_canonical_ascii_and_round_trips_utf8_account() -> None:
    request = _request("worker-☃")

    assert request.isascii()
    assert decode_account_request(request) == AccountRequest(NONCE, "worker-☃")


@pytest.mark.parametrize(
    "content",
    [
        b'{"account":"workload","nonce":"0123456789abcdef0123456789abcdef",'
        b'"purpose":"resolve_account","version":1,"version":1}',
        _json(
            {
                "account": "workload",
                "extra": True,
                "nonce": NONCE,
                "purpose": "resolve_account",
                "version": 1,
            }
        ),
        b" " + _request(),
        _request() + b"\n",
        b"[]",
        b"\xff",
    ],
)
def test_request_rejects_duplicates_extras_noncanonical_trailing_and_wrong_types(content: bytes) -> None:
    with pytest.raises(AccountRequestError) as caught:
        decode_account_request(content)

    assert caught.value.failure is AccountFailure.INVALID_REQUEST


def test_request_rejects_oversize_separately() -> None:
    with pytest.raises(AccountRequestError) as caught:
        decode_account_request(b"x" * (MAX_ACCOUNT_MESSAGE_BYTES + 1))

    assert caught.value.failure is AccountFailure.OVERSIZED


def test_identity_response_contains_only_normalized_ids_and_groups() -> None:
    identity = IdentityExpectation(1001, 1002, (1002, 1003))
    encoded = encode_account_identity(NONCE, identity)

    response = decode_account_response(encoded, NONCE)
    value = json.loads(encoded)
    assert response.identity == identity
    assert response.failure is None
    assert set(value) == {"identity", "nonce", "status", "version"}
    assert set(value["identity"]) == {"egid", "euid", "groups"}


@pytest.mark.parametrize(
    "response",
    [
        b'{"failure":"missing","nonce":"0123456789abcdef0123456789abcdef","status":"refused","version":1,"version":1}',
        _json(
            {
                "account": "reflected-private-account",
                "failure": "missing",
                "nonce": NONCE,
                "status": "refused",
                "version": 1,
            }
        ),
        encode_account_failure(NONCE, AccountFailure.MISSING) + b"\n",
        _json(
            {
                "identity": {"egid": 1002, "euid": 1001, "groups": [1003, 1002]},
                "nonce": NONCE,
                "status": "identity",
                "version": 1,
            }
        ),
        _json(
            {
                "identity": {"egid": 1002, "euid": True, "groups": [1002]},
                "nonce": NONCE,
                "status": "identity",
                "version": 1,
            }
        ),
    ],
)
def test_response_rejects_duplicates_reflection_trailing_and_invalid_identity(response: bytes) -> None:
    with pytest.raises(AccountResponseError):
        decode_account_response(response, NONCE)


def test_response_rejects_wrong_nonce_and_oversized_group_list() -> None:
    with pytest.raises(AccountResponseError):
        decode_account_response(encode_account_failure(NONCE, AccountFailure.MISSING), "f" * 32)
    with pytest.raises(AccountResponseError):
        encode_account_identity(NONCE, IdentityExpectation(1, 2, tuple(range(2, 10_000))))


def test_guest_normalizes_groups_and_includes_primary_without_identity_introspection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = SimpleNamespace(pw_uid=1001, pw_gid=1002, pw_gecos="private", pw_dir="/private", pw_shell="/bin/zsh")
    monkeypatch.setitem(sys.modules, "pwd", SimpleNamespace(getpwnam=lambda _name: entry))
    monkeypatch.setattr(os, "getgrouplist", lambda _name, _gid: [1004, 1003, 1004])
    for name in ("geteuid", "getegid", "getgroups", "getresuid", "getresgid"):
        monkeypatch.setattr(
            os,
            name,
            lambda: (_ for _ in ()).throw(AssertionError("identity introspection")),
            raising=False,
        )

    response = decode_account_response(_run_guest(monkeypatch, _request()), NONCE)

    assert response.identity == IdentityExpectation(1001, 1002, (1002, 1003, 1004))


@pytest.mark.parametrize("failure", [KeyError("missing"), OSError("lookup"), RuntimeError("lookup")])
def test_guest_closes_account_lookup_errors(monkeypatch: pytest.MonkeyPatch, failure: Exception) -> None:
    def fail(_name: str) -> None:
        raise failure

    monkeypatch.setitem(sys.modules, "pwd", SimpleNamespace(getpwnam=fail))

    response = decode_account_response(_run_guest(monkeypatch, _request()), NONCE)

    expected = AccountFailure.MISSING if isinstance(failure, KeyError) else AccountFailure.LOOKUP
    assert response.failure is expected


def test_guest_closes_group_lookup_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    entry = SimpleNamespace(pw_uid=1001, pw_gid=1002)
    monkeypatch.setitem(sys.modules, "pwd", SimpleNamespace(getpwnam=lambda _name: entry))
    monkeypatch.setattr(os, "getgrouplist", lambda _name, _gid: (_ for _ in ()).throw(OSError("private")))

    response = decode_account_response(_run_guest(monkeypatch, _request()), NONCE)

    assert response.failure is AccountFailure.LOOKUP


@pytest.mark.parametrize(
    ("content", "platform", "failure"),
    [
        (b"{}", sys.platform, AccountFailure.INVALID_REQUEST),
        (b"x" * (MAX_ACCOUNT_MESSAGE_BYTES + 1), sys.platform, AccountFailure.OVERSIZED),
        (_request(), "win32", AccountFailure.RUNTIME),
    ],
)
def test_guest_returns_closed_request_and_runtime_refusals(
    monkeypatch: pytest.MonkeyPatch,
    content: bytes,
    platform: str,
    failure: AccountFailure,
) -> None:
    monkeypatch.setattr(sys, "platform", platform)

    response = decode_account_response(_run_guest(monkeypatch, content), NONCE)

    assert response.failure is failure
