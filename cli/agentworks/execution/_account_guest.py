"""Fixed guest entry point for private account-database lookups."""

from __future__ import annotations

import os
import sys

from ._account_protocol import (
    MAX_ACCOUNT_MESSAGE_BYTES,
    AccountFailure,
    AccountIdentityError,
    AccountRequest,
    AccountRequestError,
    FileOwnership,
    FileOwnershipFailure,
    FileOwnershipRecordError,
    FileOwnershipRequest,
    decode_account_lookup_request,
    encode_account_failure,
    encode_account_identity,
    encode_file_ownership_failure,
    encode_file_ownership_success,
)
from ._helper_identity import IdentityExpectation


def _read_request() -> AccountRequest | FileOwnershipRequest:
    data = bytearray()
    while len(data) <= MAX_ACCOUNT_MESSAGE_BYTES:
        chunk = os.read(0, MAX_ACCOUNT_MESSAGE_BYTES + 1 - len(data))
        if not chunk:
            break
        data.extend(chunk)
    return decode_account_lookup_request(bytes(data))


def _write_all(data: bytes) -> None:
    offset = 0
    while offset < len(data):
        written = os.write(1, data[offset:])
        if written <= 0:
            raise OSError("account response write made no progress")
        offset += written


def _lookup(request: AccountRequest) -> IdentityExpectation | AccountFailure:
    if sys.platform not in ("linux", "darwin"):
        return AccountFailure.RUNTIME
    try:
        import pwd
    except ImportError:
        return AccountFailure.RUNTIME
    try:
        entry = pwd.getpwnam(request.account)
    except KeyError:
        return AccountFailure.MISSING
    except Exception:
        return AccountFailure.LOOKUP
    try:
        groups = tuple(sorted(set(os.getgrouplist(request.account, entry.pw_gid)) | {entry.pw_gid}))
        return IdentityExpectation(entry.pw_uid, entry.pw_gid, groups)
    except Exception:
        return AccountFailure.LOOKUP


def _lookup_file_ownership(request: FileOwnershipRequest) -> FileOwnership | FileOwnershipFailure:
    if sys.platform not in ("linux", "darwin"):
        return FileOwnershipFailure.RUNTIME
    try:
        import grp
        import pwd
    except ImportError:
        return FileOwnershipFailure.RUNTIME
    try:
        owner_entry = pwd.getpwnam(request.owner)
    except KeyError:
        return FileOwnershipFailure.MISSING_OWNER
    except Exception:
        return FileOwnershipFailure.LOOKUP
    try:
        group_entry = grp.getgrnam(request.group)
    except KeyError:
        return FileOwnershipFailure.MISSING_GROUP
    except Exception:
        return FileOwnershipFailure.LOOKUP
    try:
        return FileOwnership(owner_entry.pw_uid, group_entry.gr_gid)
    except Exception:
        return FileOwnershipFailure.LOOKUP


def main(nonce: str) -> int:
    """Perform one read-only lookup without changing or introspecting process identity."""
    try:
        request = _read_request()
    except AccountRequestError as error:
        _write_all(encode_account_failure(nonce, error.failure))
        return 0
    if request.nonce != nonce:
        if isinstance(request, FileOwnershipRequest):
            response = encode_file_ownership_failure(nonce, FileOwnershipFailure.INVALID_REQUEST)
        else:
            response = encode_account_failure(nonce, AccountFailure.INVALID_REQUEST)
        _write_all(response)
        return 0
    if isinstance(request, FileOwnershipRequest):
        file_outcome = _lookup_file_ownership(request)
        if isinstance(file_outcome, FileOwnershipFailure):
            response = encode_file_ownership_failure(nonce, file_outcome)
        else:
            try:
                response = encode_file_ownership_success(nonce, file_outcome)
            except FileOwnershipRecordError as error:
                response = encode_file_ownership_failure(nonce, error.failure)
    else:
        account_outcome = _lookup(request)
        if isinstance(account_outcome, AccountFailure):
            response = encode_account_failure(nonce, account_outcome)
        else:
            try:
                response = encode_account_identity(nonce, account_outcome)
            except AccountIdentityError as error:
                response = encode_account_failure(nonce, error.failure)
    _write_all(response)
    return 0
