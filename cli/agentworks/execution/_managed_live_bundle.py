"""Fixed Python 3.11 root relay source, delivered before its bounded envelope."""

from __future__ import annotations

from ._helper_bundle import RootGuestDelivery, build_root_guest_program

ROOT_PROGRAM = build_root_guest_program(
    "_agw_managed_live",
    (
        "_helper_identity",
        "_managed_job_wire",
        "_managed_lease_wire",
        "_managed_job_request",
        "_managed_job_store",
        "_managed_live_protocol",
        "_managed_live_guest",
    ),
    "_managed_live_guest",
    delivery=RootGuestDelivery.FIXED_PREFIX,
)
