"""Backend-level test for ``create_vm_workspace`` git identity stamping.

The workspace VM backend clones the template's repo and then stamps the
configured git identity into the checkout's own ``.git/config`` (repo-local
config, so any committer picks it up). These tests drive the backend with a
recording fake transport and assert the ``git config`` calls it emits.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from agentworks.workspaces.backends.vm import create_vm_workspace
from agentworks.workspaces.templates import ResolvedTemplate


class _Result:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.returncode = 0 if ok else 1
        self.stdout = ""
        self.stderr = ""


class _FakeTarget:
    def __init__(self) -> None:
        self.commands: list[str] = []
        self.writes: list[str] = []

    def run(self, command: str, **kwargs: Any) -> _Result:
        self.commands.append(command)
        # The directory-exists precheck must report "absent" so create proceeds.
        if command.startswith("test -d"):
            return _Result(ok=False)
        return _Result(ok=True)

    def write_file(self, path: str, content: str, **kwargs: Any) -> None:
        self.writes.append(path)


@pytest.fixture
def fake_target(monkeypatch: pytest.MonkeyPatch) -> _FakeTarget:
    target = _FakeTarget()
    monkeypatch.setattr(
        "agentworks.workspaces.backends.vm.transport",
        lambda vm, config, logger=None: target,
    )
    return target


def _vm() -> Any:
    return SimpleNamespace(name="box", tailscale_host="100.64.0.1", admin_username="admin")


def _config() -> Any:
    return SimpleNamespace(paths=SimpleNamespace(vm_workspaces="/srv/workspaces"))


def _git_config_cmds(target: _FakeTarget) -> list[str]:
    return [c for c in target.commands if " config --local user." in c]


def test_git_identity_stamped_into_checkout(fake_target: _FakeTarget) -> None:
    template = ResolvedTemplate(
        name="proj",
        repo="https://example.com/org/proj.git",
        git_user_name="Ada Lovelace",
        git_user_email="ada@example.com",
    )
    create_vm_workspace(_vm(), _config(), "proj", template)

    cmds = _git_config_cmds(fake_target)
    # Name carries a space, so shlex quotes it; the plain email is left bare.
    assert any("config --local user.name 'Ada Lovelace'" in c for c in cmds)
    assert any("config --local user.email ada@example.com" in c for c in cmds)
    # Applied repo-locally via -C on the checkout path (never --global).
    assert cmds and all("git -C /srv/workspaces/proj config --local" in c for c in cmds)


def test_git_identity_partial_only_sets_provided(fake_target: _FakeTarget) -> None:
    template = ResolvedTemplate(
        name="proj",
        repo="https://example.com/org/proj.git",
        git_user_email="ada@example.com",
    )
    create_vm_workspace(_vm(), _config(), "proj", template)

    cmds = _git_config_cmds(fake_target)
    assert any("config --local user.email" in c for c in cmds)
    assert not any("config --local user.name" in c for c in cmds)


def test_git_identity_skipped_when_unset(fake_target: _FakeTarget) -> None:
    template = ResolvedTemplate(name="proj", repo="https://example.com/org/proj.git")
    create_vm_workspace(_vm(), _config(), "proj", template)
    assert not _git_config_cmds(fake_target)


def test_git_identity_no_op_without_repo(fake_target: _FakeTarget) -> None:
    # Identity set but no repo: there is no checkout to stamp, so no git runs.
    template = ResolvedTemplate(
        name="proj",
        repo=None,
        git_user_name="Ada Lovelace",
        git_user_email="ada@example.com",
    )
    create_vm_workspace(_vm(), _config(), "proj", template)
    assert not [c for c in fake_target.commands if c.startswith("git")]
