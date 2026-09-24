"""Exact-source Python 3.11 bundle for one private managed start attempt."""

from __future__ import annotations

from importlib.resources import files

from ._helper_bundle import _build_file_helper_bundle
from ._managed_service_bundle import FIXED_SOURCE as SERVICE_SOURCE

_MODULES = (
    "_helper_identity",
    "_managed_job_wire",
    "_managed_job_request",
    "_managed_job_store",
    "_file_wire",
    "_managed_start_protocol",
)
_PACKAGE = files(__package__)
_SOURCES = tuple((name, _PACKAGE.joinpath(f"{name}.py").read_text(encoding="utf-8")) for name in _MODULES)
_SOURCES += (("_managed_service_bundle", f"FIXED_SOURCE = {SERVICE_SOURCE!r}\n"),)
_SOURCES += (("_managed_start_guest", _PACKAGE.joinpath("_managed_start_guest.py").read_text(encoding="utf-8")),)

FIXED_BUNDLE = _build_file_helper_bundle("_agw_managed_start", _SOURCES, "_managed_start_guest")
