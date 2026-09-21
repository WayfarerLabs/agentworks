"""Strict wire fragments for receipt-backed scratch ownership."""

from __future__ import annotations

from ._scratch import ReadyScratchReference, ScratchReference
from ._scratch_receipt import (
    _RECEIPT_BUILD_MODE,
    _RECEIPT_MODE,
    ScratchCleanupDebt,
    ScratchOwnership,
    ScratchReceiptContext,
    _Identity,
    scratch_name,
)

_MAX_LENGTH = (1 << 63) - 1
_MAX_IDENTITY_NUMBER = (1 << 64) - 1
_MIN_TIME_NS = -(1 << 63)
_MAX_TIME_NS = (1 << 63) - 1
_LOWER_HEX = frozenset("0123456789abcdef")
_REFERENCE_FIELDS = frozenset({"artifact_gid", "data", "directory", "length", "parent", "receipt"})
_READY_FIELDS = frozenset({"changed_ns", "digest", "modified_ns", "reference"})
_DEBT_FIELDS = frozenset({"artifact_gid", "data", "directory", "parent", "receipt", "receipt_state"})
_CREATING_RECEIPT = "creating_or_final"
_FINAL_RECEIPT = "final"


class ScratchWireError(ValueError):
    """A scratch wire fragment violated its closed schema."""

    def __init__(self) -> None:
        super().__init__("invalid scratch wire fragment")


def _bounded_integer(value: object, maximum: int) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise ScratchWireError
    return value


def _encode_identity(identity: _Identity) -> dict[str, int]:
    return {"device": identity.device, "inode": identity.inode}


def _decode_identity(value: object) -> _Identity:
    if type(value) is not dict or set(value) != {"device", "inode"}:
        raise ScratchWireError
    return _Identity(
        _bounded_integer(value["device"], _MAX_IDENTITY_NUMBER),
        _bounded_integer(value["inode"], _MAX_IDENTITY_NUMBER),
    )


def _encode_optional_identity(identity: _Identity | None) -> dict[str, int] | None:
    return None if identity is None else _encode_identity(identity)


def _decode_optional_identity(value: object) -> _Identity | None:
    return None if value is None else _decode_identity(value)


def encode_scratch_reference(
    reference: ScratchReference,
) -> dict[str, object]:
    """Encode one active reference without exporting a path or receipt body."""
    ownership = reference._ownership
    return {
        "artifact_gid": ownership._gid,
        "data": _encode_identity(ownership._data),
        "directory": _encode_identity(ownership._directory),
        "length": ownership._length,
        "parent": _encode_identity(ownership._parent),
        "receipt": _encode_identity(ownership._receipt),
    }


def decode_scratch_reference(
    value: object,
    token: bytes,
    context: ScratchReceiptContext,
) -> ScratchReference:
    """Decode syntax only; each live scratch operation validates the receipt."""
    try:
        scratch_name(token)
    except ValueError:
        raise ScratchWireError from None
    if not isinstance(context, ScratchReceiptContext) or type(value) is not dict or set(value) != _REFERENCE_FIELDS:
        raise ScratchWireError
    ownership = ScratchOwnership(
        bytes(token),
        context,
        _decode_identity(value["parent"]),
        _decode_identity(value["directory"]),
        _decode_identity(value["data"]),
        _bounded_integer(value["artifact_gid"], _MAX_IDENTITY_NUMBER),
        _bounded_integer(value["length"], _MAX_LENGTH),
        _decode_identity(value["receipt"]),
    )
    return ScratchReference(ownership)


def encode_ready_scratch_reference(ready: ReadyScratchReference) -> dict[str, object]:
    """Encode a content-ready active reference without exporting scratch paths."""
    return {
        "changed_ns": ready._changed_ns,
        "digest": ready._digest.hex(),
        "modified_ns": ready._modified_ns,
        "reference": encode_scratch_reference(ready._reference),
    }


def decode_ready_scratch_reference(
    value: object,
    token: bytes,
    context: ScratchReceiptContext,
) -> ReadyScratchReference:
    """Decode syntax for one active ready reference under an external context."""
    if type(value) is not dict or set(value) != _READY_FIELDS:
        raise ScratchWireError
    digest = value["digest"]
    if type(digest) is not str or len(digest) != 64 or any(character not in _LOWER_HEX for character in digest):
        raise ScratchWireError
    return ReadyScratchReference(
        decode_scratch_reference(value["reference"], token, context),
        bytes.fromhex(digest),
        _bounded_time(value["modified_ns"]),
        _bounded_time(value["changed_ns"]),
    )


def _bounded_time(value: object) -> int:
    if type(value) is not int or not _MIN_TIME_NS <= value <= _MAX_TIME_NS:
        raise ScratchWireError
    return value


def encode_cleanup_debt(
    debt: ScratchCleanupDebt,
) -> dict[str, object]:
    """Encode known cleanup facts without allowing the body to select a name."""
    if debt._receipt_modes == (_RECEIPT_MODE,):
        receipt_state = _FINAL_RECEIPT
    elif debt._receipt_modes == (_RECEIPT_BUILD_MODE, _RECEIPT_MODE):
        receipt_state = _CREATING_RECEIPT
    else:
        raise ScratchWireError
    return {
        "artifact_gid": debt._gid,
        "data": _encode_optional_identity(debt._object),
        "directory": _encode_optional_identity(debt._directory),
        "parent": _encode_optional_identity(debt._parent),
        "receipt": _encode_optional_identity(debt._receipt),
        "receipt_state": receipt_state,
    }


def decode_cleanup_debt(
    value: object,
    token: bytes,
    context: ScratchReceiptContext,
) -> ScratchCleanupDebt:
    """Decode exact cleanup facts while deriving all path authority externally."""
    try:
        name = scratch_name(token)
    except ValueError:
        raise ScratchWireError from None
    if not isinstance(context, ScratchReceiptContext) or type(value) is not dict or set(value) != _DEBT_FIELDS:
        raise ScratchWireError
    state = value["receipt_state"]
    if state == _FINAL_RECEIPT:
        modes: tuple[int, ...] = (_RECEIPT_MODE,)
    elif state == _CREATING_RECEIPT:
        modes = (_RECEIPT_BUILD_MODE, _RECEIPT_MODE)
    else:
        raise ScratchWireError
    return ScratchCleanupDebt(
        name,
        _decode_optional_identity(value["parent"]),
        _decode_optional_identity(value["directory"]),
        _decode_optional_identity(value["data"]),
        _decode_optional_identity(value["receipt"]),
        modes,
        context.identity.euid,
        _bounded_integer(value["artifact_gid"], _MAX_IDENTITY_NUMBER),
    )
