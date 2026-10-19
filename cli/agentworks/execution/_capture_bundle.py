"""Fixed capture entry with the ordinary numeric/full-guest admission."""

from ._helper_bundle import RootGuestDelivery, build_root_guest_program

_MODULES = (
    "_helper_identity",
    "_darwin_descriptors",
    "_file_paths",
    "_file_paths_darwin",
    "_scratch_receipt",
    "_scratch",
    "_scratch_wire",
    "_scratch_root",
    "_process",
    "_evidence_wire",
    "_inline_control",
    "_capture_control",
    "_inline_request",
    "_inline_guest_io",
    "_inline_guest",
    "_capture_guest",
)
CAPTURE_PROGRAM = build_root_guest_program(
    "_agw_capture",
    _MODULES,
    "_capture_guest",
    delivery=RootGuestDelivery.FIXED_PREFIX,
)
