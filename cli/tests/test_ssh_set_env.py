"""Tests for SSH SetEnv argument construction.

The env-and-secrets SDD's Phase 3 SetEnv pivot routes env injection through
the SSH protocol's SetEnv mechanism (accepted by the remote sshd's
`AcceptEnv *` directive). These tests pin the client-side construction.

All pairs are coalesced into ONE ``-o SetEnv=K1="v1" K2="v2" ...`` argument.
Repeating ``-o SetEnv=`` per pair is wrong: ssh_config(5) says "for each
parameter, the first obtained value will be used" -- so only the first pair
would reach the wire and every subsequent pair would be silently dropped.
"""

from __future__ import annotations

import subprocess
from typing import Any
from unittest.mock import patch

import pytest

from agentworks.ssh import (
    ExecTarget,
    SSHTarget,
    _set_env_args,
    _ssh_base_args,
    interactive,
    run,
)


def _target() -> SSHTarget:
    return SSHTarget(host="vm.tailnet", user="agentworks")


def _set_env_value(args: list[str]) -> str | None:
    """Return the value portion of the single SetEnv= arg, or None if absent."""
    for i, a in enumerate(args):
        if a == "-o" and i + 1 < len(args) and args[i + 1].startswith("SetEnv="):
            return args[i + 1][len("SetEnv=") :]
    return None


def test_base_args_no_env_omits_set_env_flag() -> None:
    args = _ssh_base_args(_target())
    assert _set_env_value(args) is None
    assert args[-1] == "agentworks@vm.tailnet"


def test_base_args_empty_env_dict_omits_set_env_flag() -> None:
    """``env={}`` is treated the same as ``env=None``: no SetEnv arg."""
    args = _ssh_base_args(_target(), env={})
    assert _set_env_value(args) is None


def test_set_env_args_coalesces_multiple_pairs_into_one_o_flag() -> None:
    """Regression: every pair must land in ONE ``-o SetEnv=...`` arg.

    ssh_config(5): "for each parameter, the first obtained value will be
    used." If we emit ``-o SetEnv=`` per pair, OpenSSH keeps the first and
    silently drops the rest -- exactly the bug this test exists to prevent.
    """
    args = _set_env_args({"A": "1", "B": "2", "C": "3"})
    set_env_flags = [i for i, a in enumerate(args) if a == "-o" and args[i + 1].startswith("SetEnv=")]
    assert len(set_env_flags) == 1, f"expected exactly one -o SetEnv= arg, got {args}"
    value = args[set_env_flags[0] + 1][len("SetEnv=") :]
    # Pair order matches dict insertion order.
    assert value == 'A="1" B="2" C="3"'


def test_set_env_args_quotes_value_with_spaces() -> None:
    """Whitespace in a value would break OpenSSH's SetEnv parser unless
    wrapped in double quotes."""
    args = _set_env_args({"GREET": "hello world", "X": "y"})
    assert _set_env_value(args) == 'GREET="hello world" X="y"'


def test_set_env_args_escapes_embedded_double_quote() -> None:
    """``"`` inside a value must be escaped as ``\\"`` so OpenSSH's
    quoting parser doesn't see it as the end of the quoted segment."""
    args = _set_env_args({"K": 'say "hi"'})
    assert _set_env_value(args) == 'K="say \\"hi\\""'


def test_set_env_args_escapes_embedded_backslash() -> None:
    r"""``\`` inside a value must be doubled to ``\\`` because OpenSSH's
    quoting parser treats ``\`` as an escape character."""
    args = _set_env_args({"K": "back\\slash"})
    assert _set_env_value(args) == 'K="back\\\\slash"'


def test_set_env_args_handles_empty_value() -> None:
    """Empty values must still produce ``K=""`` (omitting the pair would
    drop the var entirely rather than setting it to empty as intended)."""
    args = _set_env_args({"EMPTY": "", "FULL": "value"})
    assert _set_env_value(args) == 'EMPTY="" FULL="value"'


def test_base_args_set_env_precedes_host() -> None:
    """The SetEnv -o flag must appear before the user@host positional;
    otherwise OpenSSH parses it as part of the remote command."""
    args = _ssh_base_args(_target(), env={"K": "v"})
    host_index = args.index("agentworks@vm.tailnet")
    o_index = next(i for i, a in enumerate(args) if a == "-o" and args[i + 1].startswith("SetEnv="))
    assert o_index < host_index


def test_run_threads_env_to_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    """ssh.run with env=... ends up with a SetEnv= arg on the subprocess argv."""
    captured: dict[str, Any] = {}

    class _CompletedStub:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_subprocess_run(args: list[str], **kwargs: object) -> _CompletedStub:  # noqa: ARG001
        captured["args"] = args
        return _CompletedStub()

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)
    run(_target(), "true", env={"AGENTWORKS_VM": "vm-1", "EDITOR": "vim"})
    assert _set_env_value(captured["args"]) == 'AGENTWORKS_VM="vm-1" EDITOR="vim"'


def test_interactive_threads_env_to_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    """ssh.interactive with env=... ends up with a SetEnv= arg on the subprocess argv."""
    captured: dict[str, Any] = {}

    def fake_call(args: list[str]) -> int:
        captured["args"] = args
        return 0

    monkeypatch.setattr(subprocess, "call", fake_call)
    interactive(_target(), "tmux attach", env={"AGENTWORKS_SESSION": "s1", "EDITOR": "vim"})
    assert _set_env_value(captured["args"]) == 'AGENTWORKS_SESSION="s1" EDITOR="vim"'


def test_exec_target_run_threads_env_to_ssh_run() -> None:
    """ExecTarget.run(env=...) passes env down to ssh.run, which materializes
    a single SetEnv= arg with all pairs."""
    target = ExecTarget(ssh=_target())
    captured: dict[str, Any] = {}

    class _CompletedStub:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_subprocess_run(args: list[str], **kwargs: object) -> _CompletedStub:  # noqa: ARG001
        captured["argv"] = args
        return _CompletedStub()

    with patch.object(subprocess, "run", fake_subprocess_run):
        target.run("true", env={"AGENTWORKS_AGENT": "claude", "K": "v"})

    assert _set_env_value(captured["argv"]) == 'AGENTWORKS_AGENT="claude" K="v"'


def test_exec_target_call_streaming_threads_env_to_subprocess() -> None:
    """ExecTarget.call_streaming(env=...) emits a single SetEnv= arg.

    This path (used by ``agw vm exec`` / ``agw agent exec``) is structurally
    identical to ``run`` / ``interactive`` and must also pin the
    one-arg-per-call invariant.
    """
    target = ExecTarget(ssh=_target())
    captured: dict[str, Any] = {}

    def fake_call(args: list[str]) -> int:
        captured["args"] = args
        return 0

    with patch("subprocess.call", fake_call):
        target.call_streaming("env", env={"A": "1", "B": "2", "C": "3"})

    assert _set_env_value(captured["args"]) == 'A="1" B="2" C="3"'
