"""Closed control for loading one operation-owned prepared helper input."""

from __future__ import annotations

import json
from dataclasses import dataclass

from ._helper_identity import IdentityExpectation, decode_identity
from ._scratch import ScratchReference
from ._scratch_receipt import ScratchOperation, ScratchReceiptContext, scratch_name
from ._scratch_wire import ScratchWireError, decode_scratch_reference, encode_scratch_reference

MAX_ENVELOPE_BYTES = 4096
MAX_REMAINING_NS = 2**63 - 1
_HEX = frozenset("0123456789abcdef")
_FIELDS = {"version", "nonce", "identity", "token", "reference", "sha256", "remaining_ns"}


class StagedHelperError(ValueError):
    """Invalid staged-helper control without retaining its contents."""


@dataclass(frozen=True, slots=True, repr=False)
class StagedHelperRequest:
    nonce: str
    identity: IdentityExpectation
    token: bytes
    reference: ScratchReference
    sha256: bytes
    remaining_ns: int

    def __post_init__(self) -> None:
        try:
            _hex(self.nonce, 32)
            if (
                type(self.identity) is not IdentityExpectation
                or type(self.token) is not bytes
                or type(self.reference) is not ScratchReference
                or type(self.sha256) is not bytes
                or len(self.sha256) != 32
                or type(self.remaining_ns) is not int
                or not 0 < self.remaining_ns <= MAX_REMAINING_NS
            ):
                raise ValueError
            decode_identity(
                {"euid": self.identity.euid, "egid": self.identity.egid, "groups": list(self.identity.groups)}
            )
            scratch_name(self.token)
            context = ScratchReceiptContext(ScratchOperation.STAGE, self.identity)
            ownership = self.reference._ownership
            if ownership._token != self.token or ownership._context != context:
                raise ValueError
            decode_scratch_reference(encode_scratch_reference(self.reference), self.token, context)
        except (ValueError, TypeError, AttributeError, OverflowError):
            raise StagedHelperError("invalid staged helper control") from None


def _hex(value: object, length: int) -> str:
    if type(value) is not str or len(value) != length or any(char not in _HEX for char in value):
        raise StagedHelperError("invalid staged helper control")
    return value


def _json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("ascii")


def encode_request(request: StagedHelperRequest) -> bytes:
    """Validate externally supplied private control before encoding it."""
    try:
        if type(request) is not StagedHelperRequest:
            raise ValueError
        data = _json(
            {
                "version": 1,
                "nonce": request.nonce,
                "identity": {
                    "euid": request.identity.euid,
                    "egid": request.identity.egid,
                    "groups": list(request.identity.groups),
                },
                "token": request.token.hex(),
                "reference": encode_scratch_reference(request.reference),
                "sha256": request.sha256.hex(),
                "remaining_ns": request.remaining_ns,
            }
        )
        decode_request(data)
    except (ValueError, TypeError, AttributeError, OverflowError):
        raise StagedHelperError("invalid staged helper control") from None
    return data


def decode_request(data: bytes) -> StagedHelperRequest:
    """Decode syntax only; the loader independently validates live ownership."""
    try:
        if type(data) is not bytes or len(data) > MAX_ENVELOPE_BYTES:
            raise ValueError
        value = json.loads(data.decode("ascii"))
        if (
            type(value) is not dict
            or set(value) != _FIELDS
            or type(value["version"]) is not int
            or value["version"] != 1
            or _json(value) != data
            or type(value["remaining_ns"]) is not int
            or not 0 < value["remaining_ns"] <= MAX_REMAINING_NS
        ):
            raise ValueError
        nonce = _hex(value["nonce"], 32)
        identity = decode_identity(value["identity"])
        token = bytes.fromhex(_hex(value["token"], 32))
        reference = decode_scratch_reference(
            value["reference"], token, ScratchReceiptContext(ScratchOperation.STAGE, identity)
        )
        digest = bytes.fromhex(_hex(value["sha256"], 64))
        return StagedHelperRequest(nonce, identity, token, reference, digest, value["remaining_ns"])
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError, ScratchWireError):
        raise StagedHelperError("invalid staged helper control") from None
