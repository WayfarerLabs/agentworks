"""An abandoned guest clone cannot mutate a later workspace using the name."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from agentworks.errors import AlreadyExistsError
from agentworks.ssh import SSHError, SSHResult
from agentworks.workspaces.backends.vm import create_vm_workspace
from agentworks.workspaces.templates import ResolvedTemplate
from agentworks.workspaces.tmuxinator import console_session_name

if TYPE_CHECKING:
    from collections.abc import Callable


pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or shutil.which("git") is None,
    reason="requires Linux filesystem commands and Git",
)


class LocalCommands:
    """Execute filesystem/Git work locally; model only guest users and groups."""

    def __init__(self, root: Path):
        self.root = root
        self.home = root / "home"
        self.home.mkdir()
        self.environment = {
            "HOME": str(self.home),
            "PATH": os.environ["PATH"],
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_ALLOW_PROTOCOL": "file:ext",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "protocol.ext.allow",
            "GIT_CONFIG_VALUE_0": "always",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_AUTHOR_NAME": "Fixture",
            "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_NAME": "Fixture",
            "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        }
        self.commands: list[str] = []
        self.groups: set[str] = set()
        self.before: Callable[[str], None] | None = None
        self.after: Callable[[str], None] | None = None
        self.interrupt: BaseException | None = None
        self.ready = root / "clone-ready"
        self.release = root / "clone-release"
        self.orphans: list[subprocess.Popen[str]] = []
        self.clone_destinations: list[Path] = []
        self.clone_parent_modes: list[int] = []

    def run(self, command: str, **kwargs: object) -> SSHResult:
        self.commands.append(command)
        if self.before is not None:
            self.before(command)
        if "groupadd" in command:
            self.groups.add("ws-project")
            return SSHResult(0, "", "")
        if "groupdel" in command:
            self.groups.discard("ws-project")
            return SSHResult(0, "", "")
        if command.startswith(("usermod ", "chown ", "chgrp ")):
            return SSHResult(0, "", "")
        if command.startswith("git clone "):
            destination = Path(shlex.split(command)[-1])
            self.clone_destinations.append(destination)
            self.clone_parent_modes.append(destination.parent.stat().st_mode & 0o777)
            if self.interrupt is not None:
                process = subprocess.Popen(
                    ["bash", "-c", command],
                    cwd=self.home,
                    env=self.environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                self.orphans.append(process)
                deadline = time.monotonic() + 20
                while not self.ready.exists():
                    if process.poll() is not None or time.monotonic() > deadline:
                        raise AssertionError("the real Git helper did not reach its wait point")
                    time.sleep(0.02)
                raise self.interrupt
        result = subprocess.run(
            ["bash", "-c", command],
            cwd=self.home,
            env=self.environment,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if kwargs.get("check", True) and result.returncode:
            raise SSHError(f"fixture command failed: {command}: {result.stderr}")
        if self.after is not None:
            self.after(command)
        return SSHResult(result.returncode, result.stdout, result.stderr)

    def write_file(self, path: str, content: str, **kwargs: object) -> None:
        destination = Path(path)
        assert destination.is_relative_to(self.root)
        destination.write_text(content)

    def finish_orphans(self) -> list[int]:
        self.release.touch()
        codes = []
        for process in self.orphans:
            try:
                process.communicate(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=10)
                raise
            assert process.returncode is not None
            codes.append(process.returncode)
        return codes


@pytest.fixture
def backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    target = LocalCommands(tmp_path)
    monkeypatch.setattr("agentworks.workspaces.backends.vm.transport", lambda *args, **kwargs: target)
    monkeypatch.setattr("agentworks.workspaces.backends.vm.apply_workspace_acls", lambda *args: None)
    vm = SimpleNamespace(name="fixture", tailscale_host="fixture", admin_username="fixture")
    config = SimpleNamespace(paths=SimpleNamespace(vm_workspaces=str(tmp_path / "workspaces")))
    try:
        yield target, vm, config, tmp_path / "workspaces" / "project"
    finally:
        target.finish_orphans()


def source_repository(target: LocalCommands) -> Path:
    source = target.root / "source"
    source.mkdir()
    (source / "tracked").write_text("original\n")
    for command in (
        f"git init -q {shlex.quote(str(source))}",
        f"git -C {shlex.quote(str(source))} add .",
        f"git -C {shlex.quote(str(source))} commit -qm original",
    ):
        assert target.run(command).ok
    return source


@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("remote_success", [False, True])
def test_late_guest_clone_cannot_destroy_a_successful_retry(backend, cancel, remote_success):
    target, vm, config, final = backend
    source = source_repository(target)
    helper = target.root / "delayed-upload-pack"
    helper.write_text(
        "#!/bin/sh\n"
        f"touch {shlex.quote(str(target.ready))}\n"
        f"while ! test -e {shlex.quote(str(target.release))}; do sleep 0.02; done\n"
        + (f"exec git-upload-pack {shlex.quote(str(source))}\n" if remote_success else "exit 1\n")
    )
    helper.chmod(0o700)
    error = KeyboardInterrupt("fixture controller cancelled") if cancel else SSHError("fixture controller timed out")
    target.interrupt = error
    with pytest.raises(type(error)) as caught:
        create_vm_workspace(vm, config, "project", ResolvedTemplate(name="slow", repo=f"ext::{helper}"))
    assert caught.value is error
    assert not final.exists() and not target.groups
    assert target.orphans[0].poll() is None

    target.interrupt = None
    create_vm_workspace(vm, config, "project", ResolvedTemplate(name="fast", repo=source.as_uri()))
    (final / "operator-work").write_text("precious unpushed work\n")
    quoted = shlex.quote(str(final))
    target.run(f"git -C {quoted} add operator-work")
    target.run(f"git -C {quoted} commit -qm operator-work")
    head = target.run(f"git -C {quoted} rev-parse HEAD").stdout
    (final / "untracked").write_text("precious untracked work\n")
    (final / "tracked").write_text("precious stashed work\n")
    target.run(f"git -C {quoted} stash push -qm operator-stash")
    stash = target.run(f"git -C {quoted} rev-parse refs/stash").stdout

    assert all(code != 0 for code in target.finish_orphans())
    assert (final / "operator-work").read_text() == "precious unpushed work\n"
    assert (final / "untracked").read_text() == "precious untracked work\n"
    assert target.run(f"git -C {quoted} rev-parse HEAD").stdout == head
    assert target.run(f"git -C {quoted} rev-parse refs/stash").stdout == stash
    assert all(destination != final for destination in target.clone_destinations)
    assert target.clone_parent_modes == [0o700, 0o700]
    assert list(final.parent.glob("*.agentworks-clone-*")) == []


@pytest.mark.parametrize("kind", ["file", "directory", "symlink", "dangling-symlink"])
def test_existing_destination_is_never_adopted_or_removed(backend, kind):
    target, vm, config, final = backend
    final.parent.mkdir()
    original = target.root / "original"
    original.mkdir()
    (original / "operator-work").write_text("precious\n")
    if kind == "file":
        final.write_text("precious\n")
    elif kind == "directory":
        final.mkdir()
        (final / "operator-work").write_text("precious\n")
    else:
        final.symlink_to(original if kind == "symlink" else target.root / "missing", target_is_directory=True)
    with pytest.raises(AlreadyExistsError):
        create_vm_workspace(vm, config, "project", ResolvedTemplate(name="empty"))
    assert final.exists() or final.is_symlink()
    assert not target.groups and not target.clone_destinations
    assert (original / "operator-work").read_text() == "precious\n"
    assert not any(command.startswith("rm ") for command in target.commands)


@pytest.mark.parametrize("lost_response", [False, True])
def test_reservation_collision_or_lost_response_never_authorizes_cleanup(backend, lost_response):
    target, vm, config, final = backend
    error = SSHError("fixture reservation response lost")

    def before(command: str) -> None:
        if command == f"mkdir -- {shlex.quote(str(final))}" and not lost_response:
            final.mkdir()
            (final / "operator-work").write_text("precious\n")

    def after(command: str) -> None:
        if command == f"mkdir -- {shlex.quote(str(final))}" and lost_response:
            raise error

    target.before, target.after = before, after
    with pytest.raises(SSHError):
        create_vm_workspace(vm, config, "project", ResolvedTemplate(name="empty"))
    assert final.is_dir() and not target.groups
    if not lost_response:
        assert (final / "operator-work").read_text() == "precious\n"
    assert not any(command.startswith("rm ") for command in target.commands)


@pytest.mark.parametrize("obstruction", ["nonempty", "file", "symlink", "lost-response"])
def test_unconfirmed_publication_retains_final_content_and_group(backend, obstruction):
    target, vm, config, final = backend
    source = source_repository(target)
    original = target.root / "original"
    original.mkdir()
    (original / "operator-work").write_text("precious\n")

    def before(command: str) -> None:
        if command.startswith("mv -T "):
            if obstruction == "nonempty":
                (final / "operator-work").write_text("precious\n")
            elif obstruction == "file":
                final.rmdir()
                final.write_text("precious\n")
            elif obstruction == "symlink":
                final.rmdir()
                final.symlink_to(original, target_is_directory=True)

    def after(command: str) -> None:
        if command.startswith("mv -T ") and obstruction == "lost-response":
            raise SSHError("fixture publication response lost")

    target.before, target.after = before, after
    with pytest.raises(SSHError):
        create_vm_workspace(vm, config, "project", ResolvedTemplate(name="repo", repo=source.as_uri()))
    assert final.exists() and target.groups == {"ws-project"}
    if obstruction == "lost-response":
        assert (final / ".git").is_dir() and (final / "tracked").read_text() == "original\n"
    elif obstruction == "file":
        assert final.read_text() == "precious\n"
    else:
        assert (final / "operator-work").read_text() == "precious\n"
    assert (original / "operator-work").read_text() == "precious\n"
    assert list(final.parent.glob("*.agentworks-clone-*")) == []


@pytest.mark.parametrize("lost_response", [False, True])
def test_unconfirmed_private_attempt_directory_is_not_removed(backend, monkeypatch, lost_response):
    target, vm, config, final = backend
    source = source_repository(target)
    candidate = Path(f"{final}.agentworks-clone-{'a' * 32}")
    monkeypatch.setattr("agentworks.workspaces.backends.vm.uuid4", lambda: SimpleNamespace(hex="a" * 32))

    def before(command: str) -> None:
        if command.startswith("mkdir -m 700 ") and not lost_response:
            candidate.mkdir(mode=0o700)
            (candidate / "operator-work").write_text("precious\n")

    def after(command: str) -> None:
        if command.startswith("mkdir -m 700 ") and lost_response:
            raise SSHError("fixture private reservation response lost")

    target.before, target.after = before, after
    with pytest.raises(SSHError):
        create_vm_workspace(vm, config, "project", ResolvedTemplate(name="repo", repo=source.as_uri()))
    assert candidate.is_dir() and candidate.stat().st_mode & 0o777 == 0o700
    assert not final.exists() and not target.groups
    if not lost_response:
        assert (candidate / "operator-work").read_text() == "precious\n"
    assert not any(command.startswith("rm -rf") for command in target.commands)


def test_failure_after_confirmed_publication_reclaims_its_checkout_and_group(backend):
    target, vm, config, final = backend
    source = source_repository(target)

    def before(command: str) -> None:
        if "config --local" in command:
            assert (final / ".git").is_dir()
            raise SSHError("fixture identity write failed")

    target.before = before
    with pytest.raises(SSHError):
        create_vm_workspace(
            vm,
            config,
            "project",
            ResolvedTemplate(name="repo", repo=source.as_uri(), git_user_name="Fixture"),
        )
    assert not final.exists() and not target.groups
    assert list(final.parent.glob("*.agentworks-clone-*")) == []


@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("cleanup_cancel", [False, True])
def test_attempt_cleanup_failure_still_cleans_empty_reservation_and_group(backend, monkeypatch, cancel, cleanup_cancel):
    target, vm, config, final = backend
    original = KeyboardInterrupt() if cancel else SSHError("clone failed")
    warnings: list[str] = []
    monkeypatch.setattr("agentworks.workspaces.backends.vm.output.warn", warnings.append)

    def before(command: str) -> None:
        if command.startswith("git clone "):
            raise original
        if command.startswith("rm -rf -- "):
            raise (
                KeyboardInterrupt()
                if cleanup_cancel
                else SSHError("cleanup transport failed with private provider details")
            )

    target.before = before
    with pytest.raises(type(original)) as caught:
        create_vm_workspace(vm, config, "project", ResolvedTemplate(name="repo", repo="https://example.invalid/repo"))
    assert caught.value is original
    attempts = list(final.parent.glob("*.agentworks-clone-*"))
    assert len(attempts) == 1
    assert not final.exists() and not target.groups
    assert any(str(attempts[0]) in warning for warning in warnings)
    assert not any("private provider details" in warning for warning in warnings)


@pytest.mark.parametrize("occupied", [False, True])
def test_unpublished_nonrepo_failure_only_removes_empty_reservation(backend, occupied):
    target, vm, config, final = backend
    original = SSHError("setup failed")

    def before(command: str) -> None:
        if command.startswith("chmod 2770 "):
            if occupied:
                (final / "operator-work").write_text("precious\n")
            raise original

    target.before = before
    with pytest.raises(SSHError) as caught:
        create_vm_workspace(vm, config, "project", ResolvedTemplate(name="empty"))
    assert caught.value is original
    if occupied:
        assert (final / "operator-work").read_text() == "precious\n"
        assert target.groups == {"ws-project"}
    else:
        assert not final.exists() and not target.groups


@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("changed", ["none", "config", "config-symlink", "link", "other-file"])
def test_late_nonrepo_failure_reclaims_only_unchanged_generated_files(backend, monkeypatch, cancel, changed):
    target, vm, config, final = backend
    original = KeyboardInterrupt() if cancel else SSHError("fixture ACL failure")
    link = target.home / f".config/tmuxinator/{console_session_name('project')}.yml"
    preserved = target.root / "preserved"
    preserved.write_text("operator content\n")

    def fail_acl(*args):
        assert (final / ".tmuxinator.yml").is_file()
        assert link.is_symlink()
        if changed == "config":
            (final / ".tmuxinator.yml").write_text("operator config\n")
        elif changed == "config-symlink":
            (final / ".tmuxinator.yml").unlink()
            (final / ".tmuxinator.yml").symlink_to(preserved)
        elif changed == "link":
            link.unlink()
            link.symlink_to(preserved)
        elif changed == "other-file":
            (final / "operator-work").write_text("precious\n")
        raise original

    monkeypatch.setattr("agentworks.workspaces.backends.vm.apply_workspace_acls", fail_acl)
    with pytest.raises(type(original)) as caught:
        create_vm_workspace(vm, config, "project", ResolvedTemplate(name="empty", tmuxinator=True))
    assert caught.value is original
    assert preserved.read_text() == "operator content\n"
    if changed in {"config", "config-symlink", "other-file"}:
        assert final.is_dir() and target.groups
        if changed == "config-symlink":
            assert (final / ".tmuxinator.yml").is_symlink()
            assert (final / ".tmuxinator.yml").resolve() == preserved
            return
        expected = final / (".tmuxinator.yml" if changed == "config" else "operator-work")
        assert expected.read_text() == ("operator config\n" if changed == "config" else "precious\n")
    else:
        assert not final.exists() and not target.groups
        if changed == "link":
            assert link.is_symlink() and link.resolve() == preserved
        else:
            assert not link.exists() and not link.is_symlink()
            monkeypatch.setattr("agentworks.workspaces.backends.vm.apply_workspace_acls", lambda *args: None)
            assert create_vm_workspace(vm, config, "project", ResolvedTemplate(name="empty", tmuxinator=True))


def test_nonrepo_create_preserves_an_existing_tmuxinator_entry(backend):
    target, vm, config, final = backend
    link = target.home / f".config/tmuxinator/{console_session_name('project')}.yml"
    link.parent.mkdir(parents=True)
    link.write_text("operator config\n")
    with pytest.raises(SSHError):
        create_vm_workspace(vm, config, "project", ResolvedTemplate(name="empty", tmuxinator=True))
    assert link.read_text() == "operator config\n"
    assert not final.exists() and not target.groups


@pytest.mark.parametrize(
    "repo", ["git@example.invalid:private/repo", "https://user:fixture-secret@example.invalid/repo"]
)
def test_clone_failure_has_safe_structured_context_without_changing_identity(backend, monkeypatch, repo):
    target, vm, config, final = backend
    original = SSHError(f"git clone {repo}: fixture-provider-blob")
    original.__cause__ = RuntimeError("fixture-provider-blob")
    messages: list[str] = []
    monkeypatch.setattr("agentworks.workspaces.backends.vm.output.info", messages.append)

    def before(command: str) -> None:
        if command.startswith("git clone "):
            raise original

    target.before = before
    with pytest.raises(SSHError) as caught:
        create_vm_workspace(vm, config, "project", ResolvedTemplate(name="repo", repo=repo))
    assert caught.value is original
    assert original.entity_kind == "workspace" and original.entity_name == "project"
    assert original.hint
    rendered = " ".join([str(original), original.hint, *messages])
    assert "fixture-secret" not in rendered and "fixture-provider-blob" not in rendered
    assert original.__cause__ is None and original.__suppress_context__
    assert not final.exists() and not target.groups
