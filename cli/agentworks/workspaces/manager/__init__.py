"""Workspace lifecycle orchestration.

The package is split by concern: ``create`` (create / describe / list),
``reinit`` (reinit, its git-identity convergence, and the rehome
partial-state hint), ``rehome`` (rehome), ``delete`` (delete), ``copy``
(copy), plus shared ``_common`` (VM resolution, the VM-status guard, and
the shared workspace operation scope). This module re-exports the full
public + test surface so ``agentworks.workspaces.manager`` stays the one
import path callers use.
"""

from __future__ import annotations

from agentworks.workspaces.manager._common import _guard_vm_status
from agentworks.workspaces.manager.copy import copy_workspace
from agentworks.workspaces.manager.create import create_workspace, describe_workspace, list_workspaces
from agentworks.workspaces.manager.delete import delete_workspace
from agentworks.workspaces.manager.rehome import rehome_workspace
from agentworks.workspaces.manager.reinit import _revert_grant_on_failure, reinit_workspace

__all__ = [
    "_guard_vm_status",
    "_revert_grant_on_failure",
    "copy_workspace",
    "create_workspace",
    "delete_workspace",
    "describe_workspace",
    "list_workspaces",
    "rehome_workspace",
    "reinit_workspace",
]
