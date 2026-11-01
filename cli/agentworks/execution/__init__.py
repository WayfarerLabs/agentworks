"""Everything core does on a managed VM after provisioning.

Callers build an access object with :func:`vm_access` and call its ``run``,
``stream``, ``attach`` and ``forward``; requests and results are in
``agentworks.execution.requests`` and ``results``. The functions here import
their modules on first use: ``trust`` pulls in ``config``, about 250 ms, which
a command that never connects should not pay. A test reaches every caller by
patching the one attribute on this package.

This package imports nothing from the legacy transport stack (HLA D14).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import VMRow
    from agentworks.execution.access import Identity, VMAccess
    from agentworks.execution.client import SSHClient


def vm_access(vm: VMRow, config: Config, identity: Identity) -> VMAccess:
    """The passive access object for ``identity`` on ``vm`` (see ``access.vm_access``)."""
    from agentworks.execution.access import vm_access as build

    return build(vm, config, identity)


def resolve_ssh() -> SSHClient:
    """The process's OpenSSH client, or ``ClientUnsupported`` (see ``client.resolve_ssh``)."""
    from agentworks.execution.client import resolve_ssh as resolve

    return resolve()
