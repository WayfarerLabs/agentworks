"""Closed-schema checks for private destination account resolution."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from types import SimpleNamespace

import pytest

from agentworks.execution import _account_guest
from agentworks.execution._account_protocol import (
    MAX_ACCOUNT_MESSAGE_BYTES,
    AccountFailure,
    AccountRequest,
    AccountRequestError,
    AccountResponseError,
    FileOwnership,
    FileOwnershipFailure,
    FileOwnershipRequest,
    FileOwnershipRequestError,
    FileOwnershipResponseError,
    decode_account_lookup_request,
    decode_account_response,
    decode_file_ownership_response,
    encode_account_failure,
    encode_account_identity,
    encode_account_request,
    encode_file_ownership_failure,
    encode_file_ownership_request,
    encode_file_ownership_success,
)
from agentworks.execution._helper_identity import IdentityExpectation

NONCE = "0123456789abcdef0123456789abcdef"


def _json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("ascii")


def _request(account: str = "workload") -> bytes:
    return encode_account_request(AccountRequest(NONCE, account))


def _ownership_request(owner: str = "workload", group: str = "tmux-agent-access") -> bytes:
    return encode_file_ownership_request(FileOwnershipRequest(NONCE, owner, group))


def _exception_details(error: BaseException) -> str:
    pending = [error]
    seen: set[int] = set()
    details: list[object] = []
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        details.extend(current.args)
        details.append(getattr(current, "object", None))
        details.append(getattr(current, "doc", None))
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
    return repr(details)


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
    assert decode_account_lookup_request(request) == AccountRequest(NONCE, "worker-☃")


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
        decode_account_lookup_request(content)

    assert caught.value.failure is AccountFailure.INVALID_REQUEST


def test_request_rejects_oversize_separately() -> None:
    with pytest.raises(AccountRequestError) as caught:
        decode_account_lookup_request(b"x" * (MAX_ACCOUNT_MESSAGE_BYTES + 1))

    assert caught.value.failure is AccountFailure.OVERSIZED


def test_file_ownership_request_is_canonical_ascii_and_round_trips_utf8_names() -> None:
    request = _ownership_request("worker-☃", "access-☃")

    assert request.isascii()
    assert decode_account_lookup_request(request) == FileOwnershipRequest(
        NONCE,
        "worker-☃",
        "access-☃",
    )


def test_fixed_guest_request_dispatch_preserves_both_concrete_wires() -> None:
    assert decode_account_lookup_request(_request()) == AccountRequest(NONCE, "workload")
    assert decode_account_lookup_request(_ownership_request()) == FileOwnershipRequest(
        NONCE,
        "workload",
        "tmux-agent-access",
    )
    malformed = json.loads(_ownership_request())
    malformed.pop("group")
    with pytest.raises(AccountRequestError) as raised:
        decode_account_lookup_request(_json(malformed))
    assert raised.value.failure is AccountFailure.INVALID_REQUEST


@pytest.mark.parametrize(
    ("canary", "invoke", "error_type"),
    [
        (
            "account-json-doc-canary",
            lambda: decode_account_lookup_request(b'{"account":"account-json-doc-canary"'),
            AccountRequestError,
        ),
        (
            "account-json-bytes-canary",
            lambda: decode_account_lookup_request(b"\xffaccount-json-bytes-canary"),
            AccountRequestError,
        ),
        (
            "ownership-name-canary",
            lambda: encode_file_ownership_request(FileOwnershipRequest(NONCE, "\ud800ownership-name-canary", "group")),
            FileOwnershipRequestError,
        ),
        (
            "account-failure-canary",
            lambda: decode_account_response(
                _json(
                    {
                        "failure": "account-failure-canary",
                        "nonce": NONCE,
                        "status": "refused",
                        "version": 1,
                    }
                ),
                NONCE,
            ),
            AccountResponseError,
        ),
        (
            "ownership-failure-canary",
            lambda: decode_file_ownership_response(
                _json(
                    {
                        "failure": "ownership-failure-canary",
                        "nonce": NONCE,
                        "status": "file_ownership_refused",
                        "version": 1,
                    }
                ),
                NONCE,
            ),
            FileOwnershipResponseError,
        ),
    ],
)
def test_closed_account_errors_do_not_retain_sensitive_values_in_exception_chains(
    canary: str,
    invoke: Callable[[], object],
    error_type: type[BaseException],
) -> None:
    with pytest.raises(error_type) as raised:
        invoke()

    assert canary not in _exception_details(raised.value)


@pytest.mark.parametrize(
    "content",
    [
        b'{"group":"access","nonce":"0123456789abcdef0123456789abcdef",'
        b'"owner":"workload","purpose":"resolve_file_ownership","version":1,"version":1}',
        _json(
            {
                "extra": True,
                "group": "access",
                "nonce": NONCE,
                "owner": "workload",
                "purpose": "resolve_file_ownership",
                "version": 1,
            }
        ),
        b" " + _ownership_request(),
        _ownership_request() + b"\n",
        b"[]",
        b"\xff",
        _json(
            {
                "group": "access",
                "nonce": NONCE,
                "owner": "workload",
                "purpose": True,
                "version": 1,
            }
        ),
    ],
)
def test_file_ownership_request_rejects_malformed_or_wrong_schema(content: bytes) -> None:
    with pytest.raises(AccountRequestError) as caught:
        decode_account_lookup_request(content)

    assert caught.value.failure is AccountFailure.INVALID_REQUEST


def test_file_ownership_request_rejects_oversize_separately() -> None:
    with pytest.raises(AccountRequestError) as caught:
        decode_account_lookup_request(b"x" * (MAX_ACCOUNT_MESSAGE_BYTES + 1))

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


def test_file_ownership_response_contains_only_numeric_pair() -> None:
    encoded = encode_file_ownership_success(NONCE, FileOwnership(1001, 2002))

    response = decode_file_ownership_response(encoded, NONCE)
    value = json.loads(encoded)
    assert response.ownership == FileOwnership(1001, 2002)
    assert response.failure is None
    assert set(value) == {"nonce", "ownership", "status", "version"}
    assert set(value["ownership"]) == {"gid", "uid"}


@pytest.mark.parametrize(
    "response",
    [
        b'{"failure":"missing_owner","nonce":"0123456789abcdef0123456789abcdef",'
        b'"status":"file_ownership_refused","version":1,"version":1}',
        encode_file_ownership_failure(NONCE, FileOwnershipFailure.MISSING_OWNER) + b"\n",
        _json(
            {
                "failure": True,
                "nonce": NONCE,
                "status": "file_ownership_refused",
                "version": 1,
            }
        ),
        _json(
            {
                "failure": "missing_owner",
                "nonce": NONCE,
                "status": True,
                "version": 1,
            }
        ),
        _json(
            {
                "nonce": NONCE,
                "ownership": {"gid": 2, "uid": True},
                "status": "file_ownership",
                "version": 1,
            }
        ),
        _json(
            {
                "nonce": NONCE,
                "ownership": {"gid": -1, "uid": 1},
                "status": "file_ownership",
                "version": 1,
            }
        ),
        _json(
            {
                "nonce": NONCE,
                "ownership": {"gid": 1, "uid": 2**32},
                "status": "file_ownership",
                "version": 1,
            }
        ),
    ],
)
def test_file_ownership_response_rejects_malformed_and_hostile_fields(response: bytes) -> None:
    with pytest.raises(FileOwnershipResponseError):
        decode_file_ownership_response(response, NONCE)


def test_file_ownership_response_binds_nonce_and_result_kind() -> None:
    with pytest.raises(FileOwnershipResponseError):
        decode_file_ownership_response(
            encode_file_ownership_failure(NONCE, FileOwnershipFailure.MISSING_GROUP),
            "f" * 32,
        )
    with pytest.raises(FileOwnershipResponseError):
        decode_file_ownership_response(
            encode_account_identity(NONCE, IdentityExpectation(1001, 1002, (1002,))),
            NONCE,
        )
    with pytest.raises(FileOwnershipResponseError):
        decode_file_ownership_response(
            encode_account_failure(NONCE, AccountFailure.MISSING),
            NONCE,
        )
    with pytest.raises(AccountResponseError):
        decode_account_response(
            encode_file_ownership_success(NONCE, FileOwnership(1001, 1002)),
            NONCE,
        )
    with pytest.raises(AccountResponseError):
        decode_account_response(
            encode_file_ownership_failure(NONCE, FileOwnershipFailure.MISSING_OWNER),
            NONCE,
        )


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
    ("requested_group", "resolved_gid"),
    [("workload-primary", 1002), ("tmux-agent-access", 2003)],
)
def test_guest_resolves_exact_primary_or_non_primary_group_without_membership_or_identity_effects(
    monkeypatch: pytest.MonkeyPatch,
    requested_group: str,
    resolved_gid: int,
) -> None:
    owner_entry = SimpleNamespace(
        pw_uid=1001,
        pw_gid=1002,
        pw_passwd="private",
        pw_gecos="private",
        pw_dir="/private",
        pw_shell="/bin/private",
    )
    group_entry = SimpleNamespace(gr_gid=resolved_gid, gr_mem=["private-member"])

    def group_lookup(name: str) -> SimpleNamespace:
        assert name == requested_group
        return group_entry

    monkeypatch.setitem(sys.modules, "pwd", SimpleNamespace(getpwnam=lambda _name: owner_entry))
    monkeypatch.setitem(sys.modules, "grp", SimpleNamespace(getgrnam=group_lookup))
    monkeypatch.setattr(
        os,
        "getgrouplist",
        lambda *_args: (_ for _ in ()).throw(AssertionError("membership lookup")),
    )
    for name in ("geteuid", "getegid", "getgroups", "getresuid", "getresgid", "setuid", "setgid"):
        monkeypatch.setattr(
            os,
            name,
            lambda *_args: (_ for _ in ()).throw(AssertionError("identity access")),
            raising=False,
        )

    encoded = _run_guest(monkeypatch, _ownership_request("workload", requested_group))
    response = decode_file_ownership_response(encoded, NONCE)

    assert response.ownership == FileOwnership(1001, resolved_gid)
    assert b"workload" not in encoded
    assert requested_group.encode() not in encoded


@pytest.mark.parametrize(
    ("owner_failure", "group_failure", "expected"),
    [
        (KeyError("private-owner"), None, FileOwnershipFailure.MISSING_OWNER),
        (OSError("private-owner"), None, FileOwnershipFailure.LOOKUP),
        (None, KeyError("private-group"), FileOwnershipFailure.MISSING_GROUP),
        (None, RuntimeError("private-group"), FileOwnershipFailure.LOOKUP),
    ],
)
def test_guest_closes_file_ownership_lookup_errors(
    monkeypatch: pytest.MonkeyPatch,
    owner_failure: Exception | None,
    group_failure: Exception | None,
    expected: FileOwnershipFailure,
) -> None:
    def owner_lookup(_name: str) -> SimpleNamespace:
        if owner_failure is not None:
            raise owner_failure
        return SimpleNamespace(pw_uid=1001)

    def group_lookup(_name: str) -> SimpleNamespace:
        if group_failure is not None:
            raise group_failure
        return SimpleNamespace(gr_gid=2003)

    monkeypatch.setitem(sys.modules, "pwd", SimpleNamespace(getpwnam=owner_lookup))
    monkeypatch.setitem(sys.modules, "grp", SimpleNamespace(getgrnam=group_lookup))

    encoded = _run_guest(monkeypatch, _ownership_request())
    response = decode_file_ownership_response(encoded, NONCE)

    assert response.failure is expected
    assert b"private" not in encoded


@pytest.mark.parametrize(("uid", "gid"), [(True, 2), (1, -1), (2**32, 2)])
def test_guest_closes_invalid_external_file_ownership_records(
    monkeypatch: pytest.MonkeyPatch,
    uid: object,
    gid: object,
) -> None:
    monkeypatch.setitem(sys.modules, "pwd", SimpleNamespace(getpwnam=lambda _name: SimpleNamespace(pw_uid=uid)))
    monkeypatch.setitem(sys.modules, "grp", SimpleNamespace(getgrnam=lambda _name: SimpleNamespace(gr_gid=gid)))

    response = decode_file_ownership_response(
        _run_guest(monkeypatch, _ownership_request()),
        NONCE,
    )

    assert response.failure is FileOwnershipFailure.LOOKUP


def test_guest_closes_file_ownership_runtime_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")

    response = decode_file_ownership_response(
        _run_guest(monkeypatch, _ownership_request()),
        NONCE,
    )

    assert response.failure is FileOwnershipFailure.RUNTIME


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
