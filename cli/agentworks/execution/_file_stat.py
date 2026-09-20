"""Shared stdlib-only regular-file observation metadata."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True, repr=False)
class FileStat:
    device: int
    inode: int
    mode: int
    link_count: int
    uid: int
    gid: int
    size: int
    modified_ns: int
    changed_ns: int
