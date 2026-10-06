"""Descriptor-relative mutable operation control in the protected run store."""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING
from uuid import uuid4

from ._managed_job_store import (
    _CREATE_FLAGS,
    FactName,
    StoreError,
    _acquire_mutation_gate,
    _open_leaf,
    _read_all,
    _refuse_disposal,
    _safe_stat,
    _write_all,
)
from ._managed_lease_wire import (
    MAX_LEASE_BYTES,
    LeaseError,
    OperationLease,
    boottime_ns,
    checked_lease,
    decode_lease,
    encode_lease,
)

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


def _lease_directory(store: ManagedJobStore, expected_launch: bytes, lease: OperationLease) -> int:
    """Acquire before checking permanent closure and stored launch binding."""
    checked_lease(lease, expected_launch)
    if lease.run_id != store.run_id:
        raise StoreError("lease run mismatch")
    directory = store._run_dir(create=False)
    if directory is None:
        raise StoreError("missing run store")
    try:
        _acquire_mutation_gate(directory)
        _refuse_disposal(directory)
        if store.read_stop_request():
            raise StoreError("run stop committed")
        return directory
    except BaseException:
        os.close(directory)
        raise


def publish_initial_lease(store: ManagedJobStore, expected_launch: bytes, lease: OperationLease) -> None:
    """Publish after complete request staging and before initial launch."""
    directory = _lease_directory(store, expected_launch, lease)
    try:
        request = store.read_request()
        if request is None or request.launch != expected_launch or request.operation_lease != lease:
            raise StoreError("initial lease request binding mismatch")
        if store.read_fact(FactName.LAUNCH) is not None:
            raise StoreError("initial lease already launched")
        _replace_lease(store, directory, expected_launch, lease)
    finally:
        os.close(directory)


def publish_lease(store: ManagedJobStore, expected_launch: bytes, lease: OperationLease) -> None:
    """Renew only the exact launched run under the cooperative mutation gate."""
    directory = _lease_directory(store, expected_launch, lease)
    try:
        if store.read_fact(FactName.LAUNCH) != expected_launch:
            raise StoreError("lease launch binding mismatch")
        _replace_lease(store, directory, expected_launch, lease)
    finally:
        os.close(directory)


def _replace_lease(store: ManagedJobStore, directory: int, expected_launch: bytes, lease: OperationLease) -> None:
    """The caller holds the run gate through stage closure and directory sync."""
    previous = read_lease(store, expected_launch)
    if not lease.sampled_ns <= boottime_ns() < lease.expires_ns:
        raise LeaseError("lease sample is future or expired")
    if previous is not None and lease.expires_ns <= previous.expires_ns:
        if previous == lease:
            return
        raise StoreError("lease expiry cannot move backward")
    stage = ".lease-stage-" + uuid4().hex
    fd = os.open(stage, _CREATE_FLAGS, 0o400, dir_fd=directory)
    try:
        os.fchmod(fd, 0o400)
        _safe_stat(fd, 0o400, store._owner_uid, links=(1,))
        _write_all(fd, encode_lease(lease))
        os.fsync(fd)
        os.replace(stage, LEASE_LEAF, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        os.close(fd)
