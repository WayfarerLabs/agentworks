"""Closed stdlib-only protocol for private destination account lookup."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ._helper_identity import IdentityExpectation

MAX_ACCOUNT_MESSAGE_BYTES = 32_768
_MAX_ID = 2**32 - 1
_LOWER_HEX = frozenset("0123456789abcdef")
_REQUEST_FIELDS = frozenset({"account", "nonce", "purpose", "version"})
_REFUSAL_FIELDS = frozenset({"failure", "nonce", "status", "version"})
_SUCCESS_FIELDS = frozenset({"identity", "nonce", "status", "version"})


class AccountFailure(StrEnum):
    """Closed lookup refusals that disclose no account-database fields."""

    MISSING = "missing"
    LOOKUP = "lookup"
    RUNTIME = "runtime"
    OVERSIZED = "oversized"
    INVALID_REQUEST = "invalid_request"


class AccountRequestError(ValueError):
    """An account request violated the closed request schema."""

    def __init__(self, failure: AccountFailure) -> None:
        self.failure = failure
        super().__init__(failure.value)


class AccountResponseError(ValueError):
    """An account response violated the closed response schema."""


class AccountIdentityError(AccountResponseError):
    """A discovered identity cannot be represented by the response schema."""

    def __init__(self, failure: AccountFailure) -> None:
        self.failure = failure
        super().__init__(failure.value)


@dataclass(frozen=True, slots=True, repr=False)
class AccountRequest:
    nonce: str
    account: str


@dataclass(frozen=True, slots=True)
class AccountResponse:
    identity: IdentityExpectation | None = None
    failure: AccountFailure | None = None


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("ascii")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _valid_nonce(value: object) -> bool:
    return type(value) is str and len(value) == 32 and all(character in _LOWER_HEX for character in value)


def _invalid_request() -> AccountRequestError:
    return AccountRequestError(AccountFailure.INVALID_REQUEST)


def _account_text(value: object) -> str:
    if type(value) is not str or not value or "\0" in value:
        raise _invalid_request()
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise _invalid_request() from None
    return value


def _identity(value: object) -> IdentityExpectation:
    if type(value) is not dict or set(value) != {"egid", "euid", "groups"}:
        raise AccountResponseError("invalid account identity")
    euid = value["euid"]
    egid = value["egid"]
    groups = value["groups"]
    if (
        type(euid) is not int
        or not 0 <= euid <= _MAX_ID
        or type(egid) is not int
        or not 0 <= egid <= _MAX_ID
        or type(groups) is not list
        or not groups
        or any(type(group) is not int or not 0 <= group <= _MAX_ID for group in groups)
        or groups != sorted(set(groups))
        or egid not in groups
    ):
        raise AccountResponseError("invalid account identity")
    return IdentityExpectation(euid, egid, tuple(groups))


def encode_account_request(request: AccountRequest) -> bytes:
    """Encode trusted account data and enforce the guest request schema."""
    value = {
        "account": _account_text(request.account),
        "nonce": request.nonce,
        "purpose": "resolve_account",
        "version": 1,
    }
    encoded = _json_bytes(value)
    if len(encoded) > MAX_ACCOUNT_MESSAGE_BYTES:
        raise AccountRequestError(AccountFailure.OVERSIZED)
    return encoded


def decode_account_request(data: bytes) -> AccountRequest:
    """Validate one untrusted canonical request from finite stdin."""
    if type(data) is not bytes:
        raise _invalid_request()
    if len(data) > MAX_ACCOUNT_MESSAGE_BYTES:
        raise AccountRequestError(AccountFailure.OVERSIZED)
    try:
        value = json.loads(data.decode("ascii"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise _invalid_request() from None
    if type(value) is not dict or set(value) != _REQUEST_FIELDS or _json_bytes(value) != data:
        raise _invalid_request()
    if (
        type(value["version"]) is not int
        or value["version"] != 1
        or value["purpose"] != "resolve_account"
        or not _valid_nonce(value["nonce"])
    ):
        raise _invalid_request()
    return AccountRequest(value["nonce"], _account_text(value["account"]))


def encode_account_identity(nonce: str, identity: IdentityExpectation) -> bytes:
    """Encode one normalized identity, rejecting an oversized group list."""
    value = {
        "identity": {"egid": identity.egid, "euid": identity.euid, "groups": list(identity.groups)},
        "nonce": nonce,
        "status": "identity",
        "version": 1,
    }
    encoded = _json_bytes(value)
    if len(encoded) > MAX_ACCOUNT_MESSAGE_BYTES:
        raise AccountIdentityError(AccountFailure.OVERSIZED)
    try:
        _identity(value["identity"])
    except AccountResponseError as error:
        raise AccountIdentityError(AccountFailure.LOOKUP) from error
    return encoded


def encode_account_failure(nonce: str, failure: AccountFailure) -> bytes:
    value = {"failure": failure.value, "nonce": nonce, "status": "refused", "version": 1}
    return _json_bytes(value)


def decode_account_response(data: bytes, nonce: str) -> AccountResponse:
    """Validate one nonce-bound canonical response from untrusted stdout."""
    if type(data) is not bytes or len(data) > MAX_ACCOUNT_MESSAGE_BYTES:
        raise AccountResponseError("invalid account response")
    try:
        value = json.loads(data.decode("ascii"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise AccountResponseError("invalid account response") from None
    if type(value) is not dict or _json_bytes(value) != data:
        raise AccountResponseError("invalid account response")
    if (
        type(value.get("version")) is not int
        or value["version"] != 1
        or value.get("nonce") != nonce
        or not _valid_nonce(value.get("nonce"))
    ):
        raise AccountResponseError("invalid account response")
    if set(value) == _SUCCESS_FIELDS and value["status"] == "identity":
        return AccountResponse(identity=_identity(value["identity"]))
    if set(value) == _REFUSAL_FIELDS and value["status"] == "refused":
        try:
            failure = AccountFailure(value["failure"])
        except (TypeError, ValueError):
            raise AccountResponseError("invalid account response") from None
        return AccountResponse(failure=failure)
    raise AccountResponseError("invalid account response")
