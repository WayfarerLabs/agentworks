"""Darwin descriptor access metadata for mutation admission.

Read-only traversal bundles need only the separate ACL primitives. Mutation
helpers and the workstation publisher share these additional native inspections.
"""

from __future__ import annotations

import os
import stat

from . import _darwin_descriptors as acl


def bsd_flags(observed: os.stat_result) -> int:
    flags = getattr(observed, "st_flags", None)
    if flags is None:
        raise acl.DarwinMetadataError("BSD flags are unavailable")
    return int(flags)


def has_xattrs(fd: int) -> bool:
    """Use Darwin's four-argument flistxattr, independent of Python wrappers."""
    try:
        import ctypes

        list_names = ctypes.CDLL(None, use_errno=True).flistxattr
    except (ImportError, AttributeError, OSError) as error:
        raise acl.DarwinMetadataError("Extended attribute inspection is unavailable") from error
    list_names.argtypes = (ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int)
    list_names.restype = ctypes.c_ssize_t
    ctypes.set_errno(0)
    count = int(list_names(fd, None, 0, 0))
    if count < 0:
        raise acl.DarwinMetadataError("Cannot inspect extended attributes")
    return count != 0


def require_plain_metadata(fd: int) -> None:
    """Refuse target flags, xattrs, ACLs and regular-file privilege bits.

    Directory set-group-ID and sticky bits remain explicit metadata operations.
    Inspection never clears unsupported metadata.
    """
    observed = os.fstat(fd)
    privileged = stat.S_ISUID | (stat.S_ISGID if stat.S_ISREG(observed.st_mode) else 0)
    if observed.st_mode & privileged or bsd_flags(observed) != 0 or has_xattrs(fd) or acl.extended_acl_tags(fd):
        raise acl.DarwinMetadataError("Unsupported descriptor metadata")
