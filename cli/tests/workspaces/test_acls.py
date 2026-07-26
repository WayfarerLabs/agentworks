"""The canonical workspace ACL helper (``apply_workspace_acls``).

The spec opens access to the owning group (``g::rwx`` + a matching mask) and
denies ``other`` on BOTH the default ACL (so new entries created inside a
workspace inherit ``default:other::---`` and are not world-readable or
world-traversable) and the recursive access ACL (so entries that already
exist lose any ``other`` bits they carried from the old ``default:other::r-x``)
per #254. These tests assert the emitted ``setfacl`` command strings, not live
ACL state, so they pin the exact spec the three call sites share.
"""

from __future__ import annotations

from types import SimpleNamespace

from agentworks.workspaces.acls import apply_workspace_acls


class _RecordingTarget:
    """Minimal transport double: records every command and answers ok."""

    def __init__(self) -> None:
        self.commands: list[str] = []

    def run(self, cmd: str, **kwargs: object) -> SimpleNamespace:
        self.commands.append(cmd)
        return SimpleNamespace(ok=True, returncode=0, stdout="", stderr="")


def _default_cmd(target: _RecordingTarget) -> str:
    """The default-ACL-on-directories command (find ... setfacl -d ...)."""
    return next(c for c in target.commands if c.startswith("find ") and "setfacl -d" in c)


def _access_cmd(target: _RecordingTarget) -> str:
    """The recursive access-ACL command (setfacl -R ...)."""
    return next(c for c in target.commands if c.startswith("setfacl -R"))


def test_default_acl_on_dirs_denies_other() -> None:
    """The per-directory default ACL sets ``o::---`` under ``-d`` (which yields
    ``default:other::---``), so entries created later are not world-readable."""
    target = _RecordingTarget()

    apply_workspace_acls(target, "/srv/ws1")  # type: ignore[arg-type]

    assert "-m o::---" in _default_cmd(target)


def test_recursive_access_acl_denies_other() -> None:
    """The recursive access ACL sets ``o::---`` over the whole existing tree,
    so entries created under the old ``default:other::r-x`` are hardened too."""
    target = _RecordingTarget()

    apply_workspace_acls(target, "/srv/ws1")  # type: ignore[arg-type]

    assert "-m o::---" in _access_cmd(target)


def test_group_and_mask_entries_are_preserved() -> None:
    """Denying ``other`` does not clobber the group / mask entries: both the
    default and access commands still open ``g::rwx`` with a matching mask, so
    owner/group collaboration is untouched."""
    target = _RecordingTarget()

    apply_workspace_acls(target, "/srv/ws1")  # type: ignore[arg-type]

    for cmd in (_default_cmd(target), _access_cmd(target)):
        assert "-m g::rwx" in cmd
        assert "-m m::rwx" in cmd
