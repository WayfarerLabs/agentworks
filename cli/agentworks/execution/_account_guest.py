"""Fixed guest entry point for one private account-database lookup."""

from __future__ import annotations

import os
import sys

from ._account_protocol import (
    MAX_ACCOUNT_MESSAGE_BYTES,
    AccountFailure,
    AccountIdentityError,
    AccountRequest,
    AccountRequestError,
    decode_account_request,
    encode_account_failure,
    encode_account_identity,
)
from ._helper_identity import IdentityExpectation


def _read_request() -> AccountRequest:
    data = bytearray()
    while len(data) <= MAX_ACCOUNT_MESSAGE_BYTES:
        chunk = os.read(0, MAX_ACCOUNT_MESSAGE_BYTES + 1 - len(data))
        if not chunk:
            break
        data.extend(chunk)
    return decode_account_request(bytes(data))


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


def main(nonce: str) -> int:
    """Resolve one account without changing or introspecting process identity."""
    try:
        request = _read_request()
    except AccountRequestError as error:
        _write_all(encode_account_failure(nonce, error.failure))
        return 0
    if request.nonce != nonce:
        _write_all(encode_account_failure(nonce, AccountFailure.INVALID_REQUEST))
        return 0
    outcome = _lookup(request)
    if isinstance(outcome, AccountFailure):
        response = encode_account_failure(nonce, outcome)
    else:
        try:
            response = encode_account_identity(nonce, outcome)
        except AccountIdentityError as error:
            response = encode_account_failure(nonce, error.failure)
    _write_all(response)
    return 0
