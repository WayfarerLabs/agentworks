"""Focused mise configuration and install-flow coverage, over a recording access (``tests/phase_b_support.py``)."""

from __future__ import annotations

import stat
import tomllib
from pathlib import Path

import pytest

from agentworks.agents.initializer import _run_agent_mise_setup
from agentworks.agents.templates import ResolvedAgentTemplate
from agentworks.config.validation import check_mise_settings
from agentworks.execution.errors import CommandFailed, ExecutionIncomplete
from agentworks.execution.requests import Command
from agentworks.execution.results import Completed, Result, Unknown, UnknownReason
from agentworks.vms.initializer.mise import _run_mise_install, _write_mise_config, mise_run
from tests.phase_b_support import StepGuest, requires_file_program

HOME = "/home/a"
LOCKFILE_PROBE = Command(("test", "-f", f"{HOME}/.config/mise/mise.lock"), deadline=60)
LOCKED = mise_run("bash", "mise install -y --locked", 300)
UNLOCKED = mise_run("bash", "mise install -y", 300)
PRUNE = mise_run("bash", "mise prune -y", 60)


def _failed(stderr: bytes) -> CommandFailed:
    return CommandFailed("box", 1, Result(Completed(1), stderr=stderr), streams_logged=True)


@requires_file_program
def test_write_mise_config_renders_settings_and_tools_at_0644(tmp_path: Path) -> None:
    guest = StepGuest()

    _write_mise_config(guest.files, ["terraform@1.14.5", "aqua:npryce/adr-tools@3.0.0"], "7d", str(tmp_path), guest.log)

    config = tmp_path / ".config" / "mise" / "config.toml"
    assert tomllib.loads(config.read_text()) == {
        "settings": {"install_before": "7d"},
        "tools": {"terraform": "1.14.5", "aqua:npryce/adr-tools": "3.0.0"},
    }
    assert stat.S_IMODE(config.stat().st_mode) == 0o644


@requires_file_program
def test_a_linked_mise_config_is_not_written_through_and_warns(tmp_path: Path) -> None:
    dotfiles = tmp_path / "dotfiles" / "config.toml"
    dotfiles.parent.mkdir()
    dotfiles.write_text("[tools]\n")
    (tmp_path / ".config" / "mise").mkdir(parents=True)
    (tmp_path / ".config" / "mise" / "config.toml").symlink_to(dotfiles)
    guest = StepGuest()

    _write_mise_config(guest.files, ["jq@1"], "7d", str(tmp_path), guest.log)

    assert dotfiles.read_text() == "[tools]\n"
    assert len(guest.log.warned) == 1


def test_mise_validation_allows_scoped_backend_tool_name() -> None:
    check_mise_settings(["npm:@scope/tool@1.2.3"], None, "7d")


def test_mise_validation_rejects_control_characters() -> None:
    # ValueError rather than ConfigError: this is what a model validator
    # raises, and the error bridge frames it with the owning resource.
    with pytest.raises(ValueError, match="name@version"):
        check_mise_settings(["tool\x00@1"], None, "7d")


def _guest(*, lockfile: bool | BaseException) -> StepGuest:
    guest = StepGuest(local_files=False)
    found = Result(Completed(0 if lockfile is True else 1))
    probe: Result | BaseException = lockfile if isinstance(lockfile, BaseException) else found
    guest.respond(LOCKFILE_PROBE, probe)
    return guest


@pytest.mark.parametrize("lockfile", [False, ExecutionIncomplete("box", Result(Unknown(UnknownReason.CONNECTION)))])
def test_without_a_lockfile_or_a_probe_that_cannot_run_mise_installs_unlocked_then_prunes(
    lockfile: bool | BaseException,
) -> None:
    guest = _guest(lockfile=lockfile)

    _run_mise_install(guest.files, "bash", HOME, False, guest.log)

    assert guest.commands() == [LOCKFILE_PROBE, UNLOCKED, PRUNE]


def test_a_locked_failure_does_not_fall_back_by_default_and_names_the_failed_tools(captured_output) -> None:  # noqa: ANN001
    guest = _guest(lockfile=True)
    guest.respond(LOCKED, _failed(b"mise ERROR Failed to install tool@1: absent\n"))

    _run_mise_install(guest.files, "bash", HOME, False, guest.log)

    assert guest.commands() == [LOCKFILE_PROBE, LOCKED]
    assert any("tool@1" in warning for warning in captured_output.warnings)


def test_a_locked_failure_falls_back_when_allowed_and_a_failed_prune_is_ignored() -> None:
    guest = _guest(lockfile=True)
    guest.respond(LOCKED, _failed(b"mise ERROR Failed to install tool@1: absent\n"))
    guest.respond(PRUNE, _failed(b""))

    _run_mise_install(guest.files, "bash", HOME, True, guest.log)

    assert guest.commands() == [LOCKFILE_PROBE, LOCKED, UNLOCKED, PRUNE]


@requires_file_program
def test_agent_mise_setup_writes_its_config_and_installs_as_the_agent(tmp_path: Path) -> None:
    guest = StepGuest()
    guest.respond(Command(("test", "-f", f"{tmp_path}/.config/mise/mise.lock"), deadline=60), Result(Completed(1)))
    template = ResolvedAgentTemplate(name="default", mise_packages=["terraform@1.14.5"])

    _run_agent_mise_setup(agent_files=guest.files, logger=guest.log, agent_tmpl=template, home=str(tmp_path))

    assert "terraform" in tomllib.loads((tmp_path / ".config" / "mise" / "config.toml").read_text())["tools"]
    assert mise_run("bash", "mise install -y", 300) in guest.commands()
