"""The VM-init workspaces-parent setup (``_setup_workspaces_directory``).

Pins the load-bearing ordering the two steps depend on: the recursive ACL
apply (which sets ``other::---`` across ``workspaces_dir`` and its subtree)
must run BEFORE the parent-traversal ``chmod a+x`` loop that re-grants
``other::--x`` on the shared parent chain. Reversed, step 1 would clobber the
traverse bit step 2 just granted and cut agents off from every workspace on
the VM (the #254 regression). This is the pin that would have caught it: the
per-workspace create/repair tests do not cover the driver-parent path.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock


class _RecordingTarget:
    """Minimal transport double: records every command, answers ok."""

    def __init__(self) -> None:
        self.commands: list[str] = []

    def run(self, cmd: str, **kwargs: object) -> SimpleNamespace:
        self.commands.append(cmd)
        return SimpleNamespace(ok=True, returncode=0, stdout="", stderr="")


def _make_config(workspaces_dir: str = "/opt/agentworks/workspaces") -> SimpleNamespace:
    return SimpleNamespace(paths=SimpleNamespace(vm_workspaces=workspaces_dir))


def test_traversal_regrant_runs_after_the_recursive_acl_apply() -> None:
    """The parent ``chmod a+x`` on workspaces_dir is emitted AFTER the
    recursive ``setfacl ... o::--- workspaces_dir``, so ``a+x`` has the last
    word on the parent and agent traverse survives. The mkdir precedes both."""
    from agentworks.vms.initializer.workspaces_dir import _setup_workspaces_directory

    target = _RecordingTarget()
    ws = "/opt/agentworks/workspaces"

    _setup_workspaces_directory(target, _make_config(ws), MagicMock())  # type: ignore[arg-type]

    mkdir_i = next(i for i, c in enumerate(target.commands) if c == f"mkdir -p {ws}")
    # The recursive access apply that sets other::--- on workspaces_dir itself.
    acl_i = next(i for i, c in enumerate(target.commands) if c.startswith("setfacl -R") and "o::---" in c and ws in c)
    # The parent-traversal re-grant loop (walks workspaces_dir up its ancestors).
    traverse_i = next(i for i, c in enumerate(target.commands) if "chmod a+x" in c and f"p={ws};" in c)

    assert mkdir_i < acl_i < traverse_i


def test_default_acl_on_dirs_still_precedes_the_traversal_regrant() -> None:
    """Both ACL applications (the default-on-dirs and the recursive access) run
    before the traverse re-grant, so the whole ACL spec is in place first."""
    from agentworks.vms.initializer.workspaces_dir import _setup_workspaces_directory

    target = _RecordingTarget()
    ws = "/opt/agentworks/workspaces"

    _setup_workspaces_directory(target, _make_config(ws), MagicMock())  # type: ignore[arg-type]

    default_acl_i = next(i for i, c in enumerate(target.commands) if c.startswith("find ") and "setfacl -d" in c)
    traverse_i = next(i for i, c in enumerate(target.commands) if "chmod a+x" in c)

    assert default_acl_i < traverse_i


def test_setup_warns_and_continues_when_a_command_fails() -> None:
    """A step failure is non-fatal: the SSHError is caught, logged, and warned,
    not raised (the operator recovers on the next reinit)."""
    from agentworks.ssh import SSHError
    from agentworks.vms.initializer.workspaces_dir import _setup_workspaces_directory

    class _FailingTarget(_RecordingTarget):
        def run(self, cmd: str, **kwargs: object) -> SimpleNamespace:
            self.commands.append(cmd)
            raise SSHError("boom")

    logger = MagicMock()
    # Must not raise.
    _setup_workspaces_directory(_FailingTarget(), _make_config(), logger)  # type: ignore[arg-type]

    assert logger.warning.called
