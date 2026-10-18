"""Core-selected staged-helper loader with the ordinary root guest checkpoint."""

from __future__ import annotations

from importlib.resources import files

from ._helper_bundle import FixedFileHelperBundle, RootGuestDelivery, RootGuestProgram, _build_root_guest_program

_MODULES = (
    "_helper_identity",
    "_file_paths",
    "_scratch_receipt",
    "_scratch",
    "_scratch_wire",
    "_scratch_root",
    "_staged_helper_protocol",
    "_staged_helper_guest",
)


def build_staged_helper_program(bundle: FixedFileHelperBundle) -> RootGuestProgram:
    """Bind packaged first-party bootstrap source outside the request protocol."""
    package = files(__package__)
    sources = tuple((name, package.joinpath(f"{name}.py").read_text(encoding="utf-8")) for name in _MODULES)
    entry = (
        f"from ._staged_helper_guest import main as _main\ndef main(nonce):return _main(nonce,{bundle.bootstrap!r})\n"
    )
    return _build_root_guest_program(
        "_agw_staged_helper",
        sources + (("_staged_helper_entry", entry),),
        "_staged_helper_entry",
        delivery=RootGuestDelivery.FIXED_PREFIX,
    )
