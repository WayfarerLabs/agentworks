"""Descriptor-relative validation and release of one terminal managed run."""

from __future__ import annotations

import hashlib
import os
import re
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING

from . import _managed_job_wire as wire
from ._managed_job_store import FactName, RequestAsset, StopAsset, StoreError, Stream, _open_leaf, _read_all
from ._managed_observation_protocol import ManagedObservationError, checked_fact, checked_launch

if TYPE_CHECKING:
    from ._managed_job_store import ManagedJobStore

_FACT_STAGE = re.compile(r"\.fact-stage-[0-9a-f]{32}\Z")
_REQUEST_STAGE = re.compile(r"\.request-stage-[0-9a-f]{32}\Z")
_TERMINAL = (FactName.BOUNDARY_EMPTY, FactName.STDOUT_END, FactName.STDERR_END)


@dataclass(frozen=True)
class _Leaf:
    name: str
    device: int
    inode: int
    links: int
    data: bytes | None

    @property
    def key(self) -> tuple[int, int]:
        return self.device, self.inode


def _inventory(directory: int, owner: int) -> dict[str, _Leaf]:
    """Validate the entire run before permitting any unlink."""
    result: dict[str, _Leaf] = {}
    for name in os.listdir(directory):
        fact = name in FactName._value2member_map_
        request = name in RequestAsset._value2member_map_ or name == StopAsset.REQUEST.value
        stage_fact = _FACT_STAGE.fullmatch(name) is not None
        stage_request = _REQUEST_STAGE.fullmatch(name) is not None
        spool = name in Stream._value2member_map_
        if not (fact or request or stage_fact or stage_request or spool or name == "disposal"):
            raise StoreError("unknown run object")
        fd = _open_leaf(directory, name, 0o600 if spool else 0o400, owner, links=(1, 2, 3))
        if fd is None:
            raise StoreError("run object changed during validation")
        try:
            info = os.fstat(fd)
            data = _read_all(fd, wire.MAX_MANAGED_JOB_FACT_BYTES) if fact or name == "disposal" else None
            result[name] = _Leaf(name, info.st_dev, info.st_ino, info.st_nlink, data)
        finally:
            os.close(fd)
    groups: dict[tuple[int, int], list[_Leaf]] = {}
    for leaf in result.values():
        groups.setdefault(leaf.key, []).append(leaf)
    for leaves in groups.values():
        names = {leaf.name for leaf in leaves}
        count = leaves[0].links
        if any(leaf.links != count for leaf in leaves) or count != len(leaves):
            raise StoreError("unexplained hardlink")
        stages = [name for name in names if _FACT_STAGE.fullmatch(name) or _REQUEST_STAGE.fullmatch(name)]
        if len(stages) > 1:
            raise StoreError("ambiguous stage links")
        if "disposal" in names:
            if names - {"disposal", "launch"} and not (
                len(names - {"disposal", "launch"}) == 1
                and all(_FACT_STAGE.fullmatch(name) for name in names - {"disposal", "launch"})
            ):
                raise StoreError("unsafe disposal links")
        elif len(names) > 1:
            finals = names - set(stages)
            if len(stages) != 1 or len(finals) != 1:
                raise StoreError("unsafe publication links")
            final = next(iter(finals))
            stage = stages[0]
            if not (
                (_FACT_STAGE.fullmatch(stage) and final in FactName._value2member_map_)
                or (
                    _REQUEST_STAGE.fullmatch(stage)
                    and (final in RequestAsset._value2member_map_ or final == "request-stop")
                )
            ):
                raise StoreError("wrong stage link")
    return result


def _validate_facts(inventory: dict[str, _Leaf], expected: bytes) -> bool:
    launch = inventory.get("launch")
    receipt = inventory.get("disposal")
    if receipt is not None and receipt.data != expected:
        raise StoreError("disposal receipt mismatch")
    if launch is None:
        if receipt is not None and len(inventory) == 1 and receipt.links == 1:
            return True
        if receipt is None:
            raise StoreError("missing launch and receipt")
        # A committed retry can observe launch already removed.
    elif launch.data != expected:
        raise StoreError("launch binding mismatch")
    if receipt is not None and launch is not None and launch.key != receipt.key:
        raise StoreError("disposal receipt inode mismatch")
    if launch is None and receipt is None:
        raise StoreError("missing launch and receipt")
    digest = hashlib.sha256(expected).hexdigest()
    for name in FactName:
        leaf = inventory.get(name.value)
        if leaf is None:
            continue
        try:
            fact = checked_fact(name, leaf.data, expected)  # type: ignore[arg-type]
        except ManagedObservationError:
            raise StoreError("invalid terminal fact") from None
        if name is not FactName.LAUNCH and fact["receipt_sha256"] != digest:
            raise StoreError("fact launch mismatch")
    return receipt is not None or all(name.value in inventory for name in _TERMINAL)


def dispose(store: ManagedJobStore, expected_launch: bytes) -> bool:
    """Commit a hard-link receipt, then remove only prevalidated fixed leaves."""
    try:
        launch = checked_launch(expected_launch)
    except ManagedObservationError:
        raise StoreError("invalid expected launch") from None
    if launch["run_id"] != store.run_id:
        raise StoreError("wrong run identity")
    directory = store._run_dir(create=False)
    if directory is None:
        raise StoreError("missing launch and receipt")
    try:
        inventory = _inventory(directory, store._owner_uid)
        if not _validate_facts(inventory, expected_launch):
            return False
        if "disposal" not in inventory:
            with suppress(FileExistsError):
                os.link("launch", "disposal", src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
            os.fsync(directory)
        inventory = _inventory(directory, store._owner_uid)
        if not _validate_facts(inventory, expected_launch) or "disposal" not in inventory:
            raise StoreError("disposal commitment changed")
        for name in inventory:
            if name == "disposal":
                continue
            with suppress(FileNotFoundError):
                os.unlink(name, dir_fd=directory)
        os.fsync(directory)
        final = _inventory(directory, store._owner_uid)
        success = (
            len(final) == 1
            and "disposal" in final
            and final["disposal"].links == 1
            and final["disposal"].data == expected_launch
        )
        if not success:
            raise StoreError("disposal cleanup incomplete")
        return True
    finally:
        os.close(directory)
