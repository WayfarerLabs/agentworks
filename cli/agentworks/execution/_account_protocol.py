"""Closed stdlib-only protocols for private account-database lookup."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ._helper_identity import IdentityExpectation, decode_identity

MAX_ACCOUNT_MESSAGE_BYTES = 32_768
_MAX_ID = 2**32 - 1
_LOWER_HEX = frozenset("0123456789abcdef")
_REQUEST_FIELDS = frozenset({"account", "nonce", "purpose", "version"})
_REFUSAL_FIELDS = frozenset({"failure", "nonce", "status", "version"})
_SUCCESS_FIELDS = frozenset({"identity", "nonce", "status", "version"})
_FILE_OWNERSHIP_REQUEST_FIELDS = frozenset({"group", "nonce", "owner", "purpose", "version"})
_FILE_OWNERSHIP_SUCCESS_FIELDS = frozenset({"nonce", "ownership", "status", "version"})


class AccountFailure(StrEnum):
    """Closed lookup refusals that disclose no account-database fields."""

    MISSING = "missing"
    LOOKUP = "lookup"
    RUNTIME = "runtime"
    OVERSIZED = "oversized"
    INVALID_REQUEST = "invalid_request"


class FileOwnershipFailure(StrEnum):
    """Closed file-ownership refusals that disclose no account-database fields."""

    MISSING_OWNER = "missing_owner"
    MISSING_GROUP = "missing_group"
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


class FileOwnershipRequestError(ValueError):
    """A file-ownership request violated the closed request schema."""

    def __init__(self, failure: FileOwnershipFailure) -> None:
        self.failure = failure
        super().__init__(failure.value)


class FileOwnershipResponseError(ValueError):
    """A file-ownership response violated the closed response schema."""


class FileOwnershipRecordError(FileOwnershipResponseError):
    """Discovered file ownership cannot be represented by the response schema."""

    def __init__(self, failure: FileOwnershipFailure) -> None:
        self.failure = failure
        super().__init__(failure.value)


class _MessageError(ValueError):
    def __init__(self, *, oversized: bool = False) -> None:
        self.oversized = oversized


@dataclass(frozen=True, slots=True, repr=False)
class AccountRequest:
    nonce: str
    account: str


@dataclass(frozen=True, slots=True)
class AccountResponse:
    identity: IdentityExpectation | None = None
    failure: AccountFailure | None = None


@dataclass(frozen=True, slots=True, repr=False)
class FileOwnershipRequest:
    nonce: str
    owner: str
    group: str


@dataclass(frozen=True, slots=True)
class FileOwnership:
    uid: int
    gid: int


@dataclass(frozen=True, slots=True)
class FileOwnershipResponse:
    ownership: FileOwnership | None = None
    failure: FileOwnershipFailure | None = None


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("ascii")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _decode_object(data: bytes) -> dict[str, Any]:
    if type(data) is not bytes:
        raise _MessageError
    if len(data) > MAX_ACCOUNT_MESSAGE_BYTES:
        raise _MessageError(oversized=True)
    failed = False
    value: Any = None
    try:
        value = json.loads(data.decode("ascii"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, TypeError, ValueError, RecursionError):
        failed = True
    if failed or type(value) is not dict:
        raise _MessageError
    failed = False
    canonical = b""
    try:
        canonical = _json_bytes(value)
    except (TypeError, ValueError, RecursionError):
        failed = True
    if failed or canonical != data:
        raise _MessageError
    return value


def _valid_nonce(value: object) -> bool:
    return type(value) is str and len(value) == 32 and all(character in _LOWER_HEX for character in value)


def _invalid_request() -> AccountRequestError:
    return AccountRequestError(AccountFailure.INVALID_REQUEST)


def _name_text(value: object) -> str:
    if type(value) is not str or not value or "\0" in value:
        raise ValueError
    failed = False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        failed = True
    if failed:
        raise ValueError("invalid name")
    return value


def _account_text(value: object) -> str:
    failed = False
    text = ""
    try:
        text = _name_text(value)
    except ValueError:
        failed = True
    if failed:
        raise _invalid_request()
    return text


def _file_ownership_text(value: object) -> str:
    failed = False
    text = ""
    try:
        text = _name_text(value)
    except ValueError:
        failed = True
    if failed:
        raise FileOwnershipRequestError(FileOwnershipFailure.INVALID_REQUEST)
    return text


def _valid_response_envelope(value: dict[str, Any], nonce: str) -> bool:
    return (
        type(value.get("version")) is int
        and value["version"] == 1
        and value.get("nonce") == nonce
        and _valid_nonce(value.get("nonce"))
    )


def _file_ownership(value: object) -> FileOwnership:
    if type(value) is not dict or set(value) != {"gid", "uid"}:
        raise FileOwnershipResponseError("invalid file ownership")
    uid = value["uid"]
    gid = value["gid"]
    if type(uid) is not int or not 0 <= uid <= _MAX_ID or type(gid) is not int or not 0 <= gid <= _MAX_ID:
        raise FileOwnershipResponseError("invalid file ownership")
    return FileOwnership(uid, gid)


def _identity(value: object) -> IdentityExpectation:
    failed = False
    identity: IdentityExpectation | None = None
    try:
        identity = decode_identity(value)
    except ValueError:
        failed = True
    if failed or identity is None:
        raise AccountResponseError("invalid account identity")
    return identity


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


def _account_request(value: dict[str, Any]) -> AccountRequest:
    if set(value) != _REQUEST_FIELDS:
        raise _invalid_request()
    if (
        type(value["version"]) is not int
        or value["version"] != 1
        or value["purpose"] != "resolve_account"
        or not _valid_nonce(value["nonce"])
    ):
        raise _invalid_request()
    return AccountRequest(value["nonce"], _account_text(value["account"]))


def decode_account_request(data: bytes) -> AccountRequest:
    """Validate one untrusted canonical request from finite stdin."""
    failed = False
    oversized = False
    value: dict[str, Any] = {}
    try:
        value = _decode_object(data)
    except _MessageError as error:
        failed = True
        oversized = error.oversized
    if failed:
        failure = AccountFailure.OVERSIZED if oversized else AccountFailure.INVALID_REQUEST
        raise AccountRequestError(failure)
    return _account_request(value)


def encode_file_ownership_request(request: FileOwnershipRequest) -> bytes:
    """Encode trusted owner/group names and enforce the guest request schema."""
    value = {
        "group": _file_ownership_text(request.group),
        "nonce": request.nonce,
        "owner": _file_ownership_text(request.owner),
        "purpose": "resolve_file_ownership",
        "version": 1,
    }
    encoded = _json_bytes(value)
    if len(encoded) > MAX_ACCOUNT_MESSAGE_BYTES:
        raise FileOwnershipRequestError(FileOwnershipFailure.OVERSIZED)
    return encoded


def _file_ownership_request(value: dict[str, Any]) -> FileOwnershipRequest:
    if (
        set(value) != _FILE_OWNERSHIP_REQUEST_FIELDS
        or type(value["version"]) is not int
        or value["version"] != 1
        or type(value["purpose"]) is not str
        or value["purpose"] != "resolve_file_ownership"
        or not _valid_nonce(value["nonce"])
    ):
        raise FileOwnershipRequestError(FileOwnershipFailure.INVALID_REQUEST)
    return FileOwnershipRequest(
        value["nonce"],
        _file_ownership_text(value["owner"]),
        _file_ownership_text(value["group"]),
    )


def decode_file_ownership_request(data: bytes) -> FileOwnershipRequest:
    """Validate one untrusted canonical file-ownership request from finite stdin."""
    failed = False
    oversized = False
    value: dict[str, Any] = {}
    try:
        value = _decode_object(data)
    except _MessageError as error:
        failed = True
        oversized = error.oversized
    if failed:
        failure = FileOwnershipFailure.OVERSIZED if oversized else FileOwnershipFailure.INVALID_REQUEST
        raise FileOwnershipRequestError(failure)
    return _file_ownership_request(value)


def decode_account_lookup_request(data: bytes) -> AccountRequest | FileOwnershipRequest:
    """Parse once and select one of the two fixed account-database operations."""
    failed = False
    oversized = False
    value: dict[str, Any] = {}
    try:
        value = _decode_object(data)
    except _MessageError as error:
        failed = True
        oversized = error.oversized
    if failed:
        failure = AccountFailure.OVERSIZED if oversized else AccountFailure.INVALID_REQUEST
        raise AccountRequestError(failure)
    if value.get("purpose") == "resolve_file_ownership":
        failed = False
        ownership_request: FileOwnershipRequest | None = None
        try:
            ownership_request = _file_ownership_request(value)
        except FileOwnershipRequestError:
            failed = True
        if failed or ownership_request is None:
            raise AccountRequestError(AccountFailure.INVALID_REQUEST)
        return ownership_request
    return _account_request(value)


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
    failed = False
    try:
        _identity(value["identity"])
    except AccountResponseError:
        failed = True
    if failed:
        raise AccountIdentityError(AccountFailure.LOOKUP)
    return encoded


def encode_account_failure(nonce: str, failure: AccountFailure) -> bytes:
    value = {"failure": failure.value, "nonce": nonce, "status": "refused", "version": 1}
    return _json_bytes(value)


def encode_file_ownership_success(nonce: str, ownership: FileOwnership) -> bytes:
    """Encode one owner/group ID pair obtained from the external account database."""
    value = {
        "nonce": nonce,
        "ownership": {"gid": ownership.gid, "uid": ownership.uid},
        "status": "file_ownership",
        "version": 1,
    }
    failed = False
    try:
        _file_ownership(value["ownership"])
    except FileOwnershipResponseError:
        failed = True
    if failed:
        raise FileOwnershipRecordError(FileOwnershipFailure.LOOKUP)
    return _json_bytes(value)


def encode_file_ownership_failure(nonce: str, failure: FileOwnershipFailure) -> bytes:
    value = {
        "failure": failure.value,
        "nonce": nonce,
        "status": "file_ownership_refused",
        "version": 1,
    }
    return _json_bytes(value)


def decode_account_response(data: bytes, nonce: str) -> AccountResponse:
    """Validate one nonce-bound canonical response from untrusted stdout."""
    failed = False
    value: dict[str, Any] = {}
    try:
        value = _decode_object(data)
    except _MessageError:
        failed = True
    if failed:
        raise AccountResponseError("invalid account response")
    if not _valid_response_envelope(value, nonce):
        raise AccountResponseError("invalid account response")
    if set(value) == _SUCCESS_FIELDS and value["status"] == "identity":
        return AccountResponse(identity=_identity(value["identity"]))
    if set(value) == _REFUSAL_FIELDS and value["status"] == "refused":
        failed = False
        failure = AccountFailure.INVALID_REQUEST
        try:
            failure = AccountFailure(value["failure"])
        except (TypeError, ValueError):
            failed = True
        if failed:
            raise AccountResponseError("invalid account response")
        return AccountResponse(failure=failure)
    raise AccountResponseError("invalid account response")


def decode_file_ownership_response(data: bytes, nonce: str) -> FileOwnershipResponse:
    """Validate one nonce- and kind-bound file-ownership response."""
    failed = False
    value: dict[str, Any] = {}
    try:
        value = _decode_object(data)
    except _MessageError:
        failed = True
    if failed:
        raise FileOwnershipResponseError("invalid file ownership response")
    if not _valid_response_envelope(value, nonce):
        raise FileOwnershipResponseError("invalid file ownership response")
    if (
        set(value) == _FILE_OWNERSHIP_SUCCESS_FIELDS
        and type(value["status"]) is str
        and value["status"] == "file_ownership"
    ):
        return FileOwnershipResponse(ownership=_file_ownership(value["ownership"]))
    if set(value) == _REFUSAL_FIELDS and type(value["status"]) is str and value["status"] == "file_ownership_refused":
        failed = False
        failure = FileOwnershipFailure.INVALID_REQUEST
        try:
            failure = FileOwnershipFailure(value["failure"])
        except (TypeError, ValueError):
            failed = True
        if failed:
            raise FileOwnershipResponseError("invalid file ownership response")
        return FileOwnershipResponse(failure=failure)
    raise FileOwnershipResponseError("invalid file ownership response")
