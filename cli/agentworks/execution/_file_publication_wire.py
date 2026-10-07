"""Strict wire fragments for publication-stage cleanup ownership."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ._file_publication import PublicationCleanupDebt
from ._publication_receipt import (
    _RECORD_BUILD_MODE,
    _RECORD_MODE,
    PublicationStageCleanupDebt,
    PublicationStageOwnership,
    _Identity,
    publication_stage_name,
)

if TYPE_CHECKING:
    from ._scratch import ScratchReference

_MAX_IDENTITY_NUMBER = (1 << 64) - 1
_DEBT_FIELDS = frozenset({"kind", "parent", "record", "record_state", "stage", "stage_removed"})
_GENERIC_FIELDS = frozenset({"kind", "parent", "stage"})
_CREATING_RECORD = "creating_or_final"
_FINAL_RECORD = "final"


class FilePublicationWireError(ValueError):
    """A publication cleanup fragment violated its closed schema."""

    def __init__(self) -> None:
        super().__init__("invalid publication cleanup wire fragment")


@dataclass(frozen=True, slots=True, repr=False)
class BoundPublicationCleanupDebt:
    """Cleanup authority bound to its original scratch and destination parent."""

    _reference: ScratchReference
    _publication_parent: _Identity
    _debt: PublicationCleanupDebt | PublicationStageCleanupDebt


def _bounded_integer(value: object) -> int:
    if type(value) is not int or not 0 <= value <= _MAX_IDENTITY_NUMBER:
        raise FilePublicationWireError
    return value


def _encode_identity(identity: _Identity) -> dict[str, int]:
    return {"device": identity.device, "inode": identity.inode}


def _decode_identity(value: object) -> _Identity:
    if type(value) is not dict or set(value) != {"device", "inode"}:
        raise FilePublicationWireError
    return _Identity(_bounded_integer(value["device"]), _bounded_integer(value["inode"]))


def bind_publication_cleanup_debt(
    reference: ScratchReference,
    publication_parent: _Identity,
    debt: PublicationCleanupDebt | PublicationStageCleanupDebt,
) -> BoundPublicationCleanupDebt:
    """Bind primitive debt to the request facts that authorize one retry."""
    token = reference._ownership._token
    scratch_parent = reference._ownership._parent
    if publication_parent != _Identity(scratch_parent.device, scratch_parent.inode):
        raise FilePublicationWireError
    stage_name = publication_stage_name(token)
    if isinstance(debt, PublicationCleanupDebt):
        if debt.name != stage_name or debt.device is None or debt.inode is None:
            raise FilePublicationWireError
        if debt.device != publication_parent.device:
            raise FilePublicationWireError
    elif isinstance(debt, PublicationStageCleanupDebt):
        ownership = debt._ownership
        if (
            ownership._scratch_ownership != reference._ownership
            or ownership._publication_parent != publication_parent
            or ownership._stage_name != stage_name
        ):
            raise FilePublicationWireError
    else:
        raise FilePublicationWireError
    return BoundPublicationCleanupDebt(reference, publication_parent, debt)


def encode_publication_cleanup_debt(debt: BoundPublicationCleanupDebt) -> dict[str, object]:
    """Encode exact cleanup facts without exporting a caller-selected name."""
    if not isinstance(debt, BoundPublicationCleanupDebt):
        raise FilePublicationWireError
    bound = bind_publication_cleanup_debt(debt._reference, debt._publication_parent, debt._debt)
    cleanup = bound._debt
    if isinstance(cleanup, PublicationCleanupDebt):
        assert cleanup.device is not None and cleanup.inode is not None
        return {
            "kind": "generic",
            "parent": _encode_identity(bound._publication_parent),
            "stage": _encode_identity(_Identity(cleanup.device, cleanup.inode)),
        }
    ownership = cleanup._ownership
    if ownership._record_modes == (_RECORD_MODE,):
        record_state = _FINAL_RECORD
    elif ownership._record_modes == (_RECORD_BUILD_MODE, _RECORD_MODE):
        record_state = _CREATING_RECORD
    else:
        raise FilePublicationWireError
    return {
        "kind": "receipt",
        "parent": _encode_identity(ownership._publication_parent),
        "record": _encode_identity(ownership._record),
        "record_state": record_state,
        "stage": _encode_identity(ownership._stage),
        "stage_removed": cleanup._stage_removed,
    }


def decode_publication_cleanup_debt(
    value: object,
    reference: ScratchReference,
) -> BoundPublicationCleanupDebt:
    """Decode debt while deriving scratch and sibling authority externally."""
    if type(value) is not dict:
        raise FilePublicationWireError
    kind = value.get("kind")
    if kind == "generic" and set(value) == _GENERIC_FIELDS:
        parent = _decode_identity(value["parent"])
        stage = _decode_identity(value["stage"])
        debt: PublicationCleanupDebt | PublicationStageCleanupDebt = PublicationCleanupDebt(
            publication_stage_name(reference._ownership._token),
            stage.device,
            stage.inode,
        )
        return bind_publication_cleanup_debt(reference, parent, debt)
    if kind != "receipt" or set(value) != _DEBT_FIELDS or type(value["stage_removed"]) is not bool:
        raise FilePublicationWireError
    parent = _decode_identity(value["parent"])
    stage = _decode_identity(value["stage"])
    record = _decode_identity(value["record"])
    record_state = value["record_state"]
    if record_state == _FINAL_RECORD:
        record_modes: tuple[int, ...] = (_RECORD_MODE,)
    elif record_state == _CREATING_RECORD:
        record_modes = (_RECORD_BUILD_MODE, _RECORD_MODE)
    else:
        raise FilePublicationWireError
    if stage.device != parent.device or record.device != reference._ownership._directory.device:
        raise FilePublicationWireError
    ownership = PublicationStageOwnership(
        reference._ownership,
        parent,
        publication_stage_name(reference._ownership._token),
        stage,
        record,
        record_modes,
    )
    debt = PublicationStageCleanupDebt(ownership, value["stage_removed"])
    return bind_publication_cleanup_debt(reference, parent, debt)
