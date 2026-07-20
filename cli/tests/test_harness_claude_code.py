"""The ``claude-code`` harness: config vocabulary, the resume-vs-launch
detection (both directions), the flag mapping and ``extra_args``
passthrough, the visible decision, the stored-id persistence, and that
readiness probes ``claude``.

Detection is exercised with NO real ``claude`` binary by stubbing the one
transport call the op makes (the ``<sid>.jsonl`` find probe), keyed on the
stored session id (``claude-code-lld.md`` "Test double").
"""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

import pytest

from agentworks.capabilities.base import OperationScope, RunContext, ScopeLevel
from agentworks.capabilities.harness import ClaudeCodeHarness
from agentworks.errors import ConfigError, StateError
from tests.conftest import _FakeResult, _FakeTarget

if TYPE_CHECKING:
    from collections.abc import Mapping

_SID = "939b1597-7c61-5ace-80f4-14617b7b4257"  # a fixed stored uuid


def _harness(
    config: Mapping[str, object] | None = None,
    *,
    session_name: str = "s1",
    state: dict[str, object] | None = None,
    admin: bool = True,
) -> ClaudeCodeHarness:
    return ClaudeCodeHarness(
        "claude",
        config or {},
        session_name=session_name,
        vm_name="box",
        workspace_name="ws1",
        target=None,
        admin=admin,
        state={"session_id": _SID} if state is None else state,
    )


def _op_ctx(target: _FakeTarget) -> RunContext:
    """A context carrying only the launch target (admin mode); the op
    reads ``ctx.admin_target()`` and touches no scope."""
    return RunContext(admin_target=target)


def _session_scope() -> OperationScope:
    return OperationScope(
        level=ScopeLevel.SESSION,
        vm="box",
        workspace="ws1",
        session="s1",
        agent=None,
        admin=True,
    )


# -- config vocabulary -------------------------------------------------------


def test_validate_accepts_the_three_fields_and_implies_no_reference() -> None:
    refs = ClaudeCodeHarness.validate_config(
        "session-template/claude",
        {"permission_mode": "acceptEdits", "model": "opus", "extra_args": ["--foo"]},
    )
    assert refs == ()


def test_validate_accepts_empty_config() -> None:
    assert ClaudeCodeHarness.validate_config("session-template/claude", {}) == ()


def test_validate_rejects_unknown_field() -> None:
    with pytest.raises(ConfigError, match="unknown claude-code harness field"):
        ClaudeCodeHarness.validate_config(
            "session-template/claude", {"permision_mode": "typo"}
        )


def test_validate_rejects_non_string_model() -> None:
    with pytest.raises(ConfigError, match="model must be a string"):
        ClaudeCodeHarness.validate_config(
            "session-template/claude", {"model": 3}
        )


def test_validate_rejects_non_list_extra_args() -> None:
    with pytest.raises(ConfigError, match="extra_args must be a list of strings"):
        ClaudeCodeHarness.validate_config(
            "session-template/claude", {"extra_args": "just-a-string"}
        )


def test_construct_revalidates_config() -> None:
    with pytest.raises(ConfigError, match="unknown claude-code harness field"):
        _harness({"nope": 1})


# -- detection: present -> resume, absent -> launch fresh --------------------


def test_present_transcript_resumes() -> None:
    target = _FakeTarget({f"{_SID}.jsonl": _FakeResult(0)})  # found
    command = _harness().start(_op_ctx(target))
    assert f"--resume {_SID}" in command
    assert "--session-id" not in command
    assert "resuming session s1" in command


def test_absent_transcript_launches_fresh() -> None:
    target = _FakeTarget({f"{_SID}.jsonl": _FakeResult(1)})  # not found
    command = _harness().start(_op_ctx(target))
    assert f"--session-id {_SID}" in command
    assert "--resume" not in command
    assert "starting new session s1" in command


def test_launch_note_reports_resume() -> None:
    target = _FakeTarget({f"{_SID}.jsonl": _FakeResult(0)})  # found
    harness = _harness()
    assert harness.launch_note() is None  # nothing decided before the op
    harness.start(_op_ctx(target))
    assert harness.launch_note() == "Existing session found. Resuming..."


def test_launch_note_reports_fresh_start() -> None:
    target = _FakeTarget({f"{_SID}.jsonl": _FakeResult(1)})  # not found
    harness = _harness()
    harness.start(_op_ctx(target))
    assert harness.launch_note() == "No existing session. Starting a new one..."


def test_start_and_restart_are_symmetric() -> None:
    target = _FakeTarget({f"{_SID}.jsonl": _FakeResult(0)})
    harness = _harness()
    assert harness.start(_op_ctx(target)) == harness.restart(_op_ctx(target))


def test_probe_is_slug_independent_and_finds_by_stored_id() -> None:
    target = _FakeTarget({f"{_SID}.jsonl": _FakeResult(0)})
    _harness().start(_op_ctx(target))
    (probe_cmd,) = target.commands
    assert f"{_SID}.jsonl" in probe_cmd
    assert "find" in probe_cmd
    # Rooted at the CLI's config dir with its documented override.
    assert "CLAUDE_CONFIG_DIR" in probe_cmd


def test_probe_that_could_not_execute_raises_rather_than_guessing() -> None:
    """A non-{0,1} exit (an SSH failure's 255, a shell that could not
    start) means the probe never ran. Guessing "fresh" would launch
    ``--session-id`` over a reserved id and the pane would fail; the op
    raises a typed error naming the target instead."""
    target = _FakeTarget({f"{_SID}.jsonl": _FakeResult(255)})
    with pytest.raises(StateError, match="could not probe") as exc:
        _harness().start(_op_ctx(target))
    assert "exit 255" in str(exc.value)
    assert exc.value.entity_name == "s1"


# -- the stored session id ---------------------------------------------------


def test_first_start_mints_and_records_the_session_id() -> None:
    state: dict[str, object] = {}
    harness = _harness(state=state)
    target = _FakeTarget()  # empty state means no id yet; find returns default ok
    command = harness.start(_op_ctx(target))

    minted = state["session_id"]
    assert isinstance(minted, str) and len(minted) == 36  # a uuid
    assert harness.state == {"session_id": minted}  # persisted via the property
    assert minted in command


def test_restart_reads_the_stored_id_back_verbatim() -> None:
    """The round-trip the manager relies on: an id minted on create (in
    the state blob) is used verbatim on a later restart, never re-minted."""
    target = _FakeTarget({f"{_SID}.jsonl": _FakeResult(0)})
    harness = _harness(state={"session_id": _SID})
    command = harness.restart(_op_ctx(target))
    assert f"--resume {_SID}" in command
    assert harness.state == {"session_id": _SID}  # unchanged


# -- the managed flags and extra_args ----------------------------------------


def test_permission_mode_and_model_map_to_their_flags() -> None:
    target = _FakeTarget({f"{_SID}.jsonl": _FakeResult(1)})
    command = _harness(
        {"permission_mode": "acceptEdits", "model": "sonnet"}
    ).start(_op_ctx(target))
    assert "--permission-mode acceptEdits" in command
    assert "--model sonnet" in command


def test_extra_args_appended_verbatim_last_and_quoted() -> None:
    target = _FakeTarget({f"{_SID}.jsonl": _FakeResult(1)})
    command = _harness(
        {"model": "opus", "extra_args": ["--foo", "bar baz"]}
    ).start(_op_ctx(target))
    # One argv token stays one token: "bar baz" is quoted, not re-split.
    assert shlex.quote("bar baz") in command
    # Appended last: after the managed --model flag.
    assert command.index("--model") < command.index("--foo")


def test_extra_args_with_shell_metacharacters_cannot_inject() -> None:
    """``extra_args`` is operator-supplied and NOT name-validated (unlike
    ``session_name``), so an adversarial value with quotes/metacharacters
    must be ``shlex.quote``d into one inert argv token, never shell-active."""
    payload = "a'; touch /tmp/pwned #"
    target = _FakeTarget({f"{_SID}.jsonl": _FakeResult(1)})
    command = _harness(
        {"extra_args": ["--append-system-prompt", payload]}
    ).start(_op_ctx(target))

    # The command is `sh -c '<inner>'`; the payload is nested-quoted (once
    # into the argv, once into the sh -c wrapper). Peeling both quoting
    # layers back with shlex must yield the payload as exactly ONE inert
    # token, never a `touch` command the outer shell would run.
    outer = shlex.split(command)
    assert outer[:2] == ["sh", "-c"]
    inner_tokens = shlex.split(outer[2])
    assert payload in inner_tokens
    assert "touch" not in inner_tokens  # not a standalone command word


def test_name_is_set_on_both_branches_as_the_display_label() -> None:
    present = _FakeTarget({f"{_SID}.jsonl": _FakeResult(0)})
    absent = _FakeTarget({f"{_SID}.jsonl": _FakeResult(1)})
    assert "--name s1" in _harness().start(_op_ctx(present))
    assert "--name s1" in _harness().start(_op_ctx(absent))


# -- the returned pane string shape ------------------------------------------


def test_returned_string_is_a_single_sh_c_that_echoes_then_execs() -> None:
    """A single ``sh -c`` (so it survives the pane's ``exec`` wrapping),
    echoing the visible decision before exec-ing claude."""
    target = _FakeTarget({f"{_SID}.jsonl": _FakeResult(1)})
    command = _harness().start(_op_ctx(target))
    assert command.startswith("sh -c ")
    assert "echo " in command
    assert "exec claude" in command


# -- readiness probes claude -------------------------------------------------


def test_readiness_probes_claude() -> None:
    harness = _harness()
    target = _FakeTarget()  # command -v claude -> default ok
    harness.preflight(RunContext(operation_scope=_session_scope(), admin_target=target))
    assert any("command -v claude" in cmd for cmd in target.commands)


def test_readiness_missing_claude_is_a_typed_error() -> None:
    harness = _harness()
    target = _FakeTarget({"command -v claude": _FakeResult(1)})
    ctx = RunContext(operation_scope=_session_scope(), admin_target=target)
    with pytest.raises(StateError, match="'claude-code' harness.*requires 'claude'"):
        harness.preflight(ctx)
