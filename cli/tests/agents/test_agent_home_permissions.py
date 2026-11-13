"""Agent home-directory isolation, honest agent-user reporting, and who runs each step: the
enforcement points on ``create_agent_on_vm``, the single path shared by ``agent create`` and
``agent reinit``. Exercising that function directly over a recording access proves create AND
reinit behave correctly.

Issue #228 (home isolation) added:

1. a root ``chmod 0750`` of the agent's home, and
2. a ``umask 027`` line in the managed ``~/.agentworks-profile.sh``.

Issue #252 (verifying agent-user step) made the user-setup report the
DETECTED outcome instead of an unconditional "Creating user..." line:
created when the user was absent, already-exists when it converged as a
no-op, and shell-corrected when an existing user's login shell diverged
from the template. Because the message is driven by state, not by the
caller, ``reinit`` of a truly-gone agent user honestly reports a create.

ADR 0011 splits the work: the admin bootstraps the account, then the agent
configures itself on its own access, which never elevates.

The access is a recording fake (``tests/vm_access_support.py``); the
config, registry, and resolved template are real (a minimal default
template: no git credentials, install commands, dotfiles, or mise, so
the setup body reduces to its identity/permission steps).
"""

from __future__ import annotations

import dataclasses
import json
import shutil
import subprocess
from typing import TYPE_CHECKING, Any

import pytest

from agentworks.agents import initializer as agent_initializer
from agentworks.execution.errors import CommandFailed
from agentworks.execution.requests import Bytes, Command, PythonProgram, Script
from agentworks.execution.results import Completed, Result
from tests.conftest import ManifestDoc
from tests.orchestrated_fixtures import proxmox_site, write_operator_config
from tests.vm_access_support import AccessRecorder, Admin, Agent, install_access_recorder

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from agentworks.db import Database
    from tests.vm_access_support import Call

LINUX_USER = "agt-dev"
HOME = f"/home/{LINUX_USER}"
PROFILE_BASENAME = ".agentworks-profile.sh"


def _run_create_on_vm(
    db: Database,
    config: Any,
    monkeypatch: pytest.MonkeyPatch,
    *,
    user_exists: bool,
    primary_group: str | None = None,
    shell: str = "/bin/sh",
    before_create: Callable[[AccessRecorder], None] | None = None,
    dotfiles: Path | str | None = None,
    mise_packages: list[str] | None = None,
    mise_lockfile: str | None = None,
    dotfiles_destination: str | None = None,
    user_install_commands: list[str] | None = None,
) -> AccessRecorder:
    """Drive ``create_agent_on_vm`` over a recording access and return it.

    ``user_exists`` selects the ``getent passwd`` branch: False drives the
    ``useradd`` (create) path, True the existing-user path, whose login
    shell is ``shell`` (field 7); it defaults to a value that DIFFERS from
    the default template's ``/bin/bash``, so the existing-user path takes
    the ``usermod`` shell-correction branch. ``primary_group`` is what
    ``id -gn`` reports; it defaults to the username (a private per-user
    group, the healthy case).
    """
    from agentworks.agents.templates import resolve_template
    from agentworks.bootstrap import build_registry
    from agentworks.ssh import SSHLogger

    if db.get_vm("box") is None:
        db.insert_vm("box", site="proxmox", hostname="box")
        db.update_vm_tailscale("box", "100.64.0.9")
    vm = db.get_vm("box")
    assert vm is not None
    registry = build_registry(config)
    template = resolve_template(registry, None)
    if dotfiles is not None:
        template = dataclasses.replace(template, dotfiles_source=str(dotfiles))
    if mise_packages is not None:
        template = dataclasses.replace(template, mise_packages=mise_packages)
    if mise_lockfile is not None:
        template = dataclasses.replace(template, mise_lockfile=mise_lockfile)
    if dotfiles_destination is not None:
        template = dataclasses.replace(template, dotfiles_destination=dotfiles_destination)
    if user_install_commands is not None:
        template = dataclasses.replace(template, user_install_commands=user_install_commands)
    recorder = install_access_recorder(monkeypatch)

    def answer(call: Call) -> Result | None:
        request = call.request
        if isinstance(request, Command) and request.argv == ("getent", "passwd", LINUX_USER):
            if not user_exists:
                return Result(Completed(2))
            return Result(Completed(0), f"{LINUX_USER}:x:1001:1001::{HOME}:{shell}\n".encode())
        if isinstance(request, Command) and request.argv == ("id", "-gn", LINUX_USER):
            return Result(Completed(0), f"{primary_group or LINUX_USER}\n".encode())
        return None

    recorder.answer = answer
    if before_create is not None:
        before_create(recorder)

    agent_initializer.create_agent_on_vm(
        vm,
        config,
        registry,
        template,
        LINUX_USER,
        agent_name="dev",
        credential_requests=(),
        logger=SSHLogger("box", "test-home-perms"),
        admin_git_force_safe_directory=True,
    )
    return recorder


def _commands(recorder: AccessRecorder) -> list[tuple[str, ...]]:
    return [request.argv for request in recorder.requests("run") if isinstance(request, Command)]


def _profile_writes(recorder: AccessRecorder, *, before: Callable[[object], bool] | None = None) -> list[str]:
    """Contents of every write to the managed profile fragment, or of those before the first ``before`` matches."""
    contents = []
    for request in recorder.requests("run"):
        if before is not None and before(request):
            break
        if isinstance(request, PythonProgram) and request.args[0] == "write" and PROFILE_BASENAME in request.args[1]:
            assert isinstance(request.stdin, Bytes)
            contents.append(request.stdin.data.partition(b"\n")[2].decode())
    return contents


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    monkeypatch.setenv("AW_SECRET_PROXMOX_TOKEN", "pve-token")
    return write_operator_config(tmp_path, manifests=[proxmox_site(), TOOL])


TOOL = ManifestDoc("user-install-command", "tool", {"command": "install-tool", "path": ["~/.local/bin", "/opt/tools"]})


def test_create_useradd_forces_private_group(db: Database, config: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The create-path ``useradd`` carries ``-U`` so the agent's primary
    group is a per-user private group regardless of the image's
    ``USERGROUPS_ENAB``; without it, 0750 could leak the home to a shared
    primary group."""
    recorder = _run_create_on_vm(db, config, monkeypatch, user_exists=False)

    useradd = [argv for argv in _commands(recorder) if argv[0] == "useradd"]
    assert useradd == [("useradd", "-m", "-U", "-s", "/bin/bash", LINUX_USER)]


def test_create_chmods_home_0750_as_root(db: Database, config: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """create runs an idempotent ``chmod 0750`` of the agent's home as root
    on the admin's access (the agent's own access does not work before its
    ``authorized_keys`` exists, and the home is world-readable otherwise)."""
    recorder = _run_create_on_vm(db, config, monkeypatch, user_exists=False)

    assert Command(("chmod", "0750", HOME), elevated=True) in recorder.access(Admin()).requests()


def test_chmod_ordered_after_user_creation_and_before_ssh_placement(
    db: Database, config: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``chmod 0750`` must land AFTER the useradd/usermod (the home
    must exist) and BEFORE ``authorized_keys`` is placed in ``~/.ssh``
    (the stated phase-1 placement). Pins order so a reorder that moved the
    chmod ahead of user creation, a real runtime failure, cannot pass on
    presence alone."""
    recorder = _run_create_on_vm(db, config, monkeypatch, user_exists=False)
    requests = recorder.requests("run")

    def first(matches: Callable[[Any], bool]) -> int:
        return next(index for index, request in enumerate(requests) if matches(request))

    user_create = first(lambda r: isinstance(r, Command) and r.argv[0] in ("useradd", "usermod"))
    chmod = first(lambda r: isinstance(r, Command) and r.argv == ("chmod", "0750", HOME))
    ssh_placement = first(lambda r: isinstance(r, PythonProgram) and f"{HOME}/.ssh" in r.args[1])

    assert user_create < chmod < ssh_placement


def test_shared_primary_group_warns(
    db: Database,
    config: Any,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: Any,
) -> None:
    """When the agent's primary group is shared (``id -gn`` != username),
    the post-condition guard surfaces a warning rather than silently
    leaving a 0750 home that other group members can read."""
    _run_create_on_vm(db, config, monkeypatch, user_exists=True, primary_group="users")

    assert any("primary group is 'users'" in w for w in captured_output.warnings)


def test_private_primary_group_does_not_warn(
    db: Database,
    config: Any,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: Any,
) -> None:
    """The healthy case (``id -gn`` == username) raises no group warning."""
    _run_create_on_vm(db, config, monkeypatch, user_exists=False)

    assert not any("primary group" in w for w in captured_output.warnings)


def test_create_writes_umask_027_into_profile(db: Database, config: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The managed profile fragment carries ``umask 027`` so files the
    agent writes outside a workspace default to owner-only."""
    recorder = _run_create_on_vm(db, config, monkeypatch, user_exists=False)

    (content,) = _profile_writes(recorder)
    assert "umask 027" in content


@pytest.mark.parametrize("shell", ["sh", "bash", "zsh"])
def test_on_a_first_create_an_install_command_s_profile_holds_mise_s_shims_behind_its_paths(
    db: Database, config: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, shell: str
) -> None:
    """The profile the install command's login shell sources is already complete, as the admin's is."""
    shell_path = shutil.which(shell)
    if shell_path is None:
        pytest.skip(f"{shell} is unavailable")
    recorder = _run_create_on_vm(db, config, monkeypatch, user_exists=False, user_install_commands=["tool"])

    def runs_the_tool(request: object) -> bool:
        return isinstance(request, Command) and request.argv[-1] == "install-tool"

    assert any(runs_the_tool(request) for request in recorder.requests("run"))
    (content,) = _profile_writes(recorder, before=runs_the_tool)
    profile = tmp_path / "profile.sh"
    profile.write_text(content)
    result = subprocess.run(
        [shell_path, "-c", '. "$1"; printf "%s" "$PATH"', shell, str(profile)],
        env={"HOME": HOME, "PATH": "/usr/bin:/bin"},
        check=True,
        capture_output=True,
        text=True,
    )
    # Each entry prepends, so the last declared leads.
    assert result.stdout == f"/opt/tools:{HOME}/.local/bin:{HOME}/.local/share/mise/shims:/usr/bin:/bin"


INSTALLERS = [
    ManifestDoc("user-install-command", "first", {"command": "install-first", "path": ["~/.first/bin"]}),
    ManifestDoc("user-install-command", "broken", {"command": "install-broken", "path": ["~/.broken/bin"]}),
    ManifestDoc(
        "user-install-command",
        "present",
        {"command": "install-present", "test_file": "~/.present", "path": ["~/.present/bin"]},
    ),
    ManifestDoc("user-install-command", "last", {"command": "install-last", "path": ["~/.last/bin"]}),
]


@pytest.mark.parametrize("shell", ["sh", "bash", "zsh"])
def test_a_failed_install_command_s_path_leaves_the_profile_on_reinit_and_the_rest_continue(
    db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, captured_output: Any, shell: str
) -> None:
    """Paths of successful and already-installed commands stay; a failed command's go, and the batch goes on."""
    shell_path = shutil.which(shell)
    if shell_path is None:
        pytest.skip(f"{shell} is unavailable")
    monkeypatch.setenv("AW_SECRET_PROXMOX_TOKEN", "pve-token")
    config = write_operator_config(tmp_path, manifests=[proxmox_site(), *INSTALLERS])
    names = ["first", "broken", "present", "last"]
    for fail_install in (False, True):

        def scripted(recorder: AccessRecorder, fail_install: bool = fail_install) -> None:
            base = recorder.answer

            def answer(call: Call) -> Result | BaseException | None:
                request = call.request
                if fail_install and isinstance(request, Command) and request.argv[-1] == "install-broken":
                    raise CommandFailed("box", 1, Result(Completed(1)), streams_logged=True)
                if isinstance(request, Script) and f"test -f {HOME}/.present" in request.source:
                    return Result(Completed(0))
                return base(call) if base is not None else None

            recorder.answer = answer

        recorder = _run_create_on_vm(
            db,
            config,
            monkeypatch,
            user_exists=fail_install,
            shell=shell_path,
            before_create=scripted,
            user_install_commands=names,
        )
        profile = tmp_path / "profile.sh"
        profile.write_text(_profile_writes(recorder)[-1])
        result = subprocess.run(
            [shell_path, "-c", '. "$1"; printf "%s\\n" "$PATH" "$AGENTWORKS_AGENT"', shell, str(profile)],
            env={"HOME": HOME, "PATH": "/usr/bin:/bin"},
            check=True,
            capture_output=True,
            text=True,
        )
        expected = ["last", "present"] + ([] if fail_install else ["broken"]) + ["first"]
        assert result.stdout.splitlines() == [
            ":".join(
                [*(f"{HOME}/.{name}/bin" for name in expected), f"{HOME}/.local/share/mise/shims", "/usr/bin:/bin"]
            ),
            "dev",
        ]
        installs = [
            request.argv[-1]
            for request in recorder.requests("run")
            if isinstance(request, Command) and request.argv[-1].startswith("install-")
        ]
        assert installs == [f"install-{name}" for name in ["first", "broken", "last"]]
    assert len([warning for warning in captured_output.warnings if "broken" in warning]) == 1


def test_reinit_reapplies_both_enforcements(db: Database, config: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reinit (existing user with a diverged shell: the ``usermod``
    branch) re-applies both the ``chmod 0750`` and the ``umask 027``, so a
    pre-existing world-readable agent home is repaired on the next reinit."""
    recorder = _run_create_on_vm(db, config, monkeypatch, user_exists=True)

    commands = _commands(recorder)
    assert ("usermod", "-s", "/bin/bash", LINUX_USER) in commands
    assert not any(argv[0] == "useradd" for argv in commands)
    assert ("chmod", "0750", HOME) in commands
    writes = _profile_writes(recorder)
    assert writes and all("umask 027" in content for content in writes)


# -- who runs each step (ADR 0011) ---------------------------------------------


def test_after_the_bootstrap_every_request_is_the_agent_s_own_and_none_is_elevated(
    db: Database, config: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The admin runs only the account, the home's mode and group, the socket
    directories and ``authorized_keys``; once ``authorized_keys`` is in place,
    the agent configures itself on its own access, with no request elevated
    and none carrying an environment (provisioning is hermetic)."""
    recorder = _run_create_on_vm(db, config, monkeypatch, user_exists=False)
    calls = [call for call in recorder.calls if call.operation == "run"]
    keys = next(
        index
        for index, call in enumerate(calls)
        if isinstance(call.request, PythonProgram)
        and call.request.args[0] == "write"
        and json.loads(call.request.args[1])["path"] == f"{HOME}/.ssh/authorized_keys"
    )

    assert all(call.identity == Admin() for call in calls[: keys + 1])
    agent_phase = calls[keys + 1 :]
    assert agent_phase and all(call.identity == Agent("dev", LINUX_USER) for call in agent_phase)
    assert not any(call.request.elevated or call.request.env for call in agent_phase)


# -- honest agent-user reporting (issue #252) ---------------------------------


def test_create_absent_user_reports_created_not_creating(
    db: Database, config: Any, monkeypatch: pytest.MonkeyPatch, captured_output: Any
) -> None:
    """First-time create (user absent): ``useradd`` runs and the step
    reports the user was CREATED. The old unconditional "Creating
    user..." wording is gone."""
    recorder = _run_create_on_vm(db, config, monkeypatch, user_exists=False)

    assert any(argv[0] == "useradd" for argv in _commands(recorder))
    assert any(f"Created agent user '{LINUX_USER}'" in m for m in captured_output.info)
    assert not any("Creating user" in m for m in captured_output.info)
    assert not any("already exists" in m for m in captured_output.info)


def test_reinit_converged_user_reports_already_exists_and_skips_usermod(
    db: Database, config: Any, monkeypatch: pytest.MonkeyPatch, captured_output: Any
) -> None:
    """Reinit of a healthy agent whose shell already matches the template
    (``/bin/bash``): a converging no-op. The step reports the user already
    exists, does NOT say "Creating user", and runs no ``usermod`` (nothing
    to converge), matching ``workspace repair``'s "OK" no-op reporting."""
    recorder = _run_create_on_vm(db, config, monkeypatch, user_exists=True, shell="/bin/bash")

    assert not any(argv[0] == "useradd" or argv[:2] == ("usermod", "-s") for argv in _commands(recorder))
    assert any(f"Agent user '{LINUX_USER}' already exists" in m for m in captured_output.info)
    assert not any("Created agent user" in m for m in captured_output.info)


def test_reinit_diverged_shell_reports_fixed_and_runs_usermod(
    db: Database, config: Any, monkeypatch: pytest.MonkeyPatch, captured_output: Any
) -> None:
    """Reinit of an existing user whose login shell diverges from the
    template (``/bin/zsh`` on the VM vs. the default template's
    ``/bin/bash``): ``usermod -s`` converges it and the step reports the
    correction with the before/after shells."""
    recorder = _run_create_on_vm(db, config, monkeypatch, user_exists=True, shell="/bin/zsh")

    assert ("usermod", "-s", "/bin/bash", LINUX_USER) in _commands(recorder)
    assert not any(argv[0] == "useradd" for argv in _commands(recorder))
    assert any(f"Fixed agent user '{LINUX_USER}'" in m and "/bin/zsh -> /bin/bash" in m for m in captured_output.info)
    assert not any("already exists" in m for m in captured_output.info)


def test_the_profile_fragment_is_written_before_the_install_commands_run(
    db: Database, config: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Install commands see ``AGENTWORKS_AGENT`` only through the profile fragment, so it comes first."""
    profile_writes_seen: list[int] = []
    from agentworks.vms.initializer.packages import _run_install_commands as run_install_commands

    def watch(recorder: AccessRecorder) -> None:
        def record_order(*args: Any, **kwargs: Any) -> None:
            profile_writes_seen.append(len(_profile_writes(recorder)))
            run_install_commands(*args, **kwargs)

        monkeypatch.setattr("agentworks.agents.initializer._run_install_commands", record_order)

    _run_create_on_vm(db, config, monkeypatch, user_exists=False, before_create=watch)

    assert profile_writes_seen == [1]


def test_the_source_lines_are_ensured_after_the_dotfiles_install(
    db: Database, config: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A dotfiles installer that replaces an rc file cannot leave it without its source lines."""
    (tmp_path / "dots").mkdir()
    recorder = _run_create_on_vm(db, config, monkeypatch, user_exists=False, dotfiles=tmp_path / "dots")
    requests = recorder.requests("run")

    install = requests.index(Command(("bash", "-lc", "./install.sh"), cwd=f"{HOME}/.dotfiles", deadline=120))
    bashrc_reads = [
        index
        for index, request in enumerate(requests)
        if isinstance(request, PythonProgram)
        and request.args[0] == "read"
        and json.loads(request.args[1])["path"] == f"{HOME}/.bashrc"
    ]
    assert bashrc_reads and max(bashrc_reads) > install


def test_a_failed_git_dotfiles_sync_skips_the_agent_install(
    db: Database, config: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The agent's dotfiles take the admin's sync: a fetch that fails warns and the install never runs."""
    url = "https://example.com/dots.git"
    dest = f"{HOME}/.dotfiles"

    def fail_the_fetch(recorder: AccessRecorder) -> None:
        answer = recorder.answer

        def scripted(call: Call) -> Result | BaseException | None:
            argv = call.request.argv if isinstance(call.request, Command) else ()
            if argv == ("git", "-C", dest, "config", "--get", "remote.origin.url"):
                return Result(Completed(0), f"{url}\n".encode())
            if argv[:4] == ("git", "-C", dest, "fetch"):
                failed = Result(Completed(1), stderr=b"fatal: couldn't find remote ref\n")
                return CommandFailed("box", 1, failed, streams_logged=True) if call.options["check"] else failed
            return answer(call) if answer is not None else None

        recorder.answer = scripted

    recorder = _run_create_on_vm(
        db, config, monkeypatch, user_exists=False, dotfiles=f"git::{url}", before_create=fail_the_fetch
    )

    commands = _commands(recorder)
    assert any(argv[:4] == ("git", "-C", dest, "fetch") for argv in commands)
    assert ("bash", "-lc", "./install.sh") not in commands


def _file_op(request: object, operation: str, path: str) -> bool:
    return (
        isinstance(request, PythonProgram)
        and request.args[0] == operation
        and json.loads(request.args[1])["path"] == path
    )


def _written(request: object) -> str:
    assert isinstance(request, PythonProgram) and isinstance(request.stdin, Bytes)
    return request.stdin.data.partition(b"\n")[2].decode()


def _runs_the_tool(request: object) -> bool:
    return isinstance(request, Command) and request.argv[-1] == "install-tool"


def test_the_agent_follows_the_admin_s_fragment_mise_dotfiles_and_install_command_order(
    db: Database, config: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The documented order (docs/guides/mise.md), as the admin's: the profile and rc fragments, the
    mise config, dotfiles, the mise lockfile and install, the source lines again, the user install
    commands, which may depend on mise's tools, and a final pass over the source lines."""
    (tmp_path / "dots").mkdir()
    (tmp_path / "mise.lock").write_text("[tools]\n")
    recorder = _run_create_on_vm(
        db,
        config,
        monkeypatch,
        user_exists=False,
        dotfiles=tmp_path / "dots",
        mise_packages=["jq@1"],
        mise_lockfile=str(tmp_path / "mise.lock"),
        user_install_commands=["tool"],
    )
    requests = recorder.requests("run")

    def first(predicate: Callable[[Any], bool]) -> int:
        return next(index for index, request in enumerate(requests) if predicate(request))

    profile = first(lambda r: _file_op(r, "write", f"{HOME}/{PROFILE_BASENAME}"))
    rc = first(lambda r: _file_op(r, "write", f"{HOME}/.agentworks-rc.sh"))
    config_write = first(lambda r: _file_op(r, "write", f"{HOME}/.config/mise/config.toml"))
    tree_upload = first(lambda r: isinstance(r, Script) and "AGW_TREE_DEST" in r.env)
    lockfile = first(lambda r: _file_op(r, "write", f"{HOME}/.config/mise/mise.lock"))
    install = first(lambda r: isinstance(r, Command) and r.argv[-1].startswith("mise install"))
    tool = first(_runs_the_tool)
    profile_line_reads = [index for index, r in enumerate(requests) if _file_op(r, "read", f"{HOME}/.profile")]
    assert profile < rc < config_write < tree_upload < lockfile < install < tool
    assert any(install < index < tool for index in profile_line_reads)
    assert max(profile_line_reads) > tool


def test_an_install_command_s_login_shell_still_reads_the_profile_after_dotfiles_replace_dot_profile(
    db: Database, config: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A dotfiles installer that replaces ``~/.profile`` gets its source line back before the install commands."""
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is unavailable")
    (tmp_path / "dots").mkdir()
    replaced = b"# the dotfiles' own profile\n"

    def replace_dot_profile(recorder: AccessRecorder) -> None:
        answer = recorder.answer
        installed: list[bool] = []

        def scripted(call: Call) -> Result | BaseException | None:
            if call.request == Command(("bash", "-lc", "./install.sh"), cwd=f"{HOME}/.dotfiles", deadline=120):
                installed.append(True)
            elif installed and _file_op(call.request, "read", f"{HOME}/.profile"):
                return Result(Completed(0), replaced)
            return answer(call) if answer is not None else None

        recorder.answer = scripted

    recorder = _run_create_on_vm(
        db,
        config,
        monkeypatch,
        user_exists=False,
        dotfiles=tmp_path / "dots",
        user_install_commands=["tool"],
        before_create=replace_dot_profile,
    )
    requests = recorder.requests("run")
    tool = next(index for index, request in enumerate(requests) if _runs_the_tool(request))
    (fragment,) = _profile_writes(recorder, before=_runs_the_tool)
    restored = [
        _written(request)
        for request in requests[:tool]
        if _file_op(request, "write_if", f"{HOME}/.profile") and _written(request).startswith(replaced.decode())
    ]
    assert restored

    home = tmp_path / "agent-home"
    home.mkdir()
    (home / PROFILE_BASENAME).write_text(fragment.replace(HOME, str(home)))
    (home / ".profile").write_text(restored[-1].replace(HOME, str(home)))
    result = subprocess.run(
        [bash, "-lc", 'printf "%s" "$PATH"'],
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.startswith(f"/opt/tools:{home}/.local/bin:{home}/.local/share/mise/shims:")


def test_dotfiles_that_supply_a_mise_config_get_an_install_without_mise_packages(
    db: Database, config: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The admin's guard: a config in the home after the dotfiles sync is installed from."""
    (tmp_path / "dots").mkdir()
    recorder = _run_create_on_vm(db, config, monkeypatch, user_exists=False, dotfiles=tmp_path / "dots")

    commands = _commands(recorder)
    assert ("test", "-f", f"{HOME}/.config/mise/config.toml") in commands
    assert any(argv[-1].startswith("mise install") for argv in commands)


def test_an_agent_s_dotfiles_may_own_its_config_directory(
    db: Database, config: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The workspace tmuxinator links live in the admin's home only, so an agent's ~/.config is its dotfiles'."""
    (tmp_path / "dots").mkdir()
    recorder = _run_create_on_vm(
        db, config, monkeypatch, user_exists=False, dotfiles=tmp_path / "dots", dotfiles_destination=f"{HOME}/.config"
    )

    uploads = [r for r in recorder.requests("run") if isinstance(r, Script) and "AGW_TREE_DEST" in r.env]
    assert [upload.env["AGW_TREE_DEST"] for upload in uploads] == [f"{HOME}/.config"]
