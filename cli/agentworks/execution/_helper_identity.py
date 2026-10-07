"""Shared stdlib-only identity contract for private destination helpers."""

from __future__ import annotations

import os
from dataclasses import dataclass

_MAX_ID = 2**32 - 1
_MAX_GROUPS = 65_536


@dataclass(frozen=True, slots=True)
class IdentityExpectation:
    euid: int
    egid: int
    groups: tuple[int, ...]


def decode_identity(value: object) -> IdentityExpectation:
    """Validate an identity record received across a helper wire boundary."""
    if type(value) is not dict or set(value) != {"egid", "euid", "groups"}:
        raise ValueError("invalid identity")
    euid = value["euid"]
    egid = value["egid"]
    groups = value["groups"]
    if (
        type(euid) is not int
        or not 0 <= euid <= _MAX_ID
        or type(egid) is not int
        or not 0 <= egid <= _MAX_ID
        or type(groups) is not list
        or not groups
        or len(groups) > _MAX_GROUPS
        or any(type(group) is not int or not 0 <= group <= _MAX_ID for group in groups)
        or groups != sorted(set(groups))
        or egid not in groups
    ):
        raise ValueError("invalid identity")
    return IdentityExpectation(euid, egid, tuple(groups))


def matches_current_identity(expected: IdentityExpectation) -> bool:
    """Require one exact Linux real, effective, saved and group identity."""
    actual_groups = tuple(sorted(set(os.getgroups()) | {os.getegid()}))
    return (
        os.getresuid() == (expected.euid, expected.euid, expected.euid)
        and os.getresgid() == (expected.egid, expected.egid, expected.egid)
        and actual_groups == expected.groups
    )
