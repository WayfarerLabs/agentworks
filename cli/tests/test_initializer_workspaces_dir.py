"""The VM-init workspaces-parent setup (``_setup_workspaces_directory``).

Pins the load-bearing ordering the two steps depend on: the recursive ACL
apply (which sets ``other::---`` across ``workspaces_dir`` and its subtree)
must run BEFORE the parent-traversal ``chmod a+x`` that re-grants
``other::--x`` on the shared parent chain. Reversed, step 1 would clobber the
traverse bit step 2 just granted and cut agents off from every workspace on
the VM (the #254 regression). This is the pin that would have caught it: the
per-workspace create/repair tests do not cover the driver-parent path.
"""

from __future__ import annotations

from types import SimpleNamespace

from agentworks.execution.errors import CommandFailed
from agentworks.execution.requests import Command
from agentworks.execution.results import Completed, Result
from agentworks.vms.initializer.workspaces_dir import _setup_workspaces_directory, traversal_paths
from agentworks.workspaces.acls import workspace_acl_commands
from tests.phase_b_support import StepGuest

WORKSPACES = "/opt/agentworks/workspaces"


def _config(workspaces_dir: str = WORKSPACES) -> SimpleNamespace:
    return SimpleNamespace(paths=SimpleNamespace(vm_workspaces=workspaces_dir))


def test_the_acl_passes_run_after_the_mkdir_and_before_the_traversal_regrant() -> None:
    """mkdir, then the three recursive ACL passes, then one ``chmod a+x`` of the parent and its ancestors, as root."""
    guest = StepGuest(local_files=False)

    _setup_workspaces_directory(guest.files, _config(), guest.log)

    assert guest.commands() == [
        Command(("mkdir", "-p", "--", WORKSPACES), elevated=True, deadline=60),
        *workspace_acl_commands(WORKSPACES),
        Command(("chmod", "a+x", "--", WORKSPACES, "/opt/agentworks", "/opt"), elevated=True, deadline=60),
    ]
    assert guest.log.warned == []


def test_traversal_covers_the_parent_and_every_ancestor_but_the_root() -> None:
    assert traversal_paths("/srv/a/workspaces/") == ("/srv/a/workspaces", "/srv/a", "/srv")
    assert traversal_paths("/workspaces") == ("/workspaces",)


def test_a_failed_acl_pass_warns_and_skips_the_regrant() -> None:
    """A step failure is non-fatal: logged and warned, not raised (the operator recovers on the next reinit)."""
    guest = StepGuest(local_files=False)
    first_pass = workspace_acl_commands(WORKSPACES)[0]
    guest.respond(first_pass, CommandFailed("box", 1, Result(Completed(1)), streams_logged=True))

    _setup_workspaces_directory(guest.files, _config(), guest.log)

    assert len(guest.log.warned) == 1
    assert guest.commands()[-1] == first_pass
