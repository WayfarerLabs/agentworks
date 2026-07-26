"""The shared workspaces-parent directory setup, run during VM init.

Extracted from ``driver._phase_b_setup`` (matching the sibling
``ssh_keys`` / ``mise`` helpers) so the ordering the two steps depend on,
the recursive ACL apply BEFORE the parent-traversal re-grant, is a named,
testable unit rather than an inline block. Getting that order wrong silently
strips agent traversal into the whole workspaces tree (see #254).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks import output
from agentworks.ssh import SSHError
from agentworks.workspaces.acls import apply_workspace_acls

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.ssh import SSHLogger
    from agentworks.transports import Transport


def _setup_workspaces_directory(target: Transport, config: Config, logger: SSHLogger) -> None:
    """Create the shared workspaces parent and apply the canonical ACL.

    Non-fatal: a failure warns and continues (the operator recovers on the
    next ``vm reinit``). ``vm_workspaces`` is guaranteed outside ``/home``
    (config load rejects a value at or under ``/home``; see
    ``config.validation.validate_vm_workspaces``), so the admin home can stay
    ``0750`` without a workspace under it forcing the home world-traversable.

    Two steps, and their ORDER is load-bearing:

    1. ``apply_workspace_acls`` sets the canonical spec recursively, including
       ``other::---`` (access and default), across ``workspaces_dir`` and
       everything under it. That hardens the workspace subtrees (no
       world-read/traverse), which is what we want, but it also clears the
       ``other`` traverse bit on ``workspaces_dir`` itself.
    2. The parent-traversal loop then re-grants ``other::--x`` on
       ``workspaces_dir`` and its ANCESTORS. Those parents are ``root:root``
       and agents are neither their owner nor in their group, so agents reach
       ``workspaces_dir`` only via ``other``-x; step 1 having cleared it, this
       loop restores it. It walks UPWARD via ``dirname`` (never into the
       subtrees), so the workspace subtrees stay at ``other::---`` while the
       shared parent chain regains traverse. ``chmod`` does not touch default
       ACLs, so the ``default:other::---`` seeded on ``workspaces_dir`` (which
       new workspaces inherit) survives.

    Applying the ACL first and re-granting traverse second is therefore
    mandatory: the reverse order lets step 1 clobber the traverse bit step 2
    just granted, cutting agents off from every workspace on the VM.
    """
    workspaces_dir = config.paths.vm_workspaces
    try:
        # acl is installed as a system package in _install_system_packages.
        target.run(f"mkdir -p {workspaces_dir}", sudo=True)
        # The canonical workspace ACL on the workspaces parent, the same spec
        # (and shared helper) workspace create and repair apply per workspace,
        # so the three never drift.
        apply_workspace_acls(target, workspaces_dir)
        # Re-grant agent traversal on workspaces_dir and its ancestors, AFTER
        # the ACL apply (see the ordering rationale above).
        target.run(
            f'sh -c \'p={workspaces_dir}; while [ "$p" != "/" ]; do chmod a+x "$p"; p=$(dirname "$p"); done\'',
            sudo=True,
        )
    except SSHError as e:
        msg = f"workspaces directory setup failed: {e}"
        logger.warning(msg)
        output.warn(msg)
