"""Closed wire fragment for private filesystem revision evidence."""

from __future__ import annotations

import stat

from ._file_stat import FileRevision, FileStat

_MAX_ID = 2**32 - 1
_MAX_STAT_VALUE = 2**64 - 1
_MIN_TIME_NS = -(2**63)
_MAX_TIME_NS = 2**63 - 1
_LOWER_HEX = frozenset("0123456789abcdef")
_REVISION_FIELDS = frozenset(
    {
        "changed_ns",
        "device",
        "digest",
        "gid",
        "inode",
        "link_count",
        "mode",
        "modified_ns",
        "size",
        "uid",
        "version",
    }
)


class FileRevisionWireError(ValueError):
    """A filesystem revision violated its closed wire schema."""

    def __init__(self) -> None:
        super().__init__("invalid file revision wire fragment")


def _bounded_integer(value: object, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise FileRevisionWireError
    return value


def _supported_mode(mode: int) -> bool:
    return stat.S_ISREG(mode) or stat.S_ISDIR(mode) or stat.S_ISSOCK(mode)


def encode_file_revision(revision: FileRevision) -> dict[str, object]:
    """Encode one trusted typed revision without changing its object envelope."""
    observed = revision.stat
    return {
        "changed_ns": observed.changed_ns,
        "device": observed.device,
        "digest": None if revision.digest is None else revision.digest.hex(),
        "gid": observed.gid,
        "inode": observed.inode,
        "link_count": observed.link_count,
        "mode": observed.mode,
        "modified_ns": observed.modified_ns,
        "size": observed.size,
        "uid": observed.uid,
        "version": 1,
    }


def decode_file_revision(value: object) -> FileRevision:
    """Validate one revision received across a private helper boundary."""
    if (
        type(value) is not dict
        or set(value) != _REVISION_FIELDS
        or value["version"] != 1
        or type(value["version"]) is not int
    ):
        raise FileRevisionWireError
    digest_value = value["digest"]
    if digest_value is not None and (
        type(digest_value) is not str
        or len(digest_value) != 64
        or any(character not in _LOWER_HEX for character in digest_value)
    ):
        raise FileRevisionWireError
    observed = FileStat(
        device=_bounded_integer(value["device"], 0, _MAX_STAT_VALUE),
        inode=_bounded_integer(value["inode"], 1, _MAX_STAT_VALUE),
        mode=_bounded_integer(value["mode"], 0, 0o177777),
        link_count=_bounded_integer(value["link_count"], 1, _MAX_STAT_VALUE),
        uid=_bounded_integer(value["uid"], 0, _MAX_ID),
        gid=_bounded_integer(value["gid"], 0, _MAX_ID),
        size=_bounded_integer(value["size"], 0, _MAX_STAT_VALUE),
        modified_ns=_bounded_integer(value["modified_ns"], _MIN_TIME_NS, _MAX_TIME_NS),
        changed_ns=_bounded_integer(value["changed_ns"], _MIN_TIME_NS, _MAX_TIME_NS),
    )
    if (
        not _supported_mode(observed.mode)
        or (stat.S_ISREG(observed.mode) and observed.link_count != 1)
        or (digest_value is not None and not stat.S_ISREG(observed.mode))
    ):
        raise FileRevisionWireError
    return FileRevision(observed, None if digest_value is None else bytes.fromhex(digest_value))
