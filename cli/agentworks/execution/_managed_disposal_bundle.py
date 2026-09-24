"""Exact-source Python 3.11 bundle for private managed disposal."""

from __future__ import annotations

from ._helper_bundle import build_file_helper_bundle

FIXED_BUNDLE = build_file_helper_bundle(
    "_agw_managed_disposal",
    (
        "_helper_identity",
        "_managed_job_wire",
        "_managed_job_request",
        "_managed_job_store",
        "_file_wire",
        "_managed_observation_protocol",
        "_managed_disposal_store",
        "_managed_disposal_protocol",
        "_managed_disposal_guest",
    ),
    "_managed_disposal_guest",
)
