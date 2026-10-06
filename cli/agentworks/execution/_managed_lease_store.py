"""Descriptor-relative mutable operation control in the protected run store."""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING
from uuid import uuid4

from ._managed_job_store import (
    _CREATE_FLAGS,
    StoreError,
    _open_leaf,
    _read_all,
    _refuse_disposal,
    _safe_stat,
    _write_all,
)
from ._managed_lease_wire import MAX_LEASE_BYTES, OperationLease, checked_lease, decode_lease, encode_lease

if TYPE_CHECKING:
    from ._managed_job_store import ManagedJobStore

LEASE_LEAF = "operation-lease"
LEASE_STAGE = re.compile(r"\.lease-stage-[0-9a-f]{32}\Z")


def read_lease(store: ManagedJobStore, expected_launch: bytes) -> OperationLease | None:
    directory = store._run_dir(create=False)
    if directory is None:
        return None
    try:
        fd = _open_leaf(directory, LEASE_LEAF, 0o400, store._owner_uid, links=(1,))
        if fd is None:
            return None
        try:
            return checked_lease(decode_lease(_read_all(fd, MAX_LEASE_BYTES)), expected_launch)
        finally:
            os.close(fd)
    finally:
        os.close(directory)


def publish_lease(store: ManagedJobStore, expected_launch: bytes, lease: OperationLease) -> None:
    """One publisher replaces one complete record; failures leave publication uncertain."""
    checked_lease(lease, expected_launch)
    if lease.run_id != store.run_id:
        raise StoreError("lease run mismatch")
    directory = store._run_dir(create=False)
    if directory is None:
        raise StoreError("missing run store")
    stage = ".lease-stage-" + uuid4().hex
    try:
        _refuse_disposal(directory)
        previous = read_lease(store, expected_launch)
        if previous is not None and lease.expires_ns <= previous.expires_ns:
            if previous == lease:
                return
            raise StoreError("lease expiry cannot move backward")
        fd = os.open(stage, _CREATE_FLAGS, 0o400, dir_fd=directory)
        try:
            os.fchmod(fd, 0o400)
            _safe_stat(fd, 0o400, store._owner_uid, links=(1,))
            _write_all(fd, encode_lease(lease))
            os.fsync(fd)
        finally:
            os.close(fd)
        _refuse_disposal(directory)
        os.replace(stage, LEASE_LEAF, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        os.close(directory)
