"""Runtime context at installer and dotfiles interpretation boundaries."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Any

import pytest

from agentworks.agents.initializer import create_agent_on_vm
from agentworks.agents.templates import ResolvedAgentTemplate
from agentworks.bootstrap import build_registry
from agentworks.debian import DebianRelease
from agentworks.errors import ValidationError
from agentworks.execution.requests import Command, Script
from agentworks.execution.results import Completed, Result
from agentworks.install_commands import SystemInstallCommandEntry, UserInstallCommandEntry
from agentworks.interpolation import context_values
from agentworks.vms.admin import AdminConfig
from agentworks.vms.initializer.driver import VMInitializationOperation, _phase_b_setup
from agentworks.vms.initializer.packages import _run_install_commands, install_command_paths, install_run
from agentworks.vms.initializer.shell_env import profile_fragment
from agentworks.vms.templates import ResolvedVMTemplate
from tests.conftest import requires_posix_shell
from tests.phase_b_support import StepGuest
from tests.vm_access_support import install_access_recorder

if TYPE_CHECKING:
    from pathlib import Path

    from agentworks.db import Database
    from tests.vm_access_support import Call


def _context(user: str, *, agent: bool) -> dict[str, str]:
    return context_values(vm_name="box", user=user, agent_name="dev" if agent else None)


_Entry = SystemInstallCommandEntry | UserInstallCommandEntry


def _run(guest: StepGuest, entry: _Entry, context: dict[str, str]) -> list[str]:
    """Run ``entry`` alone as the account ``context`` names, in bash, returning the names that failed."""
    home = context["user_home_dir"]
    return _run_install_commands(
        guest.files, [entry.name], {entry.name: entry}, "bash", home, guest.log, context=context
    )


@pytest.mark.parametrize("agent", [False, True])
def test_install_fields_use_the_same_consuming_identity(agent: bool) -> None:
    user = "agt-dev" if agent else "operator"
    entry = UserInstallCommandEntry(
        name="tool",
        command="printf '%s' '${agw:user_home_dir}/${agw:vm_name}'",
        test_exec="${agw:user_home_dir}/.tool/bin/tool",
        test_file="${agw:user_home_dir}/.tool/installed",
        test_dir="${agw:user_home_dir}/.tool",
    )
    guest = StepGuest(local_files=False)
    guest.access.answer = lambda call: Result(Completed(1)) if isinstance(call.request, Script) else None

    _run(guest, entry, _context(user, agent=agent))

    check, install = guest.commands()
    assert isinstance(check, Script)
    assert f"/home/{user}/.tool/bin/tool" in check.source
    assert f"test -f /home/{user}/.tool/installed" in check.source
    assert f"test -d /home/{user}/.tool" in check.source
    assert install == install_run("bash", f"printf '%s' '/home/{user}/box'")
    assert entry.command == "printf '%s' '${agw:user_home_dir}/${agw:vm_name}'"


def test_skipped_install_does_not_render_its_unselected_command() -> None:
    entry = UserInstallCommandEntry(
        name="present", command="${agw:session_name}", test_file="${agw:user_home_dir}/installed"
    )
    guest = StepGuest(local_files=False)

    assert _run(guest, entry, _context("operator", agent=False)) == []
    assert [request.source for request in guest.commands() if isinstance(request, Script)] == [
        "test -f /home/operator/installed"
    ]
    assert len(guest.commands()) == 1


@requires_posix_shell
@pytest.mark.parametrize("agent", [False, True])
@pytest.mark.parametrize("outcome", ["installed", "skipped", "failed"])
def test_install_paths_render_before_profile_shell_expansion(agent: bool, outcome: str) -> None:
    user = "agt-dev" if agent else "operator"
    authored_paths = [
        "${agw:user_home_dir}/.tool/${agw:vm_name}/bin",
        "$HOME/.local/bin",
        "~/.tilde/bin",
        "${TOOLS_ROOT}/${agw:user_name}/bin",
    ]
    entry = UserInstallCommandEntry(
        name="tool",
        command="${agw:session_name}" if outcome == "skipped" else "install-tool",
        test_exec="tool",
        path=authored_paths,
    )
    guest = StepGuest(local_files=False)

    def answer(call: Call) -> Result | None:
        if isinstance(call.request, Script):
            return Result(Completed(0 if outcome == "skipped" else 1))
        if outcome == "failed" and call.request == install_run("bash", "install-tool"):
            return Result(Completed(1))
        return None

    guest.access.answer = answer
    context = _context(user, agent=agent)

    failed = _run(guest, entry, context)
    kept = [name for name in [entry.name] if name not in failed]
    paths = install_command_paths(kept, {entry.name: entry}, context=context)

    assert paths == (
        []
        if outcome == "failed"
        else [f"/home/{user}/.tool/box/bin", "$HOME/.local/bin", "~/.tilde/bin", f"${{TOOLS_ROOT}}/{user}/bin"]
    )
    assert entry.path == authored_paths
    result = subprocess.run(
        ["/bin/sh"],
        input=profile_fragment(paths) + 'printf "%s" "$PATH"',
        env={"HOME": "/shell/home", "TOOLS_ROOT": "/opt/tools", "PATH": "/base/bin"},
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout == (
        "/base/bin"
        if outcome == "failed"
        else f"/opt/tools/{user}/bin:/shell/home/.tilde/bin:/shell/home/.local/bin:/home/{user}/.tool/box/bin:/base/bin"
    )


def test_install_path_rejects_unavailable_context_before_the_profile_is_written() -> None:
    """The declared paths render before the profile write that carries them, so nothing reaches the guest."""
    entry = UserInstallCommandEntry(
        name="tool", command="install-tool", test_exec="tool", path=["/opt/bin", "${agw:workspace_dir}/bin"]
    )

    with pytest.raises(ValidationError) as caught:
        install_command_paths([entry.name], {entry.name: entry}, context=_context("operator", agent=False))

    assert caught.value.entity_name == "user-install-command/tool.path[1]"


@pytest.mark.parametrize(
    ("entry_type", "kind"),
    [(SystemInstallCommandEntry, "system-install-command"), (UserInstallCommandEntry, "user-install-command")],
)
def test_install_check_rejects_unavailable_setup_context_before_remote_work(
    entry_type: type[SystemInstallCommandEntry] | type[UserInstallCommandEntry], kind: str
) -> None:
    entry = entry_type(name="tool", command="install-tool", test_dir="${agw:workspace_dir}")
    guest = StepGuest(local_files=False)

    with pytest.raises(ValidationError) as caught:
        _run(guest, entry, _context("operator", agent=False))

    assert caught.value.entity_name == f"{kind}/tool.test_dir"
    assert guest.access.requests() == []


@pytest.mark.parametrize("agent", [False, True])
def test_dotfiles_render_target_fields_and_keep_source_literal(
    agent: bool, db: Database, make_config: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = make_config()
    registry = build_registry(config)
    db.insert_vm("box", site="proxmox", hostname="box")
    db.update_vm_tailscale("box", "100.64.0.9")
    vm = db.get_vm("box")
    assert vm is not None
    source = str(tmp_path / "${agw:user_name}")
    user = "agt-dev" if agent else "operator"
    fields = {
        "dotfiles_source": source,
        "dotfiles_destination": "${agw:user_home_dir}/a project's ${agw:vm_name}/dotfiles",
        "dotfiles_install_cmd": "printf '%s' '${agw:user_name}'",
    }
    synced: dict[str, object] = {}
    module = "agentworks.agents.initializer" if agent else "agentworks.vms.initializer.driver"
    monkeypatch.setattr(f"{module}.sync_dotfiles", lambda files, **kwargs: synced.update(kwargs))
    if agent:
        recorder = install_access_recorder(monkeypatch)
        recorder.answer = lambda call: (
            Result(Completed(2))
            if isinstance(call.request, Command) and call.request.argv[:2] == ("getent", "passwd")
            else None
        )
        create_agent_on_vm(
            vm,
            config,
            registry,
            ResolvedAgentTemplate(name="default", **fields),
            user,
            agent_name="dev",
            credential_requests=(),
            logger=StepGuest().log,
            admin_git_force_safe_directory=False,
        )
    else:
        _admin_phase_b(db, config, registry, monkeypatch, AdminConfig(name="default", username=user, **fields))

    assert synced["source"] == source
    assert synced["destination"] == f"/home/{user}/a project's box/dotfiles"
    assert synced["install_cmd"] == f"printf '%s' '{user}'"


@pytest.mark.parametrize(
    ("destination", "uploaded"),
    [("${agw:user_home_dir}/.ssh/dots", None), ("${agw:user_home_dir}/dots", "/home/operator/dots")],
)
def test_a_templated_destination_is_checked_as_rendered_before_the_sync(
    destination: str,
    uploaded: str | None,
    db: Database,
    make_config: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A destination that renders onto a managed path is refused before anything is uploaded."""
    config = make_config()
    db.insert_vm("box", site="proxmox", hostname="box")
    (tmp_path / "dots").mkdir()
    admin = AdminConfig(
        name="default", username="operator", dotfiles_source=str(tmp_path / "dots"), dotfiles_destination=destination
    )

    guest = _admin_phase_b(db, config, build_registry(config), monkeypatch, admin)

    uploads = [
        request.env["AGW_TREE_DEST"]
        for request in guest.access.requests()
        if isinstance(request, Script) and "AGW_TREE_DEST" in request.env
    ]
    assert uploads == ([] if uploaded is None else [uploaded])


def _admin_phase_b(
    db: Database, config: Any, registry: Any, monkeypatch: pytest.MonkeyPatch, admin: AdminConfig
) -> StepGuest:
    """Run Phase B for ``admin`` on VM ``box`` over a recording access, returning its guest."""
    monkeypatch.setattr("agentworks.vms.sites.site_platform_name", lambda site, registry: "proxmox")
    guest = StepGuest(local_files=False)
    _phase_b_setup(
        db,
        config,
        registry,
        ResolvedVMTemplate(name="default"),
        admin.model_copy(update={"mise_activate": False}),
        "box",
        guest.files,
        (),
        f"/home/{admin.username}",
        admin.username,
        guest.log,
        debian_release=DebianRelease.TRIXIE,
        operation=VMInitializationOperation.VM_REINIT,
    )
    return guest
