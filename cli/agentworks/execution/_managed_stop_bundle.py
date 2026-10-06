"""Exact-source Python 3.11 bundle for the private managed stop helper."""

from __future__ import annotations

from ._helper_bundle import build_file_helper_bundle

FIXED_BUNDLE = build_file_helper_bundle(
    "_agw_managed_stop",
    (
        "_helper_identity",
        "_managed_job_wire",
        "_managed_lease_wire",
        "_managed_job_request",
        "_managed_job_store",
        "_file_wire",
        "_vm_guest_identity_protocol",
        "_vm_guest_identity_guest",
        "_managed_observation_protocol",
        "_managed_stop_protocol",
        "_managed_stop_guest",
    ),
    "_managed_stop_guest",
)
