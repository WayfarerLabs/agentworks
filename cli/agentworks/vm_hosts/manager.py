"""VM host management -- PHASE-2 BRIDGE (vm-sites SDD).

The ``vm_hosts`` registry table is gone (the DB migration dropped it);
remote Lima hosts are declared as ``vm-site`` resources with
``platform_config.vm_host``. The ``agw vm-host`` commands survive until
the CLI-surface phase removes them, so each service function raises a
typed error carrying the replacement shape.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.errors import StateError
from agentworks.ssh import SSHError, SSHTarget, run

if TYPE_CHECKING:
    from agentworks.db import Database


def detect_os(ssh_host: str) -> str | None:
    """Detect the OS of a remote host via SSH."""
    try:
        result = run(
            SSHTarget(host=ssh_host, user=None, login_shell=True),
            "uname -s",
            timeout=15,
        )
        raw = result.stdout.strip().lower()
        if "darwin" in raw:
            return "darwin"
        if "linux" in raw:
            return "linux"
        return raw or None
    except (SSHError, TimeoutError):
        return None


def _replaced(name: str | None = None) -> StateError:
    from agentworks.vms.sites import site_manifest_hint

    return StateError(
        "the vm-host registry has been replaced by vm-site resources",
        entity_kind="vm-host",
        entity_name=name,
        hint=site_manifest_hint(name or "my-host")
        + "\n\nlist declared sites with `agw resource list --kind vm-site`",
    )


def add_vm_host(db: Database, name: str, ssh_host: str, platform: str = "lima") -> None:
    """PHASE-2 BRIDGE: always raises; declare a vm-site instead."""
    raise _replaced(name)


def list_vm_hosts(db: Database, *, names_only: bool = False) -> None:
    """PHASE-2 BRIDGE: always raises; list vm-site resources instead.

    ``names_only`` completion callers get empty output rather than an
    error so shell completion degrades quietly.
    """
    if names_only:
        return
    raise _replaced()


def remove_vm_host(db: Database, name: str, *, force: bool = False, yes: bool = False) -> None:
    """PHASE-2 BRIDGE: always raises; delete the vm-site manifest instead."""
    raise _replaced(name)
