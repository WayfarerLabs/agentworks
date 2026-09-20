"""Strict wire fragments for receipt-backed scratch ownership."""

from __future__ import annotations

from ._scratch import ScratchReference
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
_REFERENCE_FIELDS = frozenset({"artifact_gid", "data", "directory", "length", "parent", "receipt"})
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
    token: bytes,
    context: ScratchReceiptContext,
) -> dict[str, object]:
    """Encode one active reference without exporting a path or receipt body."""
    if not isinstance(reference, ScratchReference):
        raise ScratchWireError
    ownership = reference._ownership
    if ownership._token != token or ownership._context != context:
        raise ScratchWireError
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


def encode_cleanup_debt(
    debt: ScratchCleanupDebt,
    token: bytes,
    context: ScratchReceiptContext,
) -> dict[str, object]:
    """Encode known cleanup facts without allowing the body to select a name."""
    if not isinstance(debt, ScratchCleanupDebt):
        raise ScratchWireError
    try:
        expected_name = scratch_name(token)
    except ValueError:
        raise ScratchWireError from None
    if debt._name != expected_name or debt._uid != context.identity.euid:
        raise ScratchWireError
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
