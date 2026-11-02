"""Import existing host keys into the VM-bound host-key store.

An operator who connected to a VM before the store existed has its key in
``~/.ssh/known_hosts``. ``ssh-keygen -F`` finds exactly what ``ssh`` would have
trusted for the VM's tailnet address (wildcards, negation, comma lists, hashed
entries, bracketed ports), and this service copies those keys into the VM's
store file under its alias. The source file is only read.

The store is never widened or replaced here: a VM that already has a record is
left alone.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from agentworks import execution, output
from agentworks.errors import ConfigError, ExternalError
from agentworks.execution import trust
from agentworks.execution.client import tool_beside_ssh
from agentworks.execution.trust import HostKey
from agentworks.path_rendering import format_host_path

from ._helpers import _require_vm

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agentworks.config import Config
    from agentworks.db import Database, VMRow

_KEYGEN_TIMEOUT_SECONDS = 30


class ImportStatus(StrEnum):
    """How an import ended for one VM. A closed set, rendered as-is."""

    IMPORTED = "imported"
    ALREADY_CURRENT = "already current"
    NO_MATCH = "no matching entry"
    MARKER = "trust marker present"
    CONFLICT = "conflicts with the stored key"
    NO_ADDRESS = "no tailnet address"
    MALFORMED = "malformed host key, nothing recorded"


@dataclass(frozen=True)
class HostKeyImport:
    """One VM's import outcome.

    ``has_record`` is the store's state afterwards, which is what decides
    whether the VM is usable, whatever the status.
    """

    vm_name: str
    status: ImportStatus
    keys_written: int
    has_record: bool

    def describe(self) -> str:
        if self.status is ImportStatus.IMPORTED:
            return f"{self.status.value} ({output.count(self.keys_written, 'key')})"
        return self.status.value


def default_known_hosts() -> Path:
    return Path.home() / ".ssh" / "known_hosts"


def locate_ssh_keygen() -> str:
    """The ``ssh-keygen`` beside the resolved OpenSSH client, never one on ``PATH``.

    Import therefore needs a supported client (8.5 or newer), which costs
    nothing: a record is only useful to a client that can connect. Raises
    ``ClientUnsupported`` without one, or when no ``ssh-keygen`` sits
    beside it.
    """
    return str(tool_beside_ssh(execution.resolve_ssh(), "ssh-keygen"))


@dataclass(frozen=True)
class _Matches:
    keys: tuple[HostKey, ...]
    marker: bool


def _matches(keygen: str, source: Path, address: str) -> _Matches:
    """Ask OpenSSH which keys and markers ``source`` holds for ``address``."""
    # Boundary: an external process reading an operator-supplied file.
    try:
        proc = subprocess.run(
            [keygen, "-F", address, "-f", str(source)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_KEYGEN_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ExternalError(f"ssh-keygen could not search '{format_host_path(source)}': {e}") from e
    if proc.returncode == 1:
        return _Matches((), marker=False)
    if proc.returncode != 0:
        raise ExternalError(f"ssh-keygen could not read '{format_host_path(source)}': {proc.stderr.strip()}")
    keys: list[HostKey] = []
    marker = False
    for line in proc.stdout.splitlines():
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        if fields[0].startswith("@"):
            marker = True
            continue
        if len(fields) < 3:
            continue
        key = HostKey(fields[1], fields[2])
        if key not in keys:
            keys.append(key)
    return _Matches(tuple(keys), marker)


def _import_one(vm: VMRow, keygen: str, source: Path | None) -> HostKeyImport:
    """Decide and perform one VM's import. ``source`` is None when there is no file to read."""
    name = vm.name

    def outcome(status: ImportStatus, written: int = 0) -> HostKeyImport:
        return HostKeyImport(name, status, written, trust.has_record(name))

    if not vm.tailscale_host:
        return outcome(ImportStatus.NO_ADDRESS)
    matches = _Matches((), marker=False) if source is None else _matches(keygen, source, vm.tailscale_host)
    if matches.marker:
        return outcome(ImportStatus.MARKER)
    stored = set(trust.stored_keys(name))
    if stored:
        # Never written: replacing a key would change what is trusted, and a
        # union would widen it.
        return outcome(ImportStatus.ALREADY_CURRENT if set(matches.keys) <= stored else ImportStatus.CONFLICT)
    if not matches.keys:
        return outcome(ImportStatus.NO_MATCH)
    # A record ssh-keygen would reject blocks every connection and is permanent,
    # so a VM with any such key gets none and the others carry on.
    if not all(trust.is_valid_key(*key) for key in matches.keys):
        return outcome(ImportStatus.MALFORMED)
    trust.write_records(name, matches.keys)
    return outcome(ImportStatus.IMPORTED, len(matches.keys))


def import_host_keys(
    db: Database,
    config: Config,
    names: Sequence[str],
    *,
    all_vms: bool,
    known_hosts: Path | None,
) -> tuple[HostKeyImport, ...]:
    """Import host keys for the named VMs, or for every VM with ``all_vms``.

    ``known_hosts`` names the source file; None means ``~/.ssh/known_hosts``,
    where absence just means nothing to import. A named file that does not
    exist is a :class:`ConfigError`.
    """
    vms = db.list_vms() if all_vms else [_require_vm(db, name) for name in dict.fromkeys(names)]

    if known_hosts is None:
        default = default_known_hosts()
        source: Path | None = default if default.is_file() else None
    else:
        if not known_hosts.is_file():
            raise ConfigError(f"known-hosts file '{format_host_path(known_hosts)}' does not exist")
        source = known_hosts

    keygen = locate_ssh_keygen() if source is not None else ""
    outcomes: list[HostKeyImport] = []
    try:
        for vm in vms:
            outcomes.append(_import_one(vm, keygen, source))
    finally:
        # Whatever was imported before a later VM failed has a record now, so
        # its managed entries must carry the store options.
        if any(o.status is ImportStatus.IMPORTED for o in outcomes):
            from agentworks.ssh_config import sync_ssh_config

            try:
                sync_ssh_config(config, db)
            except Exception as e:
                output.warn(f"SSH config sync failed: {e}")
    return tuple(outcomes)


def render_import(outcomes: Sequence[HostKeyImport]) -> bool:
    """Print one line per VM. True when every selected VM ends with a record."""
    for o in outcomes:
        output.info(f"{o.vm_name}: {o.describe()}")
    missing = [o.vm_name for o in outcomes if not o.has_record]
    if missing:
        output.warn(
            f"No host key is recorded for: {', '.join(missing)}. "
            "See 'agw vm import-host-keys --help' for a VM with no matching entry."
        )
    return not missing


__all__ = ["HostKeyImport", "ImportStatus", "import_host_keys", "render_import"]
