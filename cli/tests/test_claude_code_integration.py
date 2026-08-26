"""The ``claude-code`` harness integration: config vocabulary, the resume-vs-launch
detection (both directions), the flag mapping and ``extra_args``
passthrough, the visible decision, the stored-id persistence, the legacy
pre-namespacing state hoist, and that readiness probes ``claude``.

Detection is exercised with NO real ``claude`` binary by stubbing the one
transport call the op makes (the ``<sid>.jsonl`` find probe), keyed on the
stored session id (``claude-code-lld.md`` "Test double").
"""

from __future__ import annotations

import json
import shlex
from typing import TYPE_CHECKING

import pytest

from agentworks.capabilities.base import OperationScope, RunContext, ScopeLevel
from agentworks.capabilities.config import capability_config_references, validate_capability_config
from agentworks.errors import ConfigError, StateError
from agentworks.plugins.claude.harness_integration import ClaudeCodeIntegration
from agentworks.schema import RefOwner
from tests.conftest import _FakeResult, _FakeTarget

if TYPE_CHECKING:
    from collections.abc import Mapping

_SID = "939b1597-7c61-5ace-80f4-14617b7b4257"  # a fixed stored uuid


def _harness_integration(
    config: Mapping[str, object] | None = None,
    *,
    session_name: str = "s1",
    state: dict[str, object] | None = None,
    admin: bool = True,
) -> ClaudeCodeIntegration:
    return ClaudeCodeIntegration(
        "claude",
        config or {},
        session_name=session_name,
        vm_name="box",
        workspace_name="ws1",
        workspace_path="/srv/ws1",
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


def _refs(blob: dict[str, object]) -> tuple[object, ...]:
    return capability_config_references(
        kind="harness-integration",
        config={"name": "claude-code", **blob},
        owner=RefOwner(kind="session-template", name="claude"),
    )


def test_it_implies_no_reference() -> None:
    """``claude-code`` names no Resource in its config, and extraction is
    total: it returns ``()`` for the known fields and for a malformed blob
    alike."""
    assert _refs({"model": "x"}) == ()
    assert _refs({"model": 3, "nonsense": "typo"}) == ()


def _validate(blob: dict[str, object]) -> None:
    """Validation is the CORE's now: it reads the model this integration
    declares, and no integration code runs."""
    validate_capability_config(
        kind="harness-integration",
        config={"name": "claude-code", **blob},
        owner=RefOwner(kind="session-template", name="claude"),
    )


def test_validation_accepts_the_optional_fields_and_empty_config() -> None:
    _validate(
        {
            "permission_mode": "acceptEdits",
            "model": "opus",
            "reasoning_effort": "high",
            "goal": "Finish the migration",
            "initial_prompt": "Start with the failing tests",
            "agent": "reviewer",
            "append_system_prompt": "Keep changes focused",
            "remote_control": True,
            "vim_mode": True,
            "terminal_bell": True,
            "extra_args": ["--foo"],
        }
    )
    _validate({})


def test_validation_rejects_unknown_field() -> None:
    with pytest.raises(ConfigError, match="permision_mode: unknown field; expected one of:"):
        _validate({"permision_mode": "typo"})


@pytest.mark.parametrize(
    "field",
    ["model", "reasoning_effort", "goal", "initial_prompt", "agent", "append_system_prompt"],
)
def test_validation_rejects_non_string_flag_values(field: str) -> None:
    with pytest.raises(ConfigError):
        _validate({field: 3})


def test_validation_rejects_non_list_extra_args() -> None:
    with pytest.raises(ConfigError, match="extra_args: must be a list"):
        _validate({"extra_args": "just-a-string"})


@pytest.mark.parametrize("field", ["remote_control", "vim_mode", "terminal_bell"])
def test_validation_rejects_non_boolean_preferences(field: str) -> None:
    with pytest.raises(ConfigError):
        _validate({field: "yes"})


def test_construct_revalidates_config() -> None:
    with pytest.raises(ConfigError, match="nope: unknown field"):
        _harness_integration({"nope": 1})


# -- detection: present -> resume, absent -> launch fresh --------------------


def test_present_transcript_resumes() -> None:
    target = _FakeTarget({f"{_SID}.jsonl": _FakeResult(0)})  # found
    command = _harness_integration().start(_op_ctx(target))
    assert f"--resume {_SID}" in command
    assert "--session-id" not in command
    assert "resuming session s1" in command


def test_absent_transcript_launches_fresh() -> None:
    target = _FakeTarget({f"{_SID}.jsonl": _FakeResult(1)})  # not found
    command = _harness_integration().start(_op_ctx(target))
    assert f"--session-id {_SID}" in command
    assert "--resume" not in command
    assert "starting new session s1" in command


def test_launch_note_reports_resume() -> None:
    target = _FakeTarget({f"{_SID}.jsonl": _FakeResult(0)})  # found
    harness_integration = _harness_integration()
    assert harness_integration.launch_note() is None  # nothing decided before the op
    harness_integration.start(_op_ctx(target))
    assert harness_integration.launch_note() == "Existing Claude Code session found. Resuming..."


def test_launch_note_reports_fresh_start() -> None:
    target = _FakeTarget({f"{_SID}.jsonl": _FakeResult(1)})  # not found
    harness_integration = _harness_integration()
    harness_integration.start(_op_ctx(target))
    assert harness_integration.launch_note() == "No existing Claude Code session. Starting a new one..."


def test_start_and_restart_are_symmetric() -> None:
    target = _FakeTarget({f"{_SID}.jsonl": _FakeResult(0)})
    harness_integration = _harness_integration()
    assert harness_integration.start(_op_ctx(target)) == harness_integration.resume(_op_ctx(target))


def test_probe_is_slug_independent_and_finds_by_stored_id() -> None:
    target = _FakeTarget({f"{_SID}.jsonl": _FakeResult(0)})
    _harness_integration().start(_op_ctx(target))
    (probe_cmd,) = target.commands
    assert f"{_SID}.jsonl" in probe_cmd
    assert "find" in probe_cmd
    # Rooted at the CLI's config dir with its documented override.
    assert "CLAUDE_CONFIG_DIR" in probe_cmd


def test_probe_keeps_find_failure_distinct_from_a_clean_no_match() -> None:
    """The inner command's structure: a missing projects dir short-circuits
    to the clean no-match exit (1, fresh), while a find that FAILED without
    printing a match exits 6, which the exit-code fork raises on rather
    than folding into "no transcript". Pinned here because the fake target
    answers by exit code alone; the structure is what guarantees those
    codes mean what the fork thinks they mean."""
    target = _FakeTarget({f"{_SID}.jsonl": _FakeResult(0)})
    _harness_integration().start(_op_ctx(target))
    (probe_cmd,) = target.commands
    assert "[ -d " in probe_cmd  # dir-missing is a clean no-match, not a find failure
    assert "exit 6" in probe_cmd  # find failure stays distinguishable
    # And the fork raises on that distinct code.
    failing = _FakeTarget({f"{_SID}.jsonl": _FakeResult(6)})
    with pytest.raises(StateError, match="could not probe"):
        _harness_integration().start(_op_ctx(failing))


def test_probe_that_could_not_execute_raises_rather_than_guessing() -> None:
    """A non-{0,1} exit (an SSH failure's 255, a shell that could not
    start) means the probe never ran. Guessing "fresh" would launch
    ``--session-id`` over a reserved id and the pane would fail; the op
    raises a typed error naming the target instead."""
    target = _FakeTarget({f"{_SID}.jsonl": _FakeResult(255)})
    with pytest.raises(StateError, match="could not probe") as exc:
        _harness_integration().start(_op_ctx(target))
    assert "exit 255" in str(exc.value)
    assert exc.value.entity_name == "s1"


# -- the stored session id ---------------------------------------------------


def test_first_start_mints_and_records_the_session_id() -> None:
    state: dict[str, object] = {}
    harness_integration = _harness_integration(state=state)
    target = _FakeTarget()  # empty state means no id yet; find returns default ok
    command = harness_integration.start(_op_ctx(target))

    minted = state["session_id"]
    assert isinstance(minted, str) and len(minted) == 36  # a uuid
    assert harness_integration.state == {"session_id": minted}  # persisted via the property
    assert minted in command


def test_resume_reads_the_stored_id_back_verbatim() -> None:
    """The round-trip the manager relies on: an id minted on create (in
    the state blob) is used verbatim on a later restart, never re-minted."""
    target = _FakeTarget({f"{_SID}.jsonl": _FakeResult(0)})
    harness_integration = _harness_integration(state={"session_id": _SID})
    command = harness_integration.resume(_op_ctx(target))
    assert f"--resume {_SID}" in command
    assert harness_integration.state == {"session_id": _SID}  # unchanged


# -- the legacy state hoist (compatibility, pre-namespacing) -----------------
# Compatibility (pre-namespacing harness_state): DELETE this section on the
# next major release, together with the hoist itself.


def test_hoist_moves_a_legacy_top_level_id_into_the_namespace() -> None:
    """A pre-namespacing blob stores ``session_id`` at the top level; the
    hoist adopts it into the ``claude-code`` namespace and removes the
    flat key, so the session keeps its id (and its resumable history)."""
    blob: dict[str, object] = {"session_id": _SID}
    ClaudeCodeIntegration.hoist_legacy_state(blob)
    assert blob == {"claude-code": {"session_id": _SID}}


def test_hoist_is_idempotent() -> None:
    blob: dict[str, object] = {"session_id": _SID}
    ClaudeCodeIntegration.hoist_legacy_state(blob)
    ClaudeCodeIntegration.hoist_legacy_state(blob)
    assert blob == {"claude-code": {"session_id": _SID}}


def test_hoist_never_clobbers_an_already_namespaced_id() -> None:
    """A VALID namespaced id is the one recent ops used (forward-only
    history), so it wins; the legacy top-level key is dropped either
    way."""
    blob: dict[str, object] = {
        "session_id": "00000000-0000-4000-8000-000000000000",
        "claude-code": {"session_id": _SID},
    }
    ClaudeCodeIntegration.hoist_legacy_state(blob)
    assert blob == {"claude-code": {"session_id": _SID}}


def test_hoist_replaces_a_non_string_namespaced_id_with_the_legacy_one() -> None:
    """Hand-edited garbage in the namespaced slot does not get to discard
    a real legacy id: only a non-empty string namespaced value wins."""
    blob: dict[str, object] = {"session_id": _SID, "claude-code": {"session_id": 7}}
    ClaudeCodeIntegration.hoist_legacy_state(blob)
    assert blob == {"claude-code": {"session_id": _SID}}


def test_hoist_sweeps_an_empty_flat_id_without_adopting_it() -> None:
    """An empty flat string is garbage this harness integration never wrote: it is
    swept off the top level but not adopted into the namespace."""
    blob: dict[str, object] = {"session_id": ""}
    ClaudeCodeIntegration.hoist_legacy_state(blob)
    assert blob == {}


def test_hoist_leaves_a_non_string_top_level_value_alone() -> None:
    """A non-string top-level ``session_id`` is not this harness integration's legacy
    shape; the hoist does not guess at it."""
    blob: dict[str, object] = {"session_id": 7}
    ClaudeCodeIntegration.hoist_legacy_state(blob)
    assert blob == {"session_id": 7}


def test_base_hoist_is_a_no_op() -> None:
    """The base hook exists so the platform seam stays integration-agnostic;
    an integration that never wrote unnamespaced state leaves the blob as-is."""
    from agentworks.capabilities.harness_integration import ShellIntegration

    blob: dict[str, object] = {"session_id": _SID}
    ShellIntegration.hoist_legacy_state(blob)
    assert blob == {"session_id": _SID}


# -- the managed flags and extra_args ----------------------------------------


def test_permission_mode_model_and_reasoning_effort_map_to_their_flags() -> None:
    target = _FakeTarget({f"{_SID}.jsonl": _FakeResult(1)})
    command = _harness_integration(
        {"permission_mode": "acceptEdits", "model": "sonnet", "reasoning_effort": "future-effort"}
    ).start(_op_ctx(target))
    assert "--permission-mode acceptEdits" in command
    assert "--model sonnet" in command
    assert "--effort future-effort" in command


def _claude_argv(command: str) -> list[str]:
    outer = shlex.split(command)
    assert outer[:2] == ["sh", "-c"]
    inner = shlex.split(outer[2])
    claude = inner.index("claude")
    return inner[claude + 1 :]


def test_session_preferences_default_off() -> None:
    target = _FakeTarget({f"{_SID}.jsonl": _FakeResult(1)})
    argv = _claude_argv(_harness_integration().start(_op_ctx(target)))
    assert "--effort" not in argv
    assert "--remote-control" not in argv
    assert "--settings" not in argv


def test_remote_control_uses_the_session_name_as_its_title() -> None:
    target = _FakeTarget({f"{_SID}.jsonl": _FakeResult(1)})
    argv = _claude_argv(
        _harness_integration({"remote_control": True}, session_name="my-session").start(_op_ctx(target))
    )
    index = argv.index("--remote-control")
    assert argv[index + 1] == "my-session"


def test_fresh_workload_uses_native_controls_and_separates_the_prompt() -> None:
    target = _FakeTarget({f"{_SID}.jsonl": _FakeResult(1)})
    goal = "Finish safely; printf 'done'"
    initial = "Begin with the failing test"
    argv = _claude_argv(
        _harness_integration(
            {
                "goal": goal,
                "initial_prompt": initial,
                "agent": "reviewer",
                "append_system_prompt": "Keep the diff focused",
                "extra_args": ["--future-flag"],
            }
        ).start(_op_ctx(target))
    )

    assert argv[argv.index("--agent") + 1] == "reviewer"
    assert argv[argv.index("--append-system-prompt") + 1] == "Keep the diff focused"
    prompt = next(token for token in argv if token.startswith("/goal "))
    assert prompt.index(goal) < prompt.index(initial)
    assert argv[argv.index("--") - 1] == "--future-flag"
    assert argv[-1] == prompt


@pytest.mark.parametrize("initial_prompt", ["doctor", "--version"])
def test_fresh_initial_prompt_cannot_be_parsed_as_a_command_or_option(initial_prompt: str) -> None:
    target = _FakeTarget({f"{_SID}.jsonl": _FakeResult(1)})
    argv = _claude_argv(
        _harness_integration({"initial_prompt": initial_prompt, "extra_args": ["--model", "opus"]}).start(
            _op_ctx(target)
        )
    )

    assert argv[-2:] == ["--", f"\n{initial_prompt}"]
    assert argv.index("opus") < argv.index("--")


def test_resume_reapplies_process_controls_but_not_fresh_conversation_content() -> None:
    target = _FakeTarget({f"{_SID}.jsonl": _FakeResult(0)})
    argv = _claude_argv(
        _harness_integration(
            {
                "goal": "Fresh goal",
                "initial_prompt": "Fresh prompt",
                "agent": "reviewer",
                "append_system_prompt": "Fresh instructions",
            }
        ).resume(_op_ctx(target))
    )
    assert argv[argv.index("--agent") + 1] == "reviewer"
    assert argv[argv.index("--append-system-prompt") + 1] == "Fresh instructions"
    assert all("Fresh goal" not in token and "Fresh prompt" not in token for token in argv)


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        pytest.param({"vim_mode": True}, {"editorMode": "vim"}, id="vim-only"),
        pytest.param(
            {"terminal_bell": True},
            {"preferredNotifChannel": "terminal_bell"},
            id="bell-only",
        ),
        pytest.param(
            {"vim_mode": True, "terminal_bell": True},
            {"editorMode": "vim", "preferredNotifChannel": "terminal_bell"},
            id="combined",
        ),
    ],
)
def test_vim_mode_and_terminal_bell_share_session_local_settings(
    config: dict[str, object], expected: dict[str, str]
) -> None:
    target = _FakeTarget({f"{_SID}.jsonl": _FakeResult(1)})
    argv = _claude_argv(_harness_integration(config).start(_op_ctx(target)))
    index = argv.index("--settings")
    assert json.loads(argv[index + 1]) == expected
    assert argv.count("--settings") == 1


def test_extra_args_appended_verbatim_last_and_quoted() -> None:
    target = _FakeTarget({f"{_SID}.jsonl": _FakeResult(1)})
    command = _harness_integration(
        {"model": "opus", "reasoning_effort": "high", "vim_mode": True, "extra_args": ["--foo", "bar baz"]}
    ).start(_op_ctx(target))
    # One argv token stays one token: "bar baz" is quoted, not re-split.
    assert shlex.quote("bar baz") in command
    # Appended last: after the managed flag and settings override.
    assert command.index("--model") < command.index("--foo")
    assert command.index("--effort") < command.index("--foo")
    assert command.index("--settings") < command.index("--foo")


def test_raw_settings_in_extra_args_follow_generated_settings() -> None:
    target = _FakeTarget({f"{_SID}.jsonl": _FakeResult(1)})
    raw_settings = '{"editorMode":"normal"}'
    argv = _claude_argv(
        _harness_integration(
            {
                "vim_mode": True,
                "extra_args": ["--settings", raw_settings],
            }
        ).start(_op_ctx(target))
    )
    settings_indexes = [index for index, token in enumerate(argv) if token == "--settings"]
    assert len(settings_indexes) == 2
    assert json.loads(argv[settings_indexes[0] + 1]) == {"editorMode": "vim"}
    assert argv[settings_indexes[1] + 1] == raw_settings


def test_extra_args_with_shell_metacharacters_cannot_inject() -> None:
    """``extra_args`` is operator-supplied and NOT name-validated (unlike
    ``session_name``), so an adversarial value with quotes/metacharacters
    must be ``shlex.quote``d into one inert argv token, never shell-active."""
    payload = "a'; touch /tmp/pwned #"
    target = _FakeTarget({f"{_SID}.jsonl": _FakeResult(1)})
    command = _harness_integration({"extra_args": ["--append-system-prompt", payload]}).start(_op_ctx(target))

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
    assert "--name s1" in _harness_integration().start(_op_ctx(present))
    assert "--name s1" in _harness_integration().start(_op_ctx(absent))


# -- the returned pane string shape ------------------------------------------


def test_returned_string_is_a_single_sh_c_that_echoes_then_execs() -> None:
    """A single ``sh -c`` (so it survives the pane's ``exec`` wrapping),
    echoing the visible decision before exec-ing claude."""
    target = _FakeTarget({f"{_SID}.jsonl": _FakeResult(1)})
    command = _harness_integration().start(_op_ctx(target))
    assert command.startswith("sh -c ")
    assert "echo " in command
    assert "exec claude" in command


# -- readiness probes claude -------------------------------------------------


def test_readiness_probes_claude() -> None:
    harness_integration = _harness_integration()
    target = _FakeTarget()  # command -v claude -> default ok
    harness_integration.preflight(RunContext(operation_scope=_session_scope(), admin_target=target))
    assert any("command -v claude" in cmd for cmd in target.commands)


def test_readiness_missing_claude_is_a_typed_error() -> None:
    harness_integration = _harness_integration()
    target = _FakeTarget({"command -v claude": _FakeResult(1)})
    ctx = RunContext(operation_scope=_session_scope(), admin_target=target)
    with pytest.raises(StateError, match="'claude-code' harness integration.*requires 'claude'"):
        harness_integration.preflight(ctx)
