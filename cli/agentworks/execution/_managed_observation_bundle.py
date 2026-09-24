"""Exact-source Python 3.11 bundle for the private managed observation reader."""

from __future__ import annotations

from ._helper_bundle import build_file_helper_bundle

FIXED_BUNDLE = build_file_helper_bundle(
    "_agw_managed_observation",
    (
        "_helper_identity",
        "_managed_job_wire",
        "_managed_job_request",
        "_managed_job_store",
        "_file_wire",
        "_managed_observation_protocol",
        "_managed_observation_guest",
    ),
    "_managed_observation_guest",
)
