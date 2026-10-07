"""Exact-source Python 3.11 bundle for private operation-lease helpers."""

from __future__ import annotations

from ._helper_bundle import build_file_helper_bundle

_MODULES = (
    "_helper_identity",
    "_managed_job_wire",
    "_managed_lease_wire",
    "_managed_job_request",
    "_managed_job_store",
    "_managed_lease_store",
    "_file_wire",
    "_vm_guest_identity_protocol",
    "_vm_guest_identity_guest",
    "_managed_observation_protocol",
    "_managed_lease_protocol",
    "_managed_lease_guest",
)
FIXED_BUNDLE = build_file_helper_bundle("_agw_managed_lease", _MODULES, "_managed_lease_guest")
