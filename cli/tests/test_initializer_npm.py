"""Behavior of the shared, native npm installation runner."""

from __future__ import annotations

import shlex
from unittest.mock import MagicMock

import pytest

from agentworks.ssh import SSHError, SSHResult
from agentworks.vms.initializer.npm import run_npm_install


def _target(version: str = "11.16.0") -> MagicMock:
    target = MagicMock()
    target.run.return_value = SSHResult(0, version + "\n", "")
    return target


def _argv(command: str) -> list[str]:
    shell_argv = shlex.split(command)
    assert shell_argv[:2] == ["bash", "-lc"]
    return shlex.split(shell_argv[2])


def test_empty_package_selection_does_no_remote_work() -> None:
    target = _target()
    assert run_npm_install(target, [], "bash", "/home/user", 7) == []
    target.run.assert_not_called()


@pytest.mark.parametrize("version", ["10.9.0", "11.9.9", "11.10.0-rc.1", "unknown", "", "11.10"])
def test_unsupported_or_unknown_npm_never_installs(version: str) -> None:
    target = _target(version)
    assert run_npm_install(target, ["tool"], "bash", "/home/user", 7) == ["/home/user/.local/bin"]
    assert target.run.call_count == 1
    assert _argv(target.run.call_args.args[0]) == ["npm", "--version"]


def test_missing_npm_never_installs() -> None:
    target = _target()
    target.run.side_effect = SSHError("npm: command not found")
    run_npm_install(target, ["tool"], "bash", "/home/user", 7)
    assert target.run.call_count == 1


@pytest.mark.parametrize("version", ["11.10.0", "11.16.0", "11.17.0", "12.2.2"])
def test_install_uses_user_prefix_age_policy_and_strict_engines(version: str) -> None:
    target = _target(version)
    logger = MagicMock()
    packages = ["@scope/tool@1.2.3", "other@>=1 <2 || ^3"]
    paths = run_npm_install(target, packages, "bash", "/home/a user's home", 14, logger)

    assert paths == ["/home/a user's home/.local/bin"]
    assert target.run.call_count == 3
    for call, package in zip(target.run.call_args_list[1:], packages, strict=True):
        argv = _argv(call.args[0])
        assert argv[:5] == ["npm", "install", "--global", "--prefix", "/home/a user's home/.local"]
        assert "--min-release-age=14" in argv
        assert "--engine-strict" in argv
        assert "--force=false" in argv
        assert argv[-2:] == ["--", package]
        assert call.kwargs == {"timeout": 300}
        assert ("--min-release-age-exclude=" in argv) == (version in {"11.17.0", "12.2.2"})


def test_zero_age_is_passed_explicitly() -> None:
    target = _target()
    run_npm_install(target, ["tool"], "bash", "/home/user", 0)
    assert "--min-release-age=0" in _argv(target.run.call_args.args[0])


def test_install_failure_keeps_existing_path_and_continues_without_unfiltered_retry() -> None:
    target = _target()
    target.run.side_effect = [SSHResult(0, "11.17.0\n", ""), SSHError("ETARGET"), SSHResult(0, "", "")]
    logger = MagicMock()

    paths = run_npm_install(target, ["too-new@1.2.3", "other@1.2.3"], "bash", "/home/user", 7, logger)

    assert paths == ["/home/user/.local/bin"]
    assert target.run.call_count == 3
    logger.warning.assert_called_once()
    for call in target.run.call_args_list[1:]:
        assert "--min-release-age=7" in _argv(call.args[0])


def test_declared_shell_is_used() -> None:
    target = _target()
    run_npm_install(target, ["tool"], "zsh", "/home/user", 7)
    assert all(shlex.split(call.args[0])[:2] == ["zsh", "-lc"] for call in target.run.call_args_list)
