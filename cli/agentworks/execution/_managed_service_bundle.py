"""Exact first-party derived source for the private managed service."""

from __future__ import annotations

from ._helper_bundle import RootGuestDelivery, build_helper_modules, build_root_guest_program

_MODULES = (
    "_helper_identity",
    "_managed_job_wire",
    "_managed_lease_wire",
    "_managed_job_request",
    "_managed_job_store",
    "_managed_lease_store",
    "_managed_lease_controller",
    "_managed_service_guest",
)

_PROGRAM = build_root_guest_program(
    "_agw_managed_service", _MODULES, "_managed_service_guest", delivery=RootGuestDelivery.INLINE
)
_ADMISSION = "_agw_managed_service_admission"
_ENTRY = build_helper_modules(
    _ADMISSION, ("_helper_identity", "_vm_guest_identity_protocol", "_guest_bootstrap"), compact=True
) + (
    "def _agw_service_entry():\n"
    " try:\n"
    "  if len(sys.argv)!=3 or len(sys.argv[2])>65536:raise ValueError\n"
    "  run_id=sys.argv[1]\n"
    "  if len(run_id)!=32 or any(c not in '0123456789abcdef' for c in run_id):raise ValueError\n"
    "  data=json.loads(sys.argv[2])\n"
    "  if type(data) is not dict or set(data)!={'identity','guest'}:raise ValueError\n"
    "  if json.dumps(data,ensure_ascii=True,sort_keys=True,separators=(',',':'))!=sys.argv[2]:raise ValueError\n"
    f"  identity=sys.modules[{_ADMISSION + '._helper_identity'!r}].decode_identity(data['identity'])\n"
    "  if identity.euid!=0:raise ValueError\n"
    "  guest=data['guest']\n"
    "  if type(guest) is not list or len(guest)!=3:raise ValueError\n"
    f"  expected=sys.modules[{_ADMISSION + '._vm_guest_identity_protocol'!r}].VMGuestIdentity(*guest)\n"
    " except (ValueError,TypeError,KeyError,RecursionError,OverflowError):return 125\n"
    f" return sys.modules[{_ADMISSION + '._guest_bootstrap'!r}].main(\n"
    f"  identity.euid,identity.egid,identity.groups,{_PROGRAM.loader_source!r},\n"
    "  (expected.instance_marker,expected.boot_id,expected.init_start_ticks))\n"
    "raise SystemExit(_agw_service_entry())\n"
)
FIXED_SOURCE = _ENTRY
