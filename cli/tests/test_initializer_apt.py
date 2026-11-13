"""Apt sources, apt packages and install commands in Phase B.

Each step runs over a recording admin access (``tests/phase_b_support.py``):
apt source lists publish to the test's own directory through the real file
program, and every other request is recorded and answered as the test
scripts it, matched by equality with the request the step builds.
"""

from __future__ import annotations

import stat
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agentworks.apt import AptSourceEntry
from agentworks.capabilities.vm_platform.cloud_init import INIT_SYSTEM_PACKAGES
from agentworks.debian import DebianRelease
from agentworks.errors import ConfigError
from agentworks.execution.errors import CommandFailed, ExecutionIncomplete
from agentworks.execution.requests import Command, Script, Shell
from agentworks.execution.results import Completed, Result, Unknown, UnknownReason
from agentworks.install_commands import SystemInstallCommandEntry, UserInstallCommandEntry
from agentworks.vms.initializer import (
    MISE_GPG_KEY_PATH,
    MISE_GPG_KEY_URL,
    _configure_apt_sources,
    _install_apt_packages,
    _run_install_commands,
    packages,
)
from agentworks.vms.initializer.driver import VMInitializationOperation, _phase_b_setup
from agentworks.vms.initializer.packages import install_command_paths
from tests.phase_b_support import StepGuest, requires_file_program

from ._initializer_support import _make_entries, _make_vm_template

ARCH = Command(("dpkg", "--print-architecture"), deadline=60)
APT_UPDATE = Command(("apt-get", "update", "-qq"), elevated=True, deadline=120)
TEST_KEY = "/etc/apt/keyrings/test.gpg"
TEST_LIST = "deb [arch=arm64 signed-by=/etc/apt/keyrings/test.gpg] https://example.com stable main\n"


def _key_present(path: str) -> Command:
    return Command(("test", "-f", path), deadline=60)


def _failed() -> CommandFailed:
    return CommandFailed("box", 1, Result(Completed(1)), streams_logged=True)


@pytest.fixture
def guest(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> StepGuest:
    monkeypatch.setattr(packages, "APT_SOURCES_DIR", str(tmp_path / "sources.list.d"))
    guest = StepGuest()
    guest.respond(ARCH, Result(Completed(0), b"arm64\n"))
    return guest


def _configure(guest: StepGuest, *, apt_packages: list[str]) -> None:
    entries = _make_entries()
    _configure_apt_sources(
        guest.files,
        _make_vm_template(apt_packages=apt_packages),
        entries.apt_packages,
        entries.apt_sources,
        guest.log,
        debian_release=DebianRelease.TRIXIE,
    )


def _source_list(name: str = "test.list") -> Path:
    return Path(packages.APT_SOURCES_DIR) / name


# -- Apt sources ------------------------------------------------------------------


@requires_file_program
def test_a_missing_key_is_fetched_as_root_and_the_list_published_with_the_architecture(guest: StepGuest) -> None:
    guest.respond(_key_present(TEST_KEY), Result(Completed(1)))

    _configure(guest, apt_packages=["test-pkg"])

    assert _source_list().read_text() == TEST_LIST
    assert stat.S_IMODE(_source_list().stat().st_mode) == 0o644
    assert guest.commands() == [
        ARCH,
        _key_present(TEST_KEY),
        Command(("install", "-m", "0755", "-d", "/etc/apt/keyrings"), elevated=True, deadline=60),
        Command(("curl", "-fsSL", "https://example.com/key.gpg", "-o", TEST_KEY), elevated=True, deadline=60),
        Command(("chmod", "a+r", TEST_KEY), elevated=True, deadline=60),
        APT_UPDATE,
    ]
    assert guest.log.warned == []


@requires_file_program
def test_a_configured_source_is_neither_rewritten_nor_followed_by_an_update(guest: StepGuest) -> None:
    _source_list().parent.mkdir()
    _source_list().write_text(TEST_LIST)

    _configure(guest, apt_packages=["test-pkg"])

    assert guest.writes() == []
    assert guest.commands() == [ARCH, _key_present(TEST_KEY)]


@requires_file_program
def test_a_changed_source_line_is_republished_and_followed_by_an_update(guest: StepGuest) -> None:
    _source_list().parent.mkdir()
    _source_list().write_text("deb https://old.example.com stable main\n")

    _configure(guest, apt_packages=["test-pkg"])

    assert _source_list().read_text() == TEST_LIST
    assert guest.commands()[-1] == APT_UPDATE


@requires_file_program
def test_a_dearmored_key_is_fetched_through_gpg_in_one_root_script(guest: StepGuest, monkeypatch) -> None:  # noqa: ANN001
    entries = _make_entries()
    dearmor = entries.apt_sources["dearmor-source"]
    guest.respond(_key_present(dearmor.key_path), Result(Completed(1)))

    _configure_apt_sources(
        guest.files,
        _make_vm_template(apt_packages=["dearmored"]),
        {"dearmored": entries.apt_packages["test-pkg"].model_copy(update={"apt_sources": ["dearmor-source"]})},
        entries.apt_sources,
        guest.log,
        debian_release=DebianRelease.TRIXIE,
    )

    scripts = [request for request in guest.commands() if isinstance(request, Script)]
    assert len(scripts) == 1
    assert (scripts[0].shell, scripts[0].elevated) == (Shell.SH, True)
    assert _source_list("dearmor.list").exists()


@requires_file_program
def test_a_failed_key_fetch_warns_and_skips_that_sources_list(guest: StepGuest) -> None:
    guest.respond(_key_present(TEST_KEY), Result(Completed(1)))
    guest.respond(
        Command(("curl", "-fsSL", "https://example.com/key.gpg", "-o", TEST_KEY), elevated=True, deadline=60),
        _failed(),
    )

    _configure(guest, apt_packages=["test-pkg"])

    assert len(guest.log.warned) == 1
    assert not _source_list().exists()
    assert APT_UPDATE not in guest.commands()


def test_no_selected_packages_means_no_request() -> None:
    guest = StepGuest(local_files=False)

    _configure(guest, apt_packages=[])

    assert guest.access.requests() == []


def test_an_architecture_probe_that_cannot_run_warns_without_raising() -> None:
    guest = StepGuest(local_files=False)
    guest.respond(ARCH, ExecutionIncomplete("box", Result(Unknown(UnknownReason.CONNECTION))))

    _configure(guest, apt_packages=["test-pkg"])

    assert len(guest.log.warned) == 1


def test_configure_apt_sources_resolves_all_mappings_before_guest_mutation() -> None:
    guest = StepGuest(local_files=False)
    vm_template = _make_vm_template(apt_packages=["test-pkg"])
    entries = _make_entries()
    entries.apt_sources["test-source"] = AptSourceEntry(
        name="test-source",
        key_url="https://example.com/key.gpg",
        key_path=TEST_KEY,
        source=None,
        sources={DebianRelease.TRIXIE: "deb https://example.com trixie main"},
        source_file="test.list",
    )

    with pytest.raises(ConfigError) as caught:
        _configure_apt_sources(
            guest.files,
            vm_template,
            entries.apt_packages,
            entries.apt_sources,
            guest.log,
            debian_release=DebianRelease.BOOKWORM,
        )

    assert caught.value.entity_kind == "apt-source"
    assert guest.access.requests() == []


def test_phase_b_resolves_all_release_maps_before_any_guest_call(monkeypatch: pytest.MonkeyPatch) -> None:
    guest = StepGuest(local_files=False)
    vm_template = _make_vm_template(apt_packages=["test-pkg"])
    entries = _make_entries()
    entries.apt_sources["test-source"] = AptSourceEntry(
        name="test-source",
        key_url="https://example.com/key.gpg",
        key_path=TEST_KEY,
        source=None,
        sources={DebianRelease.TRIXIE: "deb https://example.com trixie main"},
        source_file="test.list",
    )
    resources = {
        "apt-source": entries.apt_sources,
        "apt-package": entries.apt_packages,
        "system-install-command": entries.system_install_commands,
        "user-install-command": entries.user_install_commands,
    }
    monkeypatch.setattr("agentworks.resources.access.kind_dict", lambda registry, kind: resources[kind])

    with pytest.raises(ConfigError) as caught:
        _phase_b_setup(
            MagicMock(),
            MagicMock(),
            MagicMock(),
            vm_template,
            MagicMock(),
            "mapped-vm",
            guest.files,
            (),
            "/home/operator",
            "operator",
            MagicMock(),
            debian_release=DebianRelease.BOOKWORM,
            operation=VMInitializationOperation.VM_REINIT,
        )

    assert caught.value.entity_kind == "apt-source"
    assert guest.access.requests() == []


# -- System and apt packages ------------------------------------------------------------


def _apt_install(*names: str) -> Command:
    return Command(
        ("apt-get", "install", "-y", "-qq", "-o", "Dpkg::Options::=--force-confnew", *names),
        env={"DEBIAN_FRONTEND": "noninteractive"},
        elevated=True,
        deadline=300,
    )


def test_apt_packages_install_the_direct_list_and_each_entrys_packages_in_one_request() -> None:
    guest = StepGuest(local_files=False)
    entries = _make_entries()

    _install_apt_packages(
        guest.files, _make_vm_template(apt=["vim", "curl"], apt_packages=["test-pkg"]), entries.apt_packages, guest.log
    )

    assert guest.commands() == [_apt_install("vim", "curl", "test-tool")]


def test_no_apt_packages_means_no_request() -> None:
    guest = StepGuest(local_files=False)

    _install_apt_packages(guest.files, _make_vm_template(), _make_entries().apt_packages, guest.log)

    assert guest.access.requests() == []


def test_a_failed_apt_package_install_warns_without_raising() -> None:
    guest = StepGuest(local_files=False)
    guest.respond(_apt_install("vim"), _failed())

    _install_apt_packages(guest.files, _make_vm_template(apt=["vim"]), _make_entries().apt_packages, guest.log)

    assert len(guest.log.warned) == 1


def test_a_failed_system_package_install_stops_initialization(monkeypatch: pytest.MonkeyPatch) -> None:
    guest = StepGuest(local_files=False)
    guest.respond(_apt_install(*INIT_SYSTEM_PACKAGES), _failed())

    with pytest.raises(CommandFailed):
        packages._install_system_packages(guest.files, guest.log)

    assert guest.commands()[-2:] == [APT_UPDATE, _apt_install(*INIT_SYSTEM_PACKAGES)]


def test_a_failed_mise_source_or_update_warns_and_the_install_still_runs() -> None:
    guest = StepGuest(local_files=False)
    guest.respond(
        Command(("curl", "-fsSL", MISE_GPG_KEY_URL, "-o", MISE_GPG_KEY_PATH), elevated=True, deadline=30),
        _failed(),
    )
    guest.respond(APT_UPDATE, _failed())

    packages._install_system_packages(guest.files, guest.log)

    assert len(guest.log.warned) == 2
    assert guest.commands()[-1] == _apt_install(*INIT_SYSTEM_PACKAGES)


# -- Install commands -------------------------------------------------------------------


def _install(shell: str, command: str) -> Command:
    return Command((shell, "-lc", command), deadline=120)


def _tool(**tests: str) -> dict[str, UserInstallCommandEntry]:
    return {"my-tool": UserInstallCommandEntry(name="my-tool", command="run-install", path=["~/.my-tool/bin"], **tests)}


def _run(guest: StepGuest, entries: dict, names: list[str] | None = None) -> None:  # type: ignore[type-arg]
    _run_install_commands(guest.files, names or list(entries), entries, "zsh", "/home/agentworks", guest.log)


def test_an_install_command_runs_in_the_login_shell() -> None:
    guest = StepGuest(local_files=False)

    _run(guest, _make_entries().user_install_commands)

    assert guest.commands() == [_install("zsh", "curl -fsSL https://example.com/install.sh | bash")]
    assert all(not request.env for request in guest.commands())  # hermetic: nothing injected


def test_an_undeclared_install_command_warns_and_runs_nothing() -> None:
    guest = StepGuest(local_files=False)

    _run(guest, _make_entries().user_install_commands, ["nonexistent"])

    assert len(guest.log.warned) == 1
    assert guest.access.requests() == []


def test_no_install_commands_means_no_request() -> None:
    guest = StepGuest(local_files=False)

    _run(guest, {}, [])

    assert guest.access.requests() == []


def test_an_install_command_whose_tests_all_pass_is_skipped() -> None:
    guest = StepGuest(local_files=False)

    _run(guest, _tool(test_exec="my-tool", test_file="~/.my-tool/installed"))

    ((check,),) = [guest.commands()]
    assert isinstance(check, Script) and (check.shell, check.deadline, check.elevated) == (Shell.SH, 10, False)


def test_an_install_command_whose_test_fails_runs() -> None:
    guest = StepGuest(local_files=False)
    guest.access.answer = lambda call: Result(Completed(1)) if isinstance(call.request, Script) else None

    _run(guest, _tool(test_exec="my-tool"))

    assert guest.commands()[-1] == _install("zsh", "run-install")


def test_a_test_that_cannot_run_counts_as_not_installed() -> None:
    guest = StepGuest(local_files=False)
    guest.access.answer = lambda call: (
        ExecutionIncomplete("box", Result(Unknown(UnknownReason.DEADLINE)))
        if isinstance(call.request, Script)
        else None
    )

    _run(guest, _tool(test_dir="~/.my-tool"))

    assert guest.commands()[-1] == _install("zsh", "run-install")
    assert guest.log.warned == []


def test_an_install_command_with_no_test_always_runs() -> None:
    guest = StepGuest(local_files=False)

    _run(guest, _tool(test_exec="", test_file="", test_dir=""))

    assert guest.commands() == [_install("zsh", "run-install")]


def test_a_failed_install_command_warns_and_the_next_runs() -> None:
    guest = StepGuest(local_files=False)
    entries = {
        "first": UserInstallCommandEntry(name="first", command="one", path=["/opt/one"]),
        "second": UserInstallCommandEntry(name="second", command="two", path=["/opt/two"]),
    }
    guest.respond(_install("zsh", "one"), _failed())

    _run(guest, entries)

    assert len(guest.log.warned) == 1
    assert guest.commands() == [_install("zsh", "one"), _install("zsh", "two")]


def test_the_declared_paths_are_every_declared_command_s_in_order() -> None:
    entries = {
        "first": UserInstallCommandEntry(name="first", command="one", path=["/opt/one", "/opt/shared"]),
        "second": UserInstallCommandEntry(name="second", command="two", path=["/opt/two"]),
    }

    assert install_command_paths(["second", "missing", "first"], entries) == ["/opt/two", "/opt/one", "/opt/shared"]
    assert install_command_paths([], entries) == []


@pytest.mark.parametrize("entry_type", [SystemInstallCommandEntry, UserInstallCommandEntry])
def test_a_failed_install_command_is_reported_so_its_paths_leave_the_profile(
    entry_type: type[SystemInstallCommandEntry] | type[UserInstallCommandEntry],
) -> None:
    guest = StepGuest(local_files=False)
    entries = {
        name: entry_type(
            name=name,
            command=f"install-{name}",
            path=[f"~/.{name}/bin"],
            test_file="~/.present" if name == "present" else None,
        )
        for name in ["first", "broken", "present", "last"]
    }
    guest.respond(_install("zsh", "install-broken"), _failed())
    guest.access.answer = lambda call: Result(Completed(0)) if isinstance(call.request, Script) else None

    failed = _run_install_commands(guest.files, list(entries), entries, "zsh", "/home/agentworks", guest.log)

    assert failed == ["broken"]
    kept = [name for name in entries if name not in failed]
    assert install_command_paths(kept, entries) == ["~/.first/bin", "~/.present/bin", "~/.last/bin"]
    commands = guest.commands()
    assert [command for command in commands if isinstance(command, Command)] == [
        _install("zsh", "install-first"),
        _install("zsh", "install-broken"),
        _install("zsh", "install-last"),
    ]
    assert isinstance(commands[2], Script) and "test -f /home/agentworks/.present" in commands[2].source
    assert len(guest.log.warned) == 1


# -- The completion test itself, run by /bin/sh ------------------------------------------


@pytest.mark.skipif(not Path("/bin/sh").exists(), reason="runs the completion test under /bin/sh")
@pytest.mark.parametrize(
    ("present", "expected"),
    [((), 1), (("file",), 1), (("dir",), 1), (("file", "dir"), 0)],
    ids=["neither", "file-only", "dir-only", "both"],
)
def test_the_completion_test_passes_only_when_every_declared_test_passes(
    tmp_path: Path, present: tuple[str, ...], expected: int
) -> None:
    import subprocess

    (tmp_path / "tool").mkdir() if "dir" in present else None
    (tmp_path / "installed").write_text("") if "file" in present else None
    entry = UserInstallCommandEntry(name="t", command="x", test_exec="sh", test_file="~/installed", test_dir="~/tool")

    command = packages._build_test_command(entry, "sh", str(tmp_path))

    assert command is not None
    assert subprocess.run(["/bin/sh", "-c", command], capture_output=True, check=False).returncode == expected
