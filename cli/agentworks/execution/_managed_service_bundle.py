"""Exact first-party source for the private independent managed service."""

from __future__ import annotations

from ._helper_bundle import build_helper_modules

_MODULES = (
    "_helper_identity",
    "_managed_job_wire",
    "_managed_job_request",
    "_managed_job_store",
    "_managed_service_guest",
)

FIXED_SOURCE = build_helper_modules("_agw_managed_service", _MODULES) + (
    "raise SystemExit(sys.modules['_agw_managed_service._managed_service_guest'].main("
    "sys.argv[1] if len(sys.argv)==2 else ''))\n"
)
