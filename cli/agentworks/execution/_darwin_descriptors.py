"""Lazy stdlib-only Darwin descriptor ACL inspection.

The fixed helpers and workstation publisher share these native ABI calls. Import
does not load libc or inspect any filesystem object.
"""

from __future__ import annotations

import errno

ACL_TYPE_EXTENDED = 0x100  # Darwin sys/acl.h.


class DarwinMetadataError(OSError):
    """Descriptor access metadata is unsupported or could not be inspected."""


def extended_acl_tags(fd: int) -> tuple[int, ...]:
    """Enumerate at most 128 Darwin extended ACL entries, releasing the ACL."""
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        get_acl = libc.acl_get_fd_np
        free_acl = libc.acl_free
        get_entry = libc.acl_get_entry
        get_tag = libc.acl_get_tag_type
        valid_acl = libc.acl_valid
    except (ImportError, AttributeError, OSError) as error:
        raise DarwinMetadataError("Extended ACL inspection is unavailable") from error
    get_acl.argtypes = (ctypes.c_int, ctypes.c_int)
    get_acl.restype = ctypes.c_void_p
    free_acl.argtypes = (ctypes.c_void_p,)
    free_acl.restype = ctypes.c_int
    get_entry.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))
    get_entry.restype = ctypes.c_int
    get_tag.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_int))
    get_tag.restype = ctypes.c_int
    valid_acl.argtypes = (ctypes.c_void_p,)
    valid_acl.restype = ctypes.c_int
    ctypes.set_errno(0)
    acl = get_acl(fd, ACL_TYPE_EXTENDED)
    if not acl:
        if ctypes.get_errno() == errno.ENOENT:
            return ()
        raise DarwinMetadataError("Cannot inspect extended ACL")
    try:
        if valid_acl(acl) != 0:
            raise DarwinMetadataError("Extended ACL is malformed")
        tags: list[int] = []
        for index in range(129):
            entry = ctypes.c_void_p()
            ctypes.set_errno(0)
            result = get_entry(acl, index, ctypes.byref(entry))
            if result == -1 and ctypes.get_errno() == errno.EINVAL:
                return tuple(tags)
            if result != 0 or not entry.value or index == 128:
                raise DarwinMetadataError("Cannot enumerate extended ACL")
            tag = ctypes.c_int()
            if get_tag(entry, ctypes.byref(tag)) != 0:
                raise DarwinMetadataError("Cannot inspect extended ACL tag")
            tags.append(tag.value)
        raise DarwinMetadataError("Extended ACL exceeds the supported bound")
    finally:
        if free_acl(acl) != 0:
            raise DarwinMetadataError("Cannot release extended ACL")


def has_non_deny_acl(fd: int) -> bool:
    """Deny-only ancestor ACLs cannot grant an alternate writer access."""
    return any(tag != 2 for tag in extended_acl_tags(fd))
