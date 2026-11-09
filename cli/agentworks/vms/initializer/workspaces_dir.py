"""The shared workspaces-parent directory setup, run during VM init.

Extracted from ``driver._phase_b_setup`` (matching the sibling
``ssh_keys`` / ``mise`` helpers) so the ordering the two steps depend on,
the recursive ACL apply BEFORE the parent-traversal re-grant, is a named,
testable unit rather than an inline block. Getting that order wrong silently
strips agent traversal into the whole workspaces tree (see #254).
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from agentworks.execution.requests import Command
from agentworks.workspaces.acls import apply_workspace_acls

from .steps import STEP_FAILURES, warn_step

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.execution.files import Files

    from .steps import InitLog


def traversal_paths(workspaces_dir: str) -> tuple[str, ...]:
    """``workspaces_dir`` and each of its ancestors but ``/``, deepest first: the paths agents traverse."""
    path = PurePosixPath(workspaces_dir)
    return tuple(str(directory) for directory in (path, *path.parents) if str(directory) != "/")


def _setup_workspaces_directory(files: Files, config: Config, log: InitLog) -> None:
    """Create the shared workspaces parent and apply the canonical ACL, as root.

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
    2. The traversal re-grant then gives ``other::--x`` back to
       ``workspaces_dir`` and its ANCESTORS (:func:`traversal_paths`, computed
       here and passed to one ``chmod``). Those parents are ``root:root`` and
       agents are neither their owner nor in their group, so agents reach
       ``workspaces_dir`` only via ``other``-x; step 1 having cleared it, this
       restores it. It never descends into the subtrees, so they stay at
       ``other::---`` while the shared parent chain regains traverse.
       ``chmod`` does not touch default ACLs, so the ``default:other::---``
       seeded on ``workspaces_dir`` (which new workspaces inherit) survives.

    Applying the ACL first and re-granting traverse second is therefore
    mandatory: the reverse order lets step 1 clobber the traverse bit step 2
    just granted, cutting agents off from every workspace on the VM.
    """
    workspaces_dir = config.paths.vm_workspaces
    try:
        # acl is installed as a system package in _install_system_packages.
        files.access.run(
            Command(("mkdir", "-p", "--", workspaces_dir), elevated=True, deadline=files.deadline), check=True
        )
        # The canonical workspace ACL on the workspaces parent, the same spec
        # (and shared helper) workspace create, repair, copy, and rehome apply
        # per workspace, so all of them never drift. Every workspace ACL path
        # denies `other` (access and default), so copied and rehomed workspaces
        # are hardened identically to created ones.
        apply_workspace_acls(files, workspaces_dir)
        # Re-grant agent traversal on workspaces_dir and its ancestors, AFTER
        # the ACL apply (see the ordering rationale above).
        files.access.run(
            Command(("chmod", "a+x", "--", *traversal_paths(workspaces_dir)), elevated=True, deadline=files.deadline),
            check=True,
        )
    except STEP_FAILURES as error:
        warn_step(log, "workspaces directory setup failed", error)
