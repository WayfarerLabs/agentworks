"""The VM-bound host-key store and the OpenSSH options each trust mode selects.

Each VM has one file in ``STORE_DIR`` holding its host keys as plain
``known_hosts`` lines under the alias ``agentworks-vm-<name>``. The VM name, not
its address, keys the record, so address changes never orphan trust. A
connection to a VM chooses one of three modes explicitly:

- ``VM`` reads the store with ``StrictHostKeyChecking=yes`` and never falls back
  to first use: a VM with no record raises :class:`TrustMissing`.
- ``ENROLL`` is create's first core connection. It writes the VM's freshly
  reset store file under ``accept-new``.
- ``UNVERIFIED`` is cloud provisioning before the VM is in the store. It uses a
  file the create owns, accepted once and pinned for the rest of the create.

Connections to hosts, such as Remote Lima's, are not VM trust and select
:data:`HOST_TRUST`.

This module imports nothing from the legacy transport stack; legacy code calls
it, never the reverse.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import os
import re
import struct
import sys
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePath
from typing import TYPE_CHECKING, NamedTuple

from agentworks.atomic_write import atomic_write_text
from agentworks.config.validation import CONFIG_DIR
from agentworks.errors import ConfigError, StateError, ValidationError
from agentworks.execution.errors import TrustMissing
from agentworks.path_rendering import format_host_path

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

STORE_DIR = CONFIG_DIR / "known_hosts"

# The null device, for options that must name no file. OpenSSH before 8.5 reads
# a ``none`` value as a file name relative to the working directory, so a file
# planted there could add a trusted key; the null device is accepted by every
# version.
_NULL_DEVICE = "NUL" if sys.platform == "win32" else "/dev/null"

# What a VM name must satisfy to be a file name and a host-key alias. Persisted
# names are not re-validated against the creation grammar, so this checks only
# what the file name and the alias depend on.
_SAFE_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")
_KEY_TYPE = re.compile(r"[A-Za-z0-9@._-]+")
_KEY_BLOB = re.compile(r"[A-Za-z0-9+/]+={0,2}")


class TrustMode(StrEnum):
    """How a connection to a VM establishes trust in the VM's host key."""

    VM = "vm"
    ENROLL = "enroll"
    UNVERIFIED = "unverified"


class HostKey(NamedTuple):
    """One public host key: its type and base64 blob, as in a ``known_hosts`` line."""

    key_type: str
    blob: str


@dataclass(frozen=True)
class TrustBinding:
    """A VM connection's trust mode, with the file ``UNVERIFIED`` pins into."""

    mode: TrustMode
    vm_name: str
    provisioning_file: Path | None = None

    def __post_init__(self) -> None:
        if (self.mode is TrustMode.UNVERIFIED) != (self.provisioning_file is not None):
            raise ValueError("an unverified trust binding needs a provisioning file, and no other mode takes one")


@dataclass(frozen=True)
class HostTrust:
    """Marker for a connection to a host rather than a VM.

    Hosts keep the operator's own SSH configuration and ``known_hosts``:
    ``accept-new`` against whatever that configuration names.
    """


HOST_TRUST = HostTrust()


def host_key_alias(vm_name: str) -> str:
    """The lookup name for a VM's records.

    Deliberately not derived from the configurable ``ssh_host_prefix``, so
    changing that prefix never orphans trust.
    """
    return f"agentworks-vm-{_checked_name(vm_name)}"


def store_file(vm_name: str) -> Path:
    """The VM's store file. The ``vm-`` prefix keeps Windows reserved device
    names, which stay reserved with any extension, usable as VM names."""
    return STORE_DIR / f"vm-{_checked_name(vm_name)}"


def _checked_name(vm_name: str) -> str:
    # Boundary: the name arrives from the database or a CLI argument and becomes
    # a file name and an ssh option value.
    if not _SAFE_NAME.fullmatch(vm_name):
        raise ValidationError(f"invalid VM name '{vm_name}' for the host-key store", entity_kind="vm")
    return vm_name


def literal_path(path: PurePath, what: str) -> str:
    """``path`` as an ``ssh`` option value, or a :class:`ConfigError` naming ``what``.

    OpenSSH expands tokens after parsing quotes. Refuse such a path rather
    than turning a configured file name into another file or an environment
    lookup. Shared by the store paths and the VM policy's identity file, so one
    set of rules quotes every path ssh is given.
    """
    value = path.as_posix()
    if (
        not path.is_absolute()
        or any(char in value for char in '%$"\\')
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        raise ConfigError(
            f"{what} '{format_host_path(Path(path))}' cannot be passed to ssh: it must be "
            "absolute and free of '%', '$', quotes, backslashes and control characters",
        )
    return value


def _store_error(action: str, path: Path, error: OSError, vm_name: str | None = None) -> StateError:
    return StateError(
        f"cannot {action} '{format_host_path(path)}': {error}",
        entity_kind="vm",
        entity_name=vm_name,
    )


def _ensure_store_dir() -> None:
    try:
        STORE_DIR.mkdir(parents=True, exist_ok=True)
        if sys.platform != "win32":
            # mkdir's mode is masked by the umask and ignored for an existing
            # directory; the store holds trust decisions, so pin it.
            STORE_DIR.chmod(0o700)
    except OSError as e:
        raise _store_error("prepare the host-key store directory", STORE_DIR, e) from e


def stored_keys(vm_name: str) -> tuple[HostKey, ...]:
    """The keys recorded for the VM, empty when it has no record.

    Boundary: the file is persisted state that an earlier run, ssh itself or the
    operator wrote, so only well-formed lines under this VM's alias count.
    """
    path = store_file(vm_name)
    alias = host_key_alias(vm_name)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ()
    except OSError as e:
        raise _store_error("read the host-key store", path, e, vm_name) from e
    keys: list[HostKey] = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) >= 3 and fields[0] == alias and is_valid_key(fields[1], fields[2]):
            key = HostKey(fields[1], fields[2])
            if key not in keys:
                keys.append(key)
    return tuple(keys)


def is_valid_key(key_type: str, blob: str) -> bool:
    """Whether the fields could be a host key ``ssh-keygen -lf`` would read.

    Boundary: a key arrives from a ``known_hosts`` file, ``ssh-keyscan`` output
    or the store, so the check is on the blob itself: it must be base64, its
    first SSH string must be the line's key type, and (except for certificates)
    it must be nothing but whole SSH strings, which catches a truncated blob.
    """
    if not (_KEY_TYPE.fullmatch(key_type) and _KEY_BLOB.fullmatch(blob)):
        return False
    try:
        raw = base64.b64decode(blob, validate=True)
    except (binascii.Error, ValueError):
        return False
    offset, strings = 0, []
    while offset < len(raw):
        if offset + 4 > len(raw):
            return False
        (length,) = struct.unpack_from(">I", raw, offset)
        end = offset + 4 + length
        if end > len(raw):
            return False
        strings.append(raw[offset + 4 : end])
        offset = end
        if key_type.endswith("-cert-v01@openssh.com") and len(strings) == 1:
            break  # the rest of a certificate is not a run of strings
    return bool(strings) and strings[0] == key_type.encode("ascii")


def has_record(vm_name: str) -> bool:
    """Whether the store holds at least one key for the VM."""
    return bool(stored_keys(vm_name))


def require_record(vm_name: str) -> None:
    """Raise :class:`TrustMissing` unless the store holds a key for the VM."""
    if not has_record(vm_name):
        raise TrustMissing(vm_name)


def write_records(vm_name: str, keys: Sequence[HostKey]) -> None:
    """Replace the VM's record with ``keys``, atomically and privately.

    Boundary: the key fields often come from a file the operator supplied, and
    they become lines of a file ``ssh`` trusts, so a malformed field is refused
    here rather than written.
    """
    for key in keys:
        if not is_valid_key(key.key_type, key.blob):
            raise ValidationError(
                f"refusing to record a malformed host key for VM '{vm_name}'", entity_kind="vm", entity_name=vm_name
            )
    alias = host_key_alias(vm_name)
    content = "".join(f"{alias} {key.key_type} {key.blob}\n" for key in keys)
    path = store_file(vm_name)
    _ensure_store_dir()
    try:
        atomic_write_text(path, content, newline="\n")
    except OSError as e:
        raise _store_error("write the host-key store", path, e, vm_name) from e


def begin_enrollment(vm_name: str) -> None:
    """Reset the VM's record so a reused name never inherits a key.

    Creates the store directory, which OpenSSH will not create for a custom
    ``UserKnownHostsFile``: under ``accept-new`` a failed write only warns. The
    file is left empty and private for the enrolling connection to fill.
    """
    path = store_file(vm_name)
    _ensure_store_dir()
    try:
        path.unlink(missing_ok=True)
        # ssh appends to an existing file and keeps its mode, so the record the
        # first connection writes is as private as one ``write_records`` makes.
        path.touch(mode=0o600)
    except OSError as e:
        raise _store_error("reset the host-key store", path, e, vm_name) from e


def remove(vm_name: str) -> None:
    """Delete the VM's record. Missing is fine; the VM is going away."""
    path = store_file(vm_name)
    try:
        path.unlink(missing_ok=True)
    except OSError as e:
        raise _store_error("remove the host-key store", path, e, vm_name) from e


@contextlib.contextmanager
def provisioning_binding(vm_name: str) -> Iterator[TrustBinding]:
    """An ``UNVERIFIED`` binding over a private file that exists for the ``with`` body.

    The file is created by ``mkstemp`` (mode 0600, unpredictable name) inside the
    private store directory, never a shared temporary directory, so no other local
    user can seed it with a key. It is deleted on every exit.
    """
    _checked_name(vm_name)
    _ensure_store_dir()
    try:
        fd, name = tempfile.mkstemp(dir=STORE_DIR, prefix=f"provisioning-{vm_name}-", suffix=".tmp")
        os.close(fd)
    except OSError as e:
        raise _store_error("create a provisioning file in", STORE_DIR, e, vm_name) from e
    path = Path(name)
    try:
        yield TrustBinding(TrustMode.UNVERIFIED, vm_name, path)
    finally:
        path.unlink(missing_ok=True)


def _pinned_options() -> tuple[tuple[str, str], ...]:
    """Settings every VM connection pins, whatever configuration says, as
    (name, value). Shared by the spawn-time ``-o`` arguments and the managed
    SSH entries, so the operator's own ``ssh`` pins what Agentworks pins."""
    return (
        ("GlobalKnownHostsFile", _NULL_DEVICE),
        ("UpdateHostKeys", "no"),
        ("HashKnownHosts", "no"),
        ("CheckHostIP", "no"),
    )


def ssh_options(trust: TrustBinding | HostTrust) -> tuple[str, ...]:
    """The ``-o`` arguments for ``ssh`` or ``scp`` under ``trust``.

    ``VM`` mode reads the store now, at spawn rather than when the transport was
    built, and raises :class:`TrustMissing` without a record.

    Every VM mode pins the global known-hosts file to the null device and
    ``UpdateHostKeys``, ``HashKnownHosts`` and ``CheckHostIP`` off. They are
    ``-o`` options, which take precedence over any configuration file, so the
    system configuration (Debian ships ``HashKnownHosts yes``) cannot change
    them.
    """
    if isinstance(trust, HostTrust):
        return ("-o", "StrictHostKeyChecking=accept-new")
    if trust.mode is TrustMode.UNVERIFIED:
        assert trust.provisioning_file is not None  # TrustBinding.__post_init__
        values = [
            f'UserKnownHostsFile="{literal_path(trust.provisioning_file, "host-key store path")}"',
            "StrictHostKeyChecking=accept-new",
        ]
    else:
        if trust.mode is TrustMode.VM:
            require_record(trust.vm_name)
        checking = "yes" if trust.mode is TrustMode.VM else "accept-new"
        values = [
            f'UserKnownHostsFile="{literal_path(store_file(trust.vm_name), "host-key store path")}"',
            f"HostKeyAlias={host_key_alias(trust.vm_name)}",
            f"StrictHostKeyChecking={checking}",
        ]
    values += [f"{name}={value}" for name, value in _pinned_options()]
    return tuple(arg for value in values for arg in ("-o", value))


def entry_options(vm_name: str) -> tuple[str, ...]:
    """``ssh_config`` option lines that make the operator's own ``ssh`` verify
    against the store, or none while the VM has no record.

    Until the VM has a record the operator's ``ssh`` keeps working against their
    own ``known_hosts``. The pinned settings stop that ``ssh`` from reading a
    global known-hosts file, writing to the store, or hashing it.
    """
    if not has_record(vm_name):
        return ()
    return (
        f"HostKeyAlias {host_key_alias(vm_name)}",
        f'UserKnownHostsFile "{literal_path(store_file(vm_name), "host-key store path")}"',
        "StrictHostKeyChecking yes",
        *(f"{name} {value}" for name, value in _pinned_options()),
    )
