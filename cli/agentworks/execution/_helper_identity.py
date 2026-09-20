"""Shared stdlib-only identity contract for private destination helpers."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class IdentityExpectation:
    euid: int
    egid: int
    groups: tuple[int, ...]


def matches_current_identity(expected: IdentityExpectation) -> bool:
    """Require one exact Linux real, effective, saved and group identity."""
    actual_groups = tuple(sorted(set(os.getgroups()) | {os.getegid()}))
    return (
        os.getresuid() == (expected.euid, expected.euid, expected.euid)
        and os.getresgid() == (expected.egid, expected.egid, expected.egid)
        and actual_groups == expected.groups
    )
