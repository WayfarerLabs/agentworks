"""The guest file program's metadata: preserved owner, mode and extended attributes, and a private temporary file.

Run as ``test_file_program.py`` runs the program (``_file_program_support``).
Linux only: the program walks with ``O_PATH``.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import struct
import subprocess
import sys

import pytest

from tests.execution._file_program_support import Guest, digest
from tests.execution._file_program_support import as_root as as_root
from tests.execution._file_program_support import guest as guest

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="the file program walks paths with Linux's O_PATH")


def test_preserve_keeps_the_existing_mode_and_extended_attributes(guest: Guest) -> None:
    destination = guest.tree / "file"
    destination.write_bytes(b"before")
    destination.chmod(0o640)
    try:
        os.setxattr(destination, "user.agentworks-test", b"kept")
    except OSError:
        pytest.skip("this filesystem keeps no user extended attributes")

    result = guest.write_if(destination, b"after", expected=digest(b"before"), mode=0o600, preserve=True)

    assert result.returncode == 0, result.stderr
    assert destination.read_bytes() == b"after"
    assert stat.S_IMODE(destination.stat().st_mode) == 0o640
    assert os.getxattr(destination, "user.agentworks-test") == b"kept"


def test_preserve_tolerates_an_attribute_that_vanishes_while_copied(guest: Guest) -> None:
    destination = guest.tree / "file"
    destination.write_bytes(b"before")
    try:
        os.setxattr(destination, "user.agentworks-test", b"kept")
        os.setxattr(destination, "user.agentworks-vanishing", b"removed while read")
    except OSError:
        pytest.skip("this filesystem keeps no user extended attributes")
    patch = (
        "import os, errno\n"
        "real = os.getxattr\n"
        "def getxattr(target, key, *a, **k):\n"
        "    if key == 'user.agentworks-vanishing':\n"
        "        raise OSError(errno.ENODATA, 'no data')\n"
        "    return real(target, key, *a, **k)\n"
        "os.getxattr = getxattr"
    )

    result = guest.write_if(destination, b"after", expected=digest(b"before"), preserve=True, patch=patch)

    assert result.returncode == 0, result.stderr
    assert os.getxattr(destination, "user.agentworks-test") == b"kept"
    assert "user.agentworks-vanishing" not in os.listxattr(destination)


def test_preserve_on_a_filesystem_without_extended_attributes_keeps_the_mode(guest: Guest) -> None:
    destination = guest.tree / "file"
    destination.write_bytes(b"before")
    destination.chmod(0o640)
    patch = (
        "import os, errno\n"
        "def listxattr(*a, **k): raise OSError(errno.ENOTSUP, 'unsupported')\n"
        "os.listxattr = listxattr"
    )

    result = guest.write_if(destination, b"after", expected=digest(b"before"), mode=0o600, preserve=True, patch=patch)

    assert result.returncode == 0, result.stderr
    assert destination.read_bytes() == b"after"
    assert stat.S_IMODE(destination.stat().st_mode) == 0o640


_RECORD_STAGING = (
    "import json, os\n"
    "def snapshot(label):\n"
    "    for name in os.listdir({directory!r}):\n"
    "        if name.startswith('.agentworks-tmp-'):\n"
    "            path = os.path.join({directory!r}, name)\n"
    "            seen = os.stat(path, follow_symlinks=False)\n"
    "            try:\n"
    "                acl = os.getxattr(path, 'system.posix_acl_access', follow_symlinks=False).hex()\n"
    "            except OSError:\n"
    "                acl = None\n"
    "            with open({record!r}, 'a') as handle:\n"
    "                handle.write(json.dumps([label, seen.st_mode, seen.st_gid, acl]) + '\\n')\n"
    "def observed(function, label, after):\n"
    "    def wrapper(*args, **kwargs):\n"
    "        if not after:\n"
    "            snapshot(label)\n"
    "        result = function(*args, **kwargs)\n"
    "        if after:\n"
    "            snapshot(label)\n"
    "        return result\n"
    "    return wrapper\n"
    "for name in ('fchown', 'fchmod', 'setxattr', 'removexattr'):\n"
    "    setattr(os, name, observed(getattr(os, name), 'metadata', True))\n"
    "os.rename = observed(os.rename, 'metadata', False)\n"
    "file_program._open_regular = observed(file_program._open_regular, 'check', False)\n"
    "file_program._digest = observed(file_program._digest, 'check', False)"
)


_ACL_MASK = 0x10


def _private(mode: int, acl: str | None) -> bool:
    """Whether only the owner can open a file: no group or other mode bits, and an access ACL's mask grants nothing.

    A file created under a directory's default ACL carries an access ACL even at mode 0600; its mask is what bounds
    every entry but the owner's.
    """
    if stat.S_IMODE(mode) & 0o077:
        return False
    if acl is None:
        return True
    # A system.posix_acl_access value: a little-endian u32 version, then (u16 tag, u16 permissions, u32 id) entries.
    masks = [
        permissions for tag, permissions, _id in struct.iter_unpack("<HHI", bytes.fromhex(acl)[4:]) if tag == _ACL_MASK
    ]
    return masks == [0]


def _set_acl(path: os.PathLike[str], *spec: str) -> None:
    setfacl = shutil.which("setfacl")
    if setfacl is None or subprocess.run([setfacl, *spec, str(path)]).returncode:
        pytest.skip("setfacl, and a filesystem that keeps ACLs, provision the ACL fixture")


@pytest.mark.parametrize(
    "case", ["write_if preserving", "write_if preserving an ACL", "write_if", "write", "write under a default ACL"]
)
def test_staging_is_private_through_every_check_and_never_wider_than_the_published_file(
    guest: Guest, case: str
) -> None:
    destination = guest.tree / "file"
    destination.write_bytes(b"before")
    destination.chmod(0o600)
    if case == "write_if preserving an ACL":
        destination.chmod(0o640)
        _set_acl(destination, "-m", f"g::-,u:{os.getuid()}:r")
    elif case == "write under a default ACL":
        _set_acl(guest.tree, "-d", "-m", f"u:{os.getuid()}:rw")
    record = guest.root / "staging.jsonl"
    patch = _RECORD_STAGING.format(directory=str(guest.tree), record=str(record))
    if case in ("write", "write under a default ACL"):
        result = guest.write(destination, b"after", mode=0o644, patch=patch)
    else:
        preserve = case != "write_if"
        result = guest.write_if(
            destination, b"after", expected=digest(b"before"), mode=0o644, preserve=preserve, patch=patch
        )

    assert result.returncode == 0, result.stderr
    published = destination.stat()
    try:
        acl: str | None = os.getxattr(destination, "system.posix_acl_access").hex()
    except OSError:
        acl = None
    final = [stat.S_IMODE(published.st_mode), published.st_gid, acl]
    snapshots = [json.loads(line) for line in record.read_text().splitlines()]
    assert {label for label, *_ in snapshots} == {"check", "metadata"}
    for label, mode, gid, staged_acl in snapshots:
        private = _private(mode, staged_acl)
        assert private or [stat.S_IMODE(mode), gid, staged_acl] == final, (label, oct(mode), staged_acl, final)
        if label == "check":
            assert private
    assert stat.S_IMODE(published.st_mode) == {"write_if preserving": 0o600, "write_if preserving an ACL": 0o640}.get(
        case, 0o644
    )


def test_preserve_keeps_the_existing_owner_and_group(guest: Guest, as_root: None) -> None:
    destination = guest.tree / "file"
    destination.write_bytes(b"before")
    owner = destination.stat()

    result = guest.write_if(destination, b"after", expected=digest(b"before"), preserve=True, sudo=True)

    assert result.returncode == 0, result.stderr
    replaced = destination.stat()
    assert (replaced.st_uid, replaced.st_gid) == (owner.st_uid, owner.st_gid)


def test_an_elevated_write_without_preserve_creates_a_root_owned_file(guest: Guest, as_root: None) -> None:

    result = guest.write_if(guest.tree / "root" / "file", b"content", mode=0o644, parent_mode=0o755, sudo=True)

    assert result.returncode == 0, result.stderr
    assert (guest.tree / "root" / "file").stat().st_uid == 0
    assert (guest.tree / "root").stat().st_uid == 0
