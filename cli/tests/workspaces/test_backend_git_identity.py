"""Backend-level test for ``create_vm_workspace`` git identity stamping.

The workspace VM backend clones the template's repo and then stamps the
configured git identity into the checkout's own ``.git/config`` (repo-local
config, so any committer picks it up). These tests drive the backend over a
recording access and assert the ``git config`` requests it sends.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from agentworks.execution.requests import Command
from agentworks.execution.results import Completed, Result
from agentworks.workspaces.backends.vm import create_vm_workspace
from agentworks.workspaces.guest import path_occupied
from agentworks.workspaces.templates import ResolvedTemplate
from tests.vm_access_support import AccessRecorder, install_access_recorder

CHECKOUT_CONFIG = ("git", "-C", "/srv/workspaces/proj", "config", "--local")


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> AccessRecorder:
    recorder = install_access_recorder(monkeypatch)
    # The path is free, so create proceeds.
    recorder.access().respond(path_occupied("/srv/workspaces/proj"), Result(Completed(1)))
    return recorder


def _vm() -> Any:
    return SimpleNamespace(name="box", tailscale_host="100.64.0.1", admin_username="admin")


def _config() -> Any:
    return SimpleNamespace(paths=SimpleNamespace(vm_workspaces="/srv/workspaces"))


def _git_config_argvs(recorder: AccessRecorder) -> list[tuple[str, ...]]:
    return [
        request.argv
        for request in recorder.requests("run")
        if isinstance(request, Command) and "config" in request.argv and request.argv[0] == "git"
    ]


def test_git_identity_stamped_into_checkout(recorder: AccessRecorder) -> None:
    template = ResolvedTemplate(
        name="proj",
        repo="https://example.com/org/proj.git",
        git_user_name="Ada Lovelace",
        git_user_email="ada@example.com",
    )
    create_vm_workspace(_vm(), _config(), "proj", template)

    # Applied repo-locally via -C on the checkout path (never --global), each value one argument.
    assert _git_config_argvs(recorder) == [
        (*CHECKOUT_CONFIG, "user.name", "Ada Lovelace"),
        (*CHECKOUT_CONFIG, "user.email", "ada@example.com"),
    ]


def test_git_identity_partial_only_sets_provided(recorder: AccessRecorder) -> None:
    template = ResolvedTemplate(
        name="proj",
        repo="https://example.com/org/proj.git",
        git_user_email="ada@example.com",
    )
    create_vm_workspace(_vm(), _config(), "proj", template)

    assert _git_config_argvs(recorder) == [(*CHECKOUT_CONFIG, "user.email", "ada@example.com")]


def test_git_identity_skipped_when_unset(recorder: AccessRecorder) -> None:
    template = ResolvedTemplate(name="proj", repo="https://example.com/org/proj.git")
    create_vm_workspace(_vm(), _config(), "proj", template)
    assert not _git_config_argvs(recorder)


def test_git_identity_no_op_without_repo(recorder: AccessRecorder) -> None:
    # Identity set but no repo: there is no checkout to stamp, so no git runs.
    template = ResolvedTemplate(
        name="proj",
        repo=None,
        git_user_name="Ada Lovelace",
        git_user_email="ada@example.com",
    )
    create_vm_workspace(_vm(), _config(), "proj", template)
    assert not [r for r in recorder.requests("run") if isinstance(r, Command) and r.argv[0] == "git"]
