"""The ``codex`` harness integration: config vocabulary, the create-is-always-fresh
invariant, the notify-bound resume decision (all five leaves), the ``notify``
override on every launch form, the source-filtered discovery fallback, the
recorder script itself, the flag mapping and ``extra_args`` passthrough, the
visible decision, and that readiness probes ``codex``.

Note which op each test drives. Only ``resume`` runs the decision tree, so
that is what the fork tests call; ``start`` is unconditionally fresh, and the
tests that call it exist to pin exactly that (nothing probed, nothing
adopted), including the recreated-namesake scenario end to end.

Three stub layers, per what each behavior actually lives in:

- ``_FakeTarget`` (exit-code stubs) drives the integration-side forks: which
  probe runs when, how each exit code is classified, what lands in the
  state blob and the pane string.
- ``_ShellTarget`` EXECUTES the generated probes against a real scratch
  filesystem through ``sh``, because their crux (the ``"source":"cli"``
  plus session-cwd filter that keeps subagent rollouts out of the candidate
  set) is implemented target-side in the probe command itself; an
  exit-code stub cannot pin that a subagent or foreign-cwd rollout is
  filtered out rather than adopted.
- The RECORDER is provisioned by really running the generated provisioning
  fragment through ``sh``, then invoked with fixture payloads, because it
  is a shell script whose whole job is discriminating payloads.

No real ``codex`` binary anywhere; the rollout files and notify payloads are
hand-written fixtures shaped like the ones the decisions doc records from
codex-cli 0.146.0.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from typing import TYPE_CHECKING

import pytest

from agentworks.capabilities.base import OperationScope, RunContext, ScopeLevel
from agentworks.capabilities.config import capability_config_references, validate_capability_config
from agentworks.errors import ConfigError, StateError
from agentworks.plugins.codex.harness_integration import CodexIntegration
from agentworks.schema import RefOwner
from tests.conftest import _FakeResult, _FakeTarget

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

_SID = "939b1597-7c61-4ace-80f4-14617b7b4257"  # a fixed bound uuid
_OTHER_SID = "11111111-2222-4333-8444-555555555555"
_THIRD_SID = "22222222-3333-4444-8555-666666666666"
_ROLLOUT = f"/home/me/.codex/sessions/2026/08/01/rollout-2026-08-01T12-00-00-{_SID}.jsonl"
_OTHER_ROLLOUT = f"/home/me/.codex/sessions/2026/08/01/rollout-2026-08-01T12-05-00-{_OTHER_SID}.jsonl"

# A marker-era blob key (2026-08-01 through 2026-08-04), retired by the
# notify-bound redesign; a legacy blob still carrying one must be cleaned up.
_LEGACY_MARKER = ".agentworks/codex/s1-deadbeefdeadbeefdeadbeefdeadbeef.launch"

# Substring keys for the three op-time probes: the recorder read carries the
# session's ``.thread`` path, the rollout-presence probe the bound id's glob,
# and discovery the source needle.
_RECORDER_PROBE = ".thread"
_ROLLOUT_PROBE = f"*-{_SID}.jsonl"
_DISCOVERY_PROBE = "-exec awk"


def _harness_integration(
    config: Mapping[str, object] | None = None,
    *,
    session_name: str = "s1",
    workspace_path: str = "/srv/ws1",
    state: dict[str, object] | None = None,
    admin: bool = True,
) -> CodexIntegration:
    return CodexIntegration(
        "codex",
        config or {},
        session_name=session_name,
        vm_name="box",
        workspace_name="ws1",
        workspace_path=workspace_path,
        target=None,
        admin=admin,
        state={"session_id": _SID} if state is None else state,
    )


def _target(
    *,
    recorded: str | None = None,
    recorder_exit: int | None = None,
    rollout: int | None = None,
    discovered: str | None = None,
    discovery_exit: int | None = None,
) -> _FakeTarget:
    """A transport double keyed on the three op-time probes.

    The defaults are the do-nothing world: nothing recorded by the notify
    recorder (the file is absent, exit 1) and no discovery candidate, which
    is the plain fresh-launch path. ``recorded`` seeds a recorder file
    holding that uuid; ``rollout`` sets the bound id's presence-probe exit;
    ``discovered`` is the discovery probe's stdout (rollout paths, one per
    line). The ``*_exit`` forms force a raw exit code for the failure forks.
    """
    responses: dict[str, _FakeResult] = {}
    if recorder_exit is not None:
        responses[_RECORDER_PROBE] = _FakeResult(recorder_exit)
    elif recorded is not None:
        responses[_RECORDER_PROBE] = _FakeResult(0, stdout=f"{recorded}\n")
    else:
        responses[_RECORDER_PROBE] = _FakeResult(1)
    if rollout is not None:
        responses[_ROLLOUT_PROBE] = _FakeResult(rollout)
    if discovery_exit is not None:
        responses[_DISCOVERY_PROBE] = _FakeResult(discovery_exit)
    else:
        responses[_DISCOVERY_PROBE] = _FakeResult(0, stdout=discovered or "")
    return _FakeTarget(responses)


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


def _inner(command: str) -> str:
    """The inner command of the returned ``sh -c '<inner>'`` pane string."""
    outer = shlex.split(command)
    assert outer[:2] == ["sh", "-c"]
    return outer[2]


def _echo(command: str) -> str:
    """The decision line the pane echoes, with the generated quoting
    peeled off (the messages carry apostrophes, so they are not literal
    substrings of the returned command)."""
    tokens = shlex.split(_inner(command))
    assert tokens[0] == "echo"
    return tokens[1]


# -- config vocabulary -------------------------------------------------------


def _refs(blob: dict[str, object]) -> tuple[object, ...]:
    return capability_config_references(
        kind="harness-integration",
        config={"name": "codex", **blob},
        owner=RefOwner(kind="session-template", name="codex"),
    )


def test_it_implies_no_reference() -> None:
    """``codex`` names no Resource in its config, and extraction is
    total: it returns ``()`` for the known fields and for a malformed blob
    alike."""
    assert _refs({"model": "x"}) == ()
    assert _refs({"model": 3, "nonsense": "typo"}) == ()


def _validate(blob: dict[str, object]) -> None:
    """Validation is the CORE's now: it reads the model this integration
    declares, and no integration code runs."""
    validate_capability_config(
        kind="harness-integration",
        config={"name": "codex", **blob},
        owner=RefOwner(kind="session-template", name="codex"),
    )


def test_validation_accepts_the_config_vocabulary_and_empty_config() -> None:
    _validate(
        {
            "model": "gpt-5",
            "sandbox": "workspace-write",
            "approval_policy": "on-request",
            "profile": "work",
            "network": True,
            "approvals_reviewer": "auto_review",
            "reasoning_effort": "high",
            "vim_mode": True,
            "writable_dirs": ["/srv/cache"],
            "web_search": "cached",
            "disable_strict_config": False,
            "extra_args": ["--foo"],
        }
    )
    _validate({})


def test_validation_rejects_unknown_field() -> None:
    with pytest.raises(ConfigError):
        _validate({"sandbx": "typo"})


@pytest.mark.parametrize("field_name", ["model", "sandbox", "approval_policy", "profile"])
def test_validation_rejects_non_string_flag_fields(field_name: str) -> None:
    with pytest.raises(ConfigError, match=f"{field_name}: must be a string"):
        _validate({field_name: 3})


def test_validation_rejects_non_list_extra_args() -> None:
    with pytest.raises(ConfigError):
        _validate({"extra_args": "just-a-string"})


def test_construct_revalidates_config() -> None:
    with pytest.raises(ConfigError):
        _harness_integration({"nope": 1})


# -- create is always fresh; only resume adopts -------------------------------


def test_create_is_always_fresh_even_with_a_recording_and_a_candidate() -> None:
    """The pinned invariant: ``session create`` mints a brand-new session
    row, which by definition owns no codex conversation, so ``start``
    consults NOTHING. Both identity channels are name-derived on the target
    (the recorder file is ``<session-name>.thread``, discovery matches the
    workspace directory), so adopting at create time is exactly how a
    brand-new session would inherit a deleted namesake's conversation, or a
    stranger's manual codex run in the same directory. This target offers
    both a valid recording and a lone interactive candidate; create takes
    neither and makes no round trip at all."""
    state: dict[str, object] = {}
    target = _target(recorded=_SID, rollout=0, discovered=f"{_ROLLOUT}\n")
    harness_integration = _harness_integration(state=state)
    command = harness_integration.start(_op_ctx(target))
    assert target.commands == []  # nothing was probed, so nothing could be adopted
    assert state == {}
    assert harness_integration.launch_note() == "No existing Codex session. Starting a new one..."
    assert "starting new session s1" in _echo(command)
    assert "resume" not in _sh_argv(command, home="/home/me")


def test_create_needs_no_launch_target() -> None:
    """Create decides nothing, so an op context without a launch target is
    not a problem it has to refuse: there is no resume-vs-launch guess to
    make (contrast the resume path, which raises)."""
    command = _harness_integration(state={}).start(RunContext())
    assert "starting new session s1" in _echo(command)


def test_create_clears_a_stale_recording_and_retires_legacy_keys() -> None:
    """The second half of the namesake guarantee: create adopts nothing AND
    removes any leftover ``.thread`` file, so the resume that follows it
    cannot adopt the dead namesake's id either. Marker-era blob keys go the
    same way."""
    state: dict[str, object] = {"discovery_marker": _LEGACY_MARKER}
    inner = _inner(_harness_integration(state=state).start(_op_ctx(_target(recorded=_SID))))
    assert state == {}
    assert 'rm -f "$HOME"/.agentworks/codex/s1.thread' in inner
    assert f'rm -f "$HOME"/{_LEGACY_MARKER}' in inner


# -- layer 1: the notify binding ---------------------------------------------


def test_recorded_thread_id_binds_a_session_that_had_nothing_stored() -> None:
    """The primary path: codex reported the conversation's thread-id
    through the notify recorder, so the next op resumes it deterministically
    with no inference at all, and the id lands in the blob."""
    state: dict[str, object] = {}
    target = _target(recorded=_SID, rollout=0)
    command = _harness_integration(state=state).resume(_op_ctx(target))
    assert state == {"session_id": _SID}
    assert f"resume {_SID}" in command
    assert "resuming session s1" in _echo(command)
    # Binding made discovery unnecessary: the fallback probe never ran.
    assert all(_DISCOVERY_PROBE not in cmd for cmd in target.commands)


def test_recorded_thread_id_wins_over_a_differing_stored_id() -> None:
    """Last write wins, deliberately: the recorder names the conversation
    most recently live in this session's pane, so a picker-esc fresh
    conversation rebinds the session to what the operator is looking at
    rather than to the id an earlier op stored."""
    state: dict[str, object] = {"session_id": _SID}
    target = _target(recorded=_OTHER_SID, rollout=None)
    target.responses[f"*-{_OTHER_SID}.jsonl"] = _FakeResult(0)
    command = _harness_integration(state=state).resume(_op_ctx(target))
    assert state == {"session_id": _OTHER_SID}
    assert f"resume {_OTHER_SID}" in command


def test_recorder_content_that_is_not_a_uuid_is_treated_as_unbound() -> None:
    """The recorder only ever writes one uuid, so anything else in the
    file is not ours to interpret: the op falls through to discovery
    instead of handing codex a garbage id."""
    state: dict[str, object] = {}
    target = _target(recorded="not-a-uuid")
    command = _harness_integration(state=state).resume(_op_ctx(target))
    assert "session_id" not in state
    assert "starting new session s1" in _echo(command)


def test_recorder_file_that_exists_but_will_not_read_raises() -> None:
    """A file that is there and unreadable is a probe that could not run,
    not an answer: guessing unbound could orphan the conversation it
    names.

    One exit code stands for both: an SSH failure's 255 reaches the same
    raise with the same message, and what tells the two apart is the HINT,
    which ``test_raise_hints_name_a_recovery_the_operator_can_actually_take``
    pins for each.
    """
    with pytest.raises(StateError) as exc:
        _harness_integration().resume(_op_ctx(_target(recorder_exit=6)))
    assert exc.value.entity_name == "s1"


def test_raise_hints_name_a_recovery_the_operator_can_actually_take() -> None:
    """Hint accuracy is the point of this integration, so the hints fork on
    WHY the probe could not answer.

    A file that exists and will not read is not a reachability problem, and
    neither is a ``find`` that failed against the on-disk sessions tree:
    "retry once the target is reachable" would point at a healthy
    component. The recorder case gets the recovery that is actually safe,
    deleting the recording, which costs determinism for one op and orphans
    nothing because resume then falls through to discovery."""
    with pytest.raises(StateError) as unreadable:
        _harness_integration().resume(_op_ctx(_target(recorder_exit=6)))
    hint = unreadable.value.hint or ""
    assert "Remove ~/.agentworks/codex/s1.thread" in hint
    assert "nothing is orphaned" in hint
    assert "reachable" not in hint

    with pytest.raises(StateError) as find_failed:
        _harness_integration().resume(_op_ctx(_target(rollout=5)))
    assert "codex sessions directory" in (find_failed.value.hint or "")

    # A transport failure IS reachability, and still says so.
    with pytest.raises(StateError) as unreachable:
        _harness_integration().resume(_op_ctx(_target(rollout=255)))
    assert (unreachable.value.hint or "") == "Retry once the launch target is reachable."


# -- layer 2: source-filtered discovery --------------------------------------


def test_single_candidate_is_adopted_and_resumed() -> None:
    """One source-filtered rollout in this workspace: its uuid (read from
    the FILENAME) is adopted into the state blob and resumed."""
    state: dict[str, object] = {}
    harness_integration = _harness_integration(state=state)
    command = harness_integration.resume(_op_ctx(_target(discovered=f"{_ROLLOUT}\n", rollout=0)))
    assert state == {"session_id": _SID}
    assert harness_integration.state == {"session_id": _SID}  # in place, via the property
    assert f"resume {_SID}" in command
    assert "identified this session's codex conversation from codex's on-disk state" in _echo(command)


def test_zero_candidates_launches_fresh() -> None:
    state: dict[str, object] = {}
    command = _harness_integration(state=state).resume(_op_ctx(_target()))
    assert state == {}
    assert "starting new session s1" in _echo(command)
    assert "resume" not in _sh_argv(command, home="/home/me")


def test_several_candidates_open_the_picker_instead_of_raising() -> None:
    """Ambiguity is a human decision in the pane, never an error (the
    2026-08-04 redesign's core reversal): the op launches codex's own
    cwd-scoped session picker and binds nothing, so the next completed
    turn's notify recording settles the identity."""
    state: dict[str, object] = {}
    command = _harness_integration(state=state).resume(_op_ctx(_target(discovered=f"{_ROLLOUT}\n{_OTHER_ROLLOUT}\n")))
    assert state == {}  # nothing adopted: the human picks
    assert "exec codex resume -c tui.resume_cwd=current" in command
    assert "could not identify this session's codex conversation with confidence" in _echo(command)
    assert "press esc to start a fresh conversation" in _echo(command)


def test_a_candidate_whose_filename_has_no_uuid_opens_the_picker() -> None:
    """A rollout that passed the filter but whose name does not embed a
    uuid cannot be adopted, and ignoring it could turn a genuinely
    ambiguous workspace into a confident single adoption; it forces the
    picker so the human decides."""
    weird = "/home/me/.codex/sessions/2026/08/01/rollout-weird.jsonl"
    state: dict[str, object] = {}
    command = _harness_integration(state=state).resume(_op_ctx(_target(discovered=f"{_ROLLOUT}\n{weird}\n")))
    assert state == {}
    assert "exec codex resume -c tui.resume_cwd=current" in command
    # Even ALONE it opens the picker rather than raising, and the wording
    # covers that: this leaf is not always "multiple candidates", so the
    # echo has to be asserted or a wrong promise hides here.
    alone = _harness_integration(state={}).resume(_op_ctx(_target(discovered=f"{weird}\n")))
    assert "exec codex resume -c tui.resume_cwd=current" in alone
    assert "could not identify this session's codex conversation with confidence" in _echo(alone)


def test_discovery_ignores_login_shell_noise_lines() -> None:
    """The probe runs through a login shell whose dotfiles may echo;
    stdout lines that are not shaped like a rollout path are ignored, so
    noise cannot fake a candidate and push a clean adoption into the
    picker."""
    state: dict[str, object] = {}
    noisy = f"Welcome to devbox!\n{_ROLLOUT}\nmise activated\n"
    command = _harness_integration(state=state).resume(_op_ctx(_target(discovered=noisy, rollout=0)))
    assert state == {"session_id": _SID}
    assert f"resume {_SID}" in command


def test_discovery_without_a_sessions_dir_launches_fresh() -> None:
    """The probe's one definitive-empty exit (3): codex never ran on this
    target, so there is nothing to adopt."""
    state: dict[str, object] = {}
    command = _harness_integration(state=state).resume(_op_ctx(_target(discovery_exit=3)))
    assert state == {}
    assert "starting new session s1" in _echo(command)


def test_discovery_with_an_unresolvable_workspace_raises() -> None:
    with pytest.raises(StateError):
        _harness_integration(state={}).resume(_op_ctx(_target(discovery_exit=4)))


def test_discovery_probe_failure_raises_rather_than_guessing() -> None:
    """A probe that FAILED (a find error's 5, an SSH failure's 255) is not
    a probe that found nothing: for an enumeration a partial listing could
    turn "several candidates" into one confident wrong adoption."""
    for code in (5, 255):
        with pytest.raises(StateError):
            _harness_integration(state={}).resume(_op_ctx(_target(discovery_exit=code)))


# -- the stale-id fork -------------------------------------------------------


def test_gone_rollout_drops_the_id_and_falls_through_to_a_fresh_launch() -> None:
    """An archived or deleted rollout is not resumable (the pinned
    archived policy): the op drops the stale id, finds no candidate, and
    launches fresh with the stale-specific decision."""
    state: dict[str, object] = {"session_id": _SID}
    command = _harness_integration(state=state).resume(_op_ctx(_target(rollout=1)))
    assert "resume" not in _sh_argv(command, home="/home/me")
    assert "archived or gone; starting new session s1" in _echo(command)
    assert state == {}  # stale id dropped


def test_a_dropped_stale_binding_is_reported_alongside_whatever_replaced_it() -> None:
    """Dropping a stale binding is news in its own right, so it composes
    INTO the leaf that follows instead of being overwritten by it.

    An operator who ran ``codex archive`` deliberately and then finds the
    pane in a different conversation needs both halves: that their previous
    binding is gone, and what took its place. Neither surface may report
    only one of them (the bug this pins: the decision leaf used to be
    overwritten by the adoption, so the drop went unmentioned on both).

    The first block is where the fall-through itself is pinned too: the
    stale drop continues INTO discovery rather than stopping at fresh, so
    an archived conversation cannot block adopting the one actually in
    this workspace."""
    # Stale then adopted: the drop plus the adopted uuid, on both surfaces.
    adopted = _harness_integration(state={"session_id": _SID})
    command = adopted.resume(_op_ctx(_target(rollout=1, discovered=f"{_OTHER_ROLLOUT}\n")))
    assert adopted.launch_note() == (
        f"Previous Codex conversation is archived or gone. Identified a different Codex "
        f"conversation in this workspace from Codex's own on-disk state: {_OTHER_SID}. Resuming..."
    )
    echo = _echo(command)
    assert "previous codex conversation archived or gone" in echo
    assert (
        f"identified a different codex conversation in this workspace from codex's on-disk state ({_OTHER_SID})" in echo
    )

    # Stale then picker: the drop plus why the picker is opening.
    picker = _harness_integration(state={"session_id": _SID})
    picker_command = picker.resume(_op_ctx(_target(rollout=1, discovered=f"{_ROLLOUT}\n{_OTHER_ROLLOUT}\n")))
    note = picker.launch_note()
    assert note is not None
    assert note.startswith("Previous Codex conversation is archived or gone. Could not identify")
    assert "previous codex conversation archived or gone" in _echo(picker_command)

    # And with nothing to replace it, the bare archived-or-gone leaf stands,
    # which is the whole of what the stale drop says on its own.
    alone = _harness_integration(state={"session_id": _SID})
    alone.resume(_op_ctx(_target(rollout=1)))
    assert alone.launch_note() == "Previous Codex session is archived or gone. Starting a new one..."


def test_rollout_probe_that_could_not_execute_raises_rather_than_guessing() -> None:
    """A non-{0,1} exit (an SSH failure's 255, a shell that could not
    start) means the probe never ran. Guessing "gone" would drop the
    bound id and orphan a resumable conversation; the op raises a typed
    error naming the target instead."""
    with pytest.raises(StateError) as exc:
        _harness_integration().resume(_op_ctx(_target(rollout=255)))
    assert "exit 255" in str(exc.value)
    assert exc.value.entity_name == "s1"


def test_rollout_probe_keeps_find_failure_distinct_from_a_clean_no_match() -> None:
    """The inner command's structure: a missing sessions dir short-circuits
    to the clean no-match exit (1, not resumable), while a find that
    FAILED without printing a match exits 5, which the exit-code fork
    raises on rather than folding into "no rollout"."""
    target = _target(rollout=0)
    _harness_integration().resume(_op_ctx(target))
    probe_cmd = next(cmd for cmd in target.commands if _ROLLOUT_PROBE in cmd)
    assert "[ -d " in probe_cmd  # dir-missing is a clean no-match, not a find failure
    assert "exit 5" in probe_cmd  # find failure stays distinguishable
    with pytest.raises(StateError):
        _harness_integration().resume(_op_ctx(_target(rollout=5)))


def test_rollout_probe_is_rooted_at_codex_home_and_finds_by_the_bound_id() -> None:
    target = _target(rollout=0)
    _harness_integration().resume(_op_ctx(target))
    probe_cmd = next(cmd for cmd in target.commands if _ROLLOUT_PROBE in cmd)
    assert "find" in probe_cmd
    # Rooted at the CLI's state dir with its documented override.
    assert "CODEX_HOME" in probe_cmd


# -- state hygiene -----------------------------------------------------------


def test_resume_without_a_launch_target_raises_rather_than_guessing() -> None:
    """No launch target means no probe can run; unlike claude-code (whose
    fresh launch keeps its minted id), a codex fresh launch drops the
    bound id, so the resume op refuses to guess."""
    with pytest.raises(StateError):
        _harness_integration().resume(RunContext())


def test_wrong_typed_stored_id_is_swept_and_treated_as_absent() -> None:
    """A malformed stored value (the blob is only as trustworthy as the
    DB it came from) is swept out of the namespace, not merely skipped."""
    state: dict[str, object] = {"session_id": 7}
    command = _harness_integration(state=state).resume(_op_ctx(_target()))
    assert "starting new session s1" in _echo(command)
    assert state == {}  # the garbage was swept, not left behind


def test_legacy_discovery_marker_is_retired_from_the_blob_and_the_disk() -> None:
    """Marker-era state (2026-08-01 through 2026-08-04) is compatibility
    debris: no reader consults it, the key is deleted on the first op that
    touches the blob, and the pane removes the file best-effort so nothing
    dead outlives its blob entry."""
    state: dict[str, object] = {"session_id": _SID, "discovery_marker": _LEGACY_MARKER}
    command = _harness_integration(state=state).resume(_op_ctx(_target(rollout=0)))
    assert state == {"session_id": _SID}  # the retired key is gone
    assert f'rm -f "$HOME"/{_LEGACY_MARKER}' in _inner(command)
    # A blob with ONLY the legacy key still works: it just means unbound.
    legacy_only: dict[str, object] = {"discovery_marker": _LEGACY_MARKER}
    fresh = _harness_integration(state=legacy_only).resume(_op_ctx(_target()))
    assert legacy_only == {}
    assert f'rm -f "$HOME"/{_LEGACY_MARKER}' in _inner(fresh)


def test_bound_id_is_resumed_verbatim_on_the_next_op() -> None:
    """The binding round trip: an id recorded by the notify recorder on
    one op is read back from the state blob and resumed on the next, even
    once the recorder file is gone."""
    state: dict[str, object] = {}
    _harness_integration(state=state).resume(_op_ctx(_target(recorded=_SID, rollout=0)))
    assert state == {"session_id": _SID}
    command = _harness_integration(state=state).resume(_op_ctx(_target(rollout=0)))
    assert f"resume {_SID}" in command
    assert "resuming session s1" in _echo(command)


# -- launch_note: the five decision leaves ------------------------------------


def test_launch_note_reports_resume() -> None:
    harness_integration = _harness_integration()
    assert harness_integration.launch_note() is None  # nothing decided before the op
    harness_integration.resume(_op_ctx(_target(rollout=0)))
    assert harness_integration.launch_note() == "Existing Codex session found. Resuming..."


def test_launch_note_reports_fresh_start() -> None:
    harness_integration = _harness_integration(state={})
    harness_integration.resume(_op_ctx(_target()))
    assert harness_integration.launch_note() == "No existing Codex session. Starting a new one..."


def test_launch_note_reports_adoption_and_names_the_adopted_id() -> None:
    """Adoption is the one leaf that is explicitly a heuristic, so both
    operator surfaces name the uuid: the whole reason for announcing the
    decision is that a wrong one can be caught, which needs something
    checkable against the conversation the pane comes up in."""
    harness_integration = _harness_integration(state={})
    command = harness_integration.resume(_op_ctx(_target(discovered=f"{_ROLLOUT}\n", rollout=0)))
    assert harness_integration.launch_note() == (
        f"Identified this session's Codex conversation from Codex's own on-disk state: {_SID}. Resuming..."
    )
    assert f"on-disk state ({_SID})" in _echo(command)


def test_launch_note_reports_the_picker_including_what_esc_does() -> None:
    """The picker note carries the whole operator story: why the pane is
    showing a picker, what picking binds, and what esc does."""
    harness_integration = _harness_integration(state={})
    harness_integration.resume(_op_ctx(_target(discovered=f"{_ROLLOUT}\n{_OTHER_ROLLOUT}\n")))
    note = harness_integration.launch_note()
    assert note is not None
    assert note.startswith("Could not identify this session's Codex conversation with confidence.")
    assert "session picker is opening in the pane" in note
    assert "binds this session to that conversation from its next turn" in note
    assert "esc starts a fresh conversation instead" in note


# -- the notify override on every launch form ---------------------------------


def _notify_token(command: str) -> str:
    """The single ``notify=[...]`` argv token, with ``$HOME`` resolved by a
    real ``sh``, as codex itself would receive it."""
    tokens = _sh_argv(command, home="/home/me")
    return next(token for token in tokens if token.startswith("notify="))


def _sh_argv(command: str, *, home: str) -> list[str]:
    """The argv ``exec codex`` would receive, resolved by a real ``sh``:
    the generated tokens go through the same expansion and word-splitting
    the pane's shell applies, so a mis-quoted token shows up as split or
    unexpanded here rather than only in production."""
    args = _inner(command).split("exec codex ", 1)[1]
    proc = subprocess.run(
        ["sh", "-c", f"printf '%s\\n' {args}"],
        env={"HOME": home, "PATH": os.environ["PATH"]},
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.splitlines()


@pytest.mark.parametrize(
    ("state", "kwargs", "expected_head"),
    [
        ({"session_id": _SID}, {"rollout": 0}, ["resume", _SID, "-c", "tui.resume_cwd=current"]),
        ({}, {}, []),
        ({}, {"discovered": f"{_ROLLOUT}\n{_OTHER_ROLLOUT}\n"}, ["resume", "-c", "tui.resume_cwd=current"]),
    ],
    ids=["resume-bound-id", "fresh", "picker"],
)
def test_every_launch_form_attaches_the_notify_recorder(
    state: dict[str, object], kwargs: dict[str, object], expected_head: list[str]
) -> None:
    """All three generated forms carry the binding, which is what makes
    the picker and picker-esc paths self-heal into deterministic resume."""
    command = _harness_integration(state=dict(state)).resume(_op_ctx(_target(**kwargs)))  # type: ignore[arg-type]
    argv = _sh_argv(command, home="/home/me")
    assert argv[: len(expected_head)] == expected_head
    assert (
        argv.count('notify=["/home/me/.agentworks/codex/record-thread-v1.sh","/home/me/.agentworks/codex/s1.thread"]')
        == 1
    )
    assert "-c" in argv


def test_notify_survives_the_shell_nesting_as_one_token_with_home_expanded() -> None:
    """The token has three quoting layers to survive (its own, the
    ``sh -c`` wrapper, the pane's ``$SHELL -lic`` wrapper) AND needs
    ``$HOME`` expanded target-side, since codex spawns the recorder by
    absolute path with no shell of its own. Both paths in one assertion,
    run through a real ``sh``."""
    command = _harness_integration(state={}).resume(_op_ctx(_target()))
    tokens = _sh_argv(command, home="/var/lib/me")
    assert tokens.count("-c") == 1  # the token did not split into several words
    assert (
        'notify=["/var/lib/me/.agentworks/codex/record-thread-v1.sh","/var/lib/me/.agentworks/codex/s1.thread"]'
        in tokens
    )


def test_notify_lands_after_the_managed_flags_and_before_extra_args() -> None:
    """Emission order is the operator-override contract: later codex ``-c``
    overrides win, so ``extra_args`` must be able to replace our
    ``notify`` (see the next test) while every managed flag precedes it."""
    command = _harness_integration(
        {
            "model": "gpt-5",
            "network": True,
            "approvals_reviewer": "auto_review",
            "reasoning_effort": "high",
            "vim_mode": True,
            "writable_dirs": ["/srv/cache"],
            "web_search": "live",
            "extra_args": ["--foo"],
        },
        state={},
    ).resume(_op_ctx(_target()))
    argv = _sh_argv(command, home="/home/me")
    notify_at = next(i for i, token in enumerate(argv) if token.startswith("notify="))
    for managed in (
        "--strict-config",
        "gpt-5",
        "sandbox_workspace_write.network_access=true",
        'approvals_reviewer="auto_review"',
        'model_reasoning_effort="high"',
        "tui.vim_mode_default=true",
        "/srv/cache",
        'web_search="live"',
    ):
        assert argv.index(managed) < notify_at
    assert notify_at < argv.index("--foo")


def test_extra_args_can_override_the_notify_binding() -> None:
    """The documented escape-hatch cost: an operator ``notify`` in
    ``extra_args`` lands after ours and silently replaces it (codex's
    last-override-wins), disabling the id binding. Discovery and the
    picker still hold, so this pins the ORDER, not a promise that the
    binding survives."""
    command = _harness_integration(
        {"extra_args": ["-c", 'notify=["/usr/local/bin/mine"]']},
        state={},
    ).resume(_op_ctx(_target()))
    argv = _sh_argv(command, home="/home/me")
    ours = next(i for i, token in enumerate(argv) if token.startswith('notify=["/home/me'))
    assert ours < argv.index('notify=["/usr/local/bin/mine"]')


# -- the managed flags and extra_args ----------------------------------------


def test_config_fields_map_to_their_short_flags() -> None:
    command = _harness_integration(
        {
            "model": "gpt-5",
            "sandbox": "workspace-write",
            "approval_policy": "on-request",
            "profile": "work",
        }
    ).resume(_op_ctx(_target(rollout=0)))
    assert "-m gpt-5" in command
    assert "-s workspace-write" in command
    assert "-a on-request" in command
    assert "-p work" in command


def test_strict_config_is_emitted_by_default_on_every_path() -> None:
    """The harness integration owns the emitted config surface, so --strict-config is
    on by default: codex-owned key drift (the network -c override) fails
    loudly at startup instead of being silently ignored."""
    assert "--strict-config" in _harness_integration().resume(_op_ctx(_target(rollout=0)))
    assert "--strict-config" in _harness_integration(state={}).resume(_op_ctx(_target()))
    picker = _harness_integration(state={}).resume(_op_ctx(_target(discovered=f"{_ROLLOUT}\n{_OTHER_ROLLOUT}\n")))
    assert "--strict-config" in picker


def test_disable_strict_config_suppresses_the_flag_on_every_path() -> None:
    resumed = _harness_integration({"disable_strict_config": True}).resume(_op_ctx(_target(rollout=0)))
    assert "--strict-config" not in resumed
    fresh = _harness_integration({"disable_strict_config": True}, state={}).resume(_op_ctx(_target()))
    assert "--strict-config" not in fresh
    # With strict suppressed and no other config, the notify override is
    # the only thing after the bare exec.
    assert _sh_argv(fresh, home="/home/me")[0] == "-c"


def test_network_forwards_both_directions_to_the_config_key() -> None:
    """`true` AND `false` forward explicitly (false overrides a profile
    or config.toml that enabled network access); absent emits nothing."""
    on = _harness_integration({"network": True}).resume(_op_ctx(_target(rollout=0)))
    assert "-c sandbox_workspace_write.network_access=true" in on
    off = _harness_integration({"network": False}).resume(_op_ctx(_target(rollout=0)))
    assert "-c sandbox_workspace_write.network_access=false" in off
    absent = _harness_integration().resume(_op_ctx(_target(rollout=0)))
    assert "network_access" not in absent


def test_approvals_reviewer_forwards_as_a_quoted_toml_string() -> None:
    """The value rides the `approvals_reviewer` config key via -c (codex
    exposes no dedicated flag), quoted as a TOML string; absent emits
    nothing. Values are codex-owned and forward unvalidated."""
    command = _harness_integration({"approvals_reviewer": "auto_review"}).resume(_op_ctx(_target(rollout=0)))
    argv = _sh_argv(command, home="/home/me")
    assert 'approvals_reviewer="auto_review"' in argv
    assert argv[argv.index('approvals_reviewer="auto_review"') - 1] == "-c"
    absent = _harness_integration().resume(_op_ctx(_target(rollout=0)))
    assert "approvals_reviewer" not in absent


@pytest.mark.parametrize(
    ("field", "key", "payload", "expected"),
    [
        (
            "approvals_reviewer",
            "approvals_reviewer",
            'user"\nsandbox_mode="danger-full-access',
            'approvals_reviewer="user\\"\\nsandbox_mode=\\"danger-full-access"',
        ),
        (
            "reasoning_effort",
            "model_reasoning_effort",
            'high"\nsandbox_mode="danger-full-access',
            'model_reasoning_effort="high\\"\\nsandbox_mode=\\"danger-full-access"',
        ),
    ],
)
def test_config_string_fields_escape_toml_structural_characters(
    field: str,
    key: str,
    payload: str,
    expected: str,
) -> None:
    """Codex parses -c key=value as a TOML DOCUMENT splice (verified
    against 0.146.0), so an unescaped newline in the value silently
    defines EXTRA config keys, even under --strict-config, and an
    unescaped quote breaks the value into the raw-string fallback.
    Escaping keeps any operator value one literal string instead."""
    command = _harness_integration({field: payload}).resume(_op_ctx(_target(rollout=0)))
    argv = _sh_argv(command, home="/home/me")
    token = next(t for t in argv if t.startswith(f"{key}="))
    # One argv token, no raw newline, quote and newline TOML-escaped: the
    # smuggled second key stays inert text inside one string value.
    assert "\n" not in token
    assert token == expected
    assert not any(t.startswith("sandbox_mode") for t in argv)


def test_reasoning_effort_forwards_as_a_quoted_toml_string() -> None:
    command = _harness_integration({"reasoning_effort": "high"}).resume(_op_ctx(_target(rollout=0)))
    argv = _sh_argv(command, home="/home/me")
    assert 'model_reasoning_effort="high"' in argv
    assert argv[argv.index('model_reasoning_effort="high"') - 1] == "-c"
    assert "model_reasoning_effort" not in _harness_integration().resume(_op_ctx(_target(rollout=0)))


def test_vim_mode_true_emits_override_and_false_emits_nothing() -> None:
    on = _sh_argv(_harness_integration({"vim_mode": True}).resume(_op_ctx(_target(rollout=0))), home="/home/me")
    assert "tui.vim_mode_default=true" in on
    assert on[on.index("tui.vim_mode_default=true") - 1] == "-c"
    off = _harness_integration({"vim_mode": False}).resume(_op_ctx(_target(rollout=0)))
    assert "tui.vim_mode_default" not in off


def test_writable_dirs_emit_one_add_dir_each_in_order_and_quoted() -> None:
    command = _harness_integration({"writable_dirs": ["/srv/cache", "/data/shared dir"]}).resume(
        _op_ctx(_target(rollout=0))
    )
    argv = _sh_argv(command, home="/home/me")
    assert "/data/shared dir" in argv  # the spaced dir survives as ONE token
    first = argv.index("/srv/cache")
    second = argv.index("/data/shared dir")
    assert argv[first - 1] == "--add-dir"
    assert argv[second - 1] == "--add-dir"
    assert first < second


def test_web_search_legacy_bools_keep_their_existing_behavior() -> None:
    on = _sh_argv(_harness_integration({"web_search": True}).resume(_op_ctx(_target(rollout=0))), home="/home/me")
    off = _sh_argv(_harness_integration({"web_search": False}).resume(_op_ctx(_target(rollout=0))), home="/home/me")
    assert "--search" in on
    assert "--search" not in off
    assert not any(token.startswith("web_search=") for token in off)


def test_web_search_string_forwards_as_a_quoted_toml_string() -> None:
    argv = _sh_argv(
        _harness_integration({"web_search": "indexed"}).resume(_op_ctx(_target(rollout=0))), home="/home/me"
    )
    token = 'web_search="indexed"'
    assert token in argv
    assert argv[argv.index(token) - 1] == "-c"
    assert "--search" not in argv


def test_new_fields_reject_wrong_types() -> None:
    for field, bad in (
        ("network", "yes"),
        ("web_search", 1),
        ("disable_strict_config", "true"),
        ("approvals_reviewer", True),
        ("reasoning_effort", True),
        ("vim_mode", "true"),
        ("writable_dirs", "/srv/cache"),
        ("writable_dirs", [1, 2]),
    ):
        with pytest.raises(ConfigError, match=field):
            _validate({field: bad})


def test_merge_config_unions_writable_dirs_and_child_wins_the_rest() -> None:
    """`writable_dirs` is an additive grant list (like shell's
    required_commands): a child adding one dir must not drop the
    parent's. Scalars/bools and extra_args child-win."""
    merged = CodexIntegration.merge_config(
        {
            "writable_dirs": ["/srv/a"],
            "network": True,
            "reasoning_effort": "medium",
            "vim_mode": False,
            "extra_args": ["--x"],
        },
        {
            "writable_dirs": ["/srv/b", "/srv/a"],
            "network": False,
            "reasoning_effort": "high",
            "vim_mode": True,
            "extra_args": ["--y"],
        },
    )
    assert merged["writable_dirs"] == ["/srv/a", "/srv/b"]
    assert merged["network"] is False
    assert merged["reasoning_effort"] == "high"
    assert merged["vim_mode"] is True
    assert merged["extra_args"] == ["--y"]


def test_merge_config_never_launders_an_invalid_writable_dirs_entry() -> None:
    """merge_config runs on RAW declared blobs (the resolver merges before
    the final validate), so a mixed valid/invalid list must survive the
    merge un-filtered for validate to reject; silently dropping the bad
    entry would produce a valid-looking blob that validate passes."""
    merged = CodexIntegration.merge_config({}, {"writable_dirs": ["/srv/a", 5]})
    assert merged["writable_dirs"] == ["/srv/a", 5]
    with pytest.raises(ConfigError):
        _validate(merged)


def test_extra_args_appended_verbatim_last_and_quoted() -> None:
    command = _harness_integration({"extra_args": ["--foo", "bar baz"]}).resume(_op_ctx(_target(rollout=0)))
    argv = _sh_argv(command, home="/home/me")
    assert argv[-2:] == ["--foo", "bar baz"]  # one token stays one token


def test_extra_args_with_shell_metacharacters_cannot_inject() -> None:
    """``extra_args`` is operator-supplied and NOT name-validated, so an
    adversarial value with quotes/metacharacters must be ``shlex.quote``d
    into one inert argv token, never shell-active."""
    payload = "a'; touch /tmp/pwned #"
    command = _harness_integration({"extra_args": ["-c", payload]}).resume(_op_ctx(_target(rollout=0)))
    argv = _sh_argv(command, home="/home/me")
    assert payload in argv
    assert "touch" not in argv  # not a standalone command word


# -- the returned pane string shape ------------------------------------------


def test_fresh_string_is_a_single_sh_c_that_provisions_then_execs() -> None:
    """A single ``sh -c`` (so it survives the pane's ``exec`` wrapping):
    echo the visible decision, clear any stale recording, provision the
    recorder, then exec codex with NO positional prompt (no
    wrapper-authored turn ever appears in the conversation)."""
    command = _harness_integration(state={}).resume(_op_ctx(_target()))
    assert command.startswith("sh -c ")
    inner = _inner(command)
    assert inner.startswith("echo ")
    assert 'mkdir -p "$HOME"/.agentworks/codex' in inner
    assert 'chmod +x "$HOME"/.agentworks/codex/.record-thread-v1.sh."$$"' in inner
    assert 'rm -f "$HOME"/.agentworks/codex/s1.thread' in inner  # no dead binding survives
    # The exec is last, and there is no positional prompt: the argv is the
    # default-on --strict-config plus the notify override.
    assert _sh_argv(command, home="/home/me")[:2] == ["--strict-config", "-c"]


def test_resume_string_execs_codex_resume_with_the_id_and_the_cwd_pin() -> None:
    command = _harness_integration().resume(_op_ctx(_target(rollout=0)))
    assert command.startswith("sh -c ")
    assert f"exec codex resume {_SID} -c tui.resume_cwd=current" in command
    # The live recording is left alone: only a fresh launch clears it.
    assert 'rm -f "$HOME"/.agentworks/codex/s1.thread' not in _inner(command)


def test_picker_string_execs_bare_codex_resume_with_the_cwd_pin() -> None:
    command = _harness_integration(state={}).resume(_op_ctx(_target(discovered=f"{_ROLLOUT}\n{_OTHER_ROLLOUT}\n")))
    assert "exec codex resume -c tui.resume_cwd=current" in command
    # No id argument: codex's own picker chooses (and neither id leaks in).
    argv = _sh_argv(command, home="/home/me")
    assert argv[1] != _SID
    assert _SID not in argv
    assert _OTHER_SID not in argv


def test_generated_pieces_carry_no_template_var_tokens() -> None:
    """The core template-var substitution raises on unknown ``{{word}}``
    tokens over the WHOLE returned string; the generated skeleton must
    stay clear of doubled braces on every leaf."""
    leaves = (
        _harness_integration(state={}).resume(_op_ctx(_target())),  # fresh
        _harness_integration().resume(_op_ctx(_target(rollout=0))),  # resumed
        _harness_integration(state={}).resume(_op_ctx(_target(discovered=f"{_ROLLOUT}\n", rollout=0))),  # adopted
        _harness_integration(state={}).resume(_op_ctx(_target(discovered=f"{_ROLLOUT}\n{_OTHER_ROLLOUT}\n"))),  # picker
        _harness_integration(state={"session_id": _SID}).resume(_op_ctx(_target(rollout=1))),  # stale
    )
    for command in leaves:
        assert "{{" not in command


def test_session_name_needing_quotes_stays_one_recorder_path_word() -> None:
    """The session name lands in the recorder destination; a name needing
    quoting is ``shlex.quote``d into the path token rather than splitting
    it (session names are validated to a safe charset, so this is defense
    in depth for both the shell word and the TOML string)."""
    command = _harness_integration(state={}, session_name="s one").resume(_op_ctx(_target()))
    assert "\"$HOME\"/'.agentworks/codex/s one.thread'" in _inner(command)
    assert _notify_token(command).endswith('","/home/me/.agentworks/codex/s one.thread"]')


# -- probe semantics, executed through a real sh -----------------------------
#
# The source-and-cwd filter lives in the generated probe command,
# target-side; these tests EXECUTE that command against a scratch
# filesystem (fake $HOME, hand-written rollout fixtures, no codex binary)
# so the filter's behavior is pinned, not just its spelling.


class _ShellTarget:
    """A transport double that runs the probe's inner command through a
    real ``sh`` with ``$HOME`` pointed at a scratch directory. It peels
    the ``"$SHELL" -lic '<inner>'`` wrapper and runs the inner through
    plain ``sh -c`` (no dotfiles, deterministic output)."""

    def __init__(self, home: Path) -> None:
        self._home = home
        self.commands: list[str] = []

    def run(self, command: str, **kwargs: object) -> _FakeResult:
        self.commands.append(command)
        inner = shlex.split(command)[-1]
        proc = subprocess.run(
            ["sh", "-c", inner],
            env={"HOME": str(self._home), "PATH": os.environ["PATH"]},
            capture_output=True,
            text=True,
            check=False,
        )
        return _FakeResult(proc.returncode, stdout=proc.stdout, stderr=proc.stderr)


@pytest.fixture
def codex_home(tmp_path: Path) -> Path:
    """A scratch ``$HOME`` with the codex sessions tree and a workspace
    directory the harness integration under test points at
    (``tmp_path/ws1``)."""
    home = tmp_path / "home"
    (home / ".codex/sessions/2026/08/01").mkdir(parents=True)
    (home / ".agentworks/codex").mkdir(parents=True)
    (tmp_path / "ws1").mkdir()
    return home


def _write_rollout(
    home: Path,
    sid: str,
    cwd: Path | str,
    *,
    source: object = "cli",
    session_id: str | None = None,
    ts: str = "2026-08-01T12-00-00",
) -> Path:
    """A rollout fixture shaped like codex-cli 0.146.0's: the filename
    embeds ``sid`` and the first JSONL line is the ``session_meta`` in
    COMPACT json (the shape the target-side filter matches).

    ``source`` mirrors the discriminator the decisions doc records: the
    string ``"cli"`` for an interactive TUI session, ``"exec"`` for
    ``codex exec``, and a JSON OBJECT for a subagent. ``session_id``
    defaults to ``sid`` but a subagent rollout carries its PARENT's uuid
    there, which is why identity must come from the filename.
    """
    path = home / ".codex/sessions/2026/08/01" / f"rollout-{ts}-{sid}.jsonl"
    meta = {
        "timestamp": ts,
        "type": "session_meta",
        "payload": {
            "session_id": session_id or sid,
            "id": sid,
            "cwd": os.path.realpath(cwd),
            "originator": "codex-tui",
            "source": source,
            "thread_source": "subagent" if isinstance(source, dict) else "user",
        },
    }
    path.write_text(json.dumps(meta, separators=(",", ":")) + "\n")
    return path


def _sh_harness_integration(tmp_path: Path, state: dict[str, object]) -> CodexIntegration:
    return _harness_integration(state=state, workspace_path=str(tmp_path / "ws1"))


def _sh_resume(tmp_path: Path, home: Path, state: dict[str, object]) -> str:
    return _sh_harness_integration(tmp_path, state).resume(_op_ctx(_ShellTarget(home)))  # type: ignore[arg-type]


def test_sh_probe_adopts_ours_and_filters_the_noise_around_it(codex_home: Path, tmp_path: Path) -> None:
    """One interactive rollout in our workspace among a subagent's, an
    exec run's, and a foreign directory's: the filter leaves exactly ours,
    so this adopts rather than degrading to the picker.

    Every exclusion at once, and against a workspace that HAS a real
    candidate, which is what makes the assertion sharp: a filter that
    stops excluding any one of the three would see several candidates and
    degrade to the picker, so the adopted id below is what says all three
    still hold. Excluding each in a workspace of its own could only assert
    the fresh-launch leaf, which is the leaf
    ``test_zero_candidates_launches_fresh`` pins without a real shell.

    The adopted id also says identity comes from the FILENAME: a subagent
    rollout's ``session_meta.session_id`` holds its parent's uuid (verified
    2026-08-04), so the matching rollout below disagrees with its own
    filename on purpose.
    """
    foreign_ws = tmp_path / "elsewhere"
    foreign_ws.mkdir()
    _write_rollout(codex_home, _SID, tmp_path / "ws1", session_id=_OTHER_SID)
    _write_rollout(codex_home, _OTHER_SID, tmp_path / "ws1", source={"subagent": {}}, ts="2026-08-01T12-05-00")
    _write_rollout(codex_home, _THIRD_SID, tmp_path / "ws1", source="exec", ts="2026-08-01T12-06-00")
    _write_rollout(codex_home, _OTHER_SID, foreign_ws, ts="2026-08-01T12-07-00")
    state: dict[str, object] = {}
    _sh_resume(tmp_path, codex_home, state)
    assert state == {"session_id": _SID}


def test_sh_probe_two_interactive_rollouts_open_the_picker(codex_home: Path, tmp_path: Path) -> None:
    _write_rollout(codex_home, _SID, tmp_path / "ws1")
    _write_rollout(codex_home, _OTHER_SID, tmp_path / "ws1", ts="2026-08-01T12-05-00")
    state: dict[str, object] = {}
    command = _sh_resume(tmp_path, codex_home, state)
    assert state == {}
    assert "exec codex resume -c tui.resume_cwd=current" in command


def test_sh_probe_counts_an_unreadable_rollout_as_a_candidate_it_cannot_name(codex_home: Path, tmp_path: Path) -> None:
    """A rollout whose first line will not read is NOT skipped: it is an
    unnamed candidate, so a workspace holding one interactive rollout plus
    one unreadable file goes to the picker instead of confidently adopting
    the readable one. Same reasoning as raising on a failed ``find``: for
    an enumeration, a dropped candidate can turn several into one wrong
    adoption."""
    _write_rollout(codex_home, _SID, tmp_path / "ws1")
    unreadable = _write_rollout(codex_home, _OTHER_SID, tmp_path / "ws1", ts="2026-08-01T12-05-00")
    unreadable.chmod(0o000)
    try:
        state: dict[str, object] = {}
        command = _sh_resume(tmp_path, codex_home, state)
        assert state == {}  # nothing adopted, despite one readable candidate
        assert "exec codex resume -c tui.resume_cwd=current" in command
        assert "could not identify this session's codex conversation" in _echo(command)
    finally:
        unreadable.chmod(0o644)


def test_sh_probe_missing_workspace_dir_raises(codex_home: Path, tmp_path: Path) -> None:
    harness_integration = _harness_integration(state={}, workspace_path=str(tmp_path / "nonexistent"))
    with pytest.raises(StateError):
        harness_integration.resume(_op_ctx(_ShellTarget(codex_home)))  # type: ignore[arg-type]


_PARENT_PAYLOAD = json.dumps(
    {
        "type": "agent-turn-complete",
        "thread-id": _SID,
        "turn-id": "019fcd99-2a49-7991-9cac-d94bdb3077a0",
        "cwd": "/srv/ws1",
        "client": "codex-tui",
        "input-messages": ["hi"],
        "last-assistant-message": "done",
    },
    separators=(",", ":"),
)

# A subagent's completed turn fires the PARENT's notify hook carrying the
# SUBAGENT's thread-id and NO client key (verified 2026-08-04 against
# 0.146.0). Recording it would bind the session to a subagent
# conversation: the exact splice this design exists to prevent.
_SUBAGENT_PAYLOAD = json.dumps(
    {
        "type": "agent-turn-complete",
        "thread-id": _OTHER_SID,
        "turn-id": "019fcd99-2a49-7991-9cac-d94bdb3077a1",
        "cwd": "/srv/ws1",
        "input-messages": ["review this"],
        "last-assistant-message": "looks fine",
    },
    separators=(",", ":"),
)


def _provision(home: Path) -> Path:
    """Run the REAL generated provisioning fragment through ``sh`` and
    return the installed recorder's path, so these tests exercise the
    shipped shell text rather than a copy of the script."""
    command = _harness_integration(state={}).resume(_op_ctx(_target()))
    fragment = _inner(command).split("; exec codex ", 1)[0]
    proc = subprocess.run(
        ["sh", "-c", fragment],
        env={"HOME": str(home), "PATH": os.environ["PATH"]},
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return home / ".agentworks/codex/record-thread-v1.sh"


def _record(recorder: Path, dest: Path, payload: str) -> int:
    proc = subprocess.run([str(recorder), str(dest), payload], capture_output=True, text=True, check=False)
    return proc.returncode


def test_recorder_is_provisioned_executable_and_idempotently(tmp_path: Path) -> None:
    """The pane provisions it before every launch, so an upgrade cannot
    leave an older recorder behind and a second launch must be a no-op
    rather than a conflict."""
    home = tmp_path / "home"
    recorder = _provision(home)
    assert recorder.is_file()
    assert os.access(recorder, os.X_OK)
    first = recorder.read_text()
    recorder.write_text("#!/bin/sh\nexit 1\n")  # a stale recorder from an older release
    assert _provision(home).read_text() == first
    # No staging file outlives provisioning.
    assert [p.name for p in sorted((home / ".agentworks/codex").iterdir())] == ["record-thread-v1.sh"]


def test_recorder_records_a_parent_turns_thread_id(tmp_path: Path) -> None:
    home = tmp_path / "home"
    recorder = _provision(home)
    dest = home / ".agentworks/codex/s1.thread"
    assert _record(recorder, dest, _PARENT_PAYLOAD) == 0
    assert dest.read_text() == f"{_SID}\n"


def test_recorder_ignores_a_subagent_turn(tmp_path: Path) -> None:
    """The whole point of the ``client`` discriminator: a subagent turn
    must not overwrite the parent conversation's binding."""
    home = tmp_path / "home"
    recorder = _provision(home)
    dest = home / ".agentworks/codex/s1.thread"
    _record(recorder, dest, _PARENT_PAYLOAD)
    assert _record(recorder, dest, _SUBAGENT_PAYLOAD) == 0
    assert dest.read_text() == f"{_SID}\n"  # unchanged
    # And with nothing recorded yet, a subagent turn records nothing.
    dest.unlink()
    assert _record(recorder, dest, _SUBAGENT_PAYLOAD) == 0
    assert not dest.exists()


def test_recorder_takes_the_first_thread_id_not_a_later_nested_one(tmp_path: Path) -> None:
    """The extraction takes the FIRST ``thread-id`` field in the payload,
    which on 0.146.0 is the payload's own.

    A greedy ``.*"thread-id":"..."`` pattern silently prefers the LAST
    match, so any structural ``thread-id`` codex nests in a later object
    (it is free to add one; the payload is not a frozen contract) would win
    and bind the session to the wrong conversation. This fixture is that
    shape, and pins first-match.

    Be clear about what it does NOT pin: the rule is byte order, not
    nesting depth. A future codex emitting a nested ``thread-id`` BEFORE
    its own would bind that one, which is why the recorder documents
    re-verification on major bumps. The cost is a recoverable wrong
    binding (a real conversation, visibly wrong in the pane, recovered
    through the picker), which is why byte order is good enough here."""
    home = tmp_path / "home"
    recorder = _provision(home)
    dest = home / ".agentworks/codex/s1.thread"
    nested = json.dumps(
        {
            "type": "agent-turn-complete",
            "thread-id": _SID,
            "client": "codex-tui",
            "meta": {"thread-id": _OTHER_SID, "detail": {"thread-id": _THIRD_SID}},
        },
        separators=(",", ":"),
    )
    assert _record(recorder, dest, nested) == 0
    assert dest.read_text() == f"{_SID}\n"


def test_recorder_last_write_wins(tmp_path: Path) -> None:
    """Deliberate (2026-08-04): a picker-esc fresh conversation rebinds
    the session to the conversation actually in the pane."""
    home = tmp_path / "home"
    recorder = _provision(home)
    dest = home / ".agentworks/codex/s1.thread"
    _record(recorder, dest, _PARENT_PAYLOAD)
    rebound = _PARENT_PAYLOAD.replace(_SID, _THIRD_SID)
    assert _record(recorder, dest, rebound) == 0
    assert dest.read_text() == f"{_THIRD_SID}\n"


@pytest.mark.parametrize(
    "payload",
    [
        "",
        "not json at all",
        '{"type":"agent-turn-complete","client":"codex-tui"}',  # no thread-id
        '{"thread-id":"nope","client":"codex-tui"}',  # thread-id is not a uuid
        # An assistant message quoting the needles cannot forge them: JSON
        # escapes its quotes, so neither the client nor the thread-id
        # pattern matches.
        '{"type":"agent-turn-complete","thread-id":"' + _OTHER_SID + '","last-assistant-message":'
        '"I wrote \\"client\\": and \\"thread-id\\":\\"' + _THIRD_SID + '\\" here"}',
        # The client needle is a POSITIVE match on the interactive TUI, not a
        # presence test, so an exec-client turn is not ours to record.
        '{"type":"agent-turn-complete","thread-id":"' + _OTHER_SID + '","client":"codex_exec"}',
        # ... and neither is a client key codex might someday NEST inside
        # another object, which mere presence would have accepted.
        '{"type":"agent-turn-complete","thread-id":"' + _OTHER_SID + '","detail":{"client":"whatever"}}',
    ],
    ids=[
        "empty",
        "garbage",
        "no-thread-id",
        "bad-uuid",
        "forged-needles",
        "exec-client",
        "nested-client",
    ],
)
def test_recorder_never_breaks_the_turn_and_writes_nothing_on_bad_input(tmp_path: Path, payload: str) -> None:
    """Codex ignores notify failures, but the recorder must not rely on
    that: every path exits 0, and nothing that is not a codex-reported
    uuid is ever written."""
    home = tmp_path / "home"
    recorder = _provision(home)
    dest = home / ".agentworks/codex/s1.thread"
    assert _record(recorder, dest, payload) == 0
    assert not dest.exists()
    assert [p.name for p in sorted((home / ".agentworks/codex").iterdir())] == ["record-thread-v1.sh"]


def test_recorder_writes_atomically_leaving_no_staging_file(tmp_path: Path) -> None:
    """printf-to-temp then ``mv``: a concurrent reader never sees a
    half-written id, and no staging file is left behind either way."""
    home = tmp_path / "home"
    recorder = _provision(home)
    dest = home / ".agentworks/codex/s1.thread"
    _record(recorder, dest, _PARENT_PAYLOAD)
    assert [p.name for p in sorted((home / ".agentworks/codex").iterdir())] == [
        "record-thread-v1.sh",
        "s1.thread",
    ]


def test_recorder_round_trips_into_the_op_decision(tmp_path: Path) -> None:
    """End to end across the two halves: the recorder writes what codex
    reported, and the next op reads that file and resumes it."""
    home = tmp_path / "home"
    (home / ".codex/sessions/2026/08/01").mkdir(parents=True)
    (tmp_path / "ws1").mkdir()
    recorder = _provision(home)
    _record(recorder, home / ".agentworks/codex/s1.thread", _PARENT_PAYLOAD)
    _write_rollout(home, _SID, tmp_path / "ws1")
    state: dict[str, object] = {}
    command = _sh_resume(tmp_path, home, state)
    assert state == {"session_id": _SID}
    assert f"resume {_SID}" in command


def test_recreated_namesake_never_inherits_the_dead_binding(tmp_path: Path) -> None:
    """The delete-and-recreate scenario, end to end on real files, and the
    exact boundary of what create-is-always-fresh buys.

    A session named ``s1`` bound a conversation and was deleted, leaving its
    ``s1.thread`` recording and its rollout on disk (nothing cleans either
    up: an integration has no delete hook, and ``codex delete`` is the
    operator's call). A NEW ``s1`` is then created in the same workspace.

    What is CLOSED: create reads no recording and deletes the one it finds,
    so no op ever resumes the dead uuid as a BOUND id, silently. What is
    NOT closed, and is asserted here so the limit cannot rot into a false
    claim: the dead session's rollout is still the workspace's only
    interactive one, so the first resume adopts it through layer 2. That
    lands on the announced adoption leaf, which names the uuid on both
    operator surfaces, rather than on the silent bound-id resume."""
    home = tmp_path / "home"
    (home / ".codex/sessions/2026/08/01").mkdir(parents=True)
    (tmp_path / "ws1").mkdir()
    recorder = _provision(home)
    thread = home / ".agentworks/codex/s1.thread"
    _record(recorder, thread, _PARENT_PAYLOAD)  # the dead namesake's binding
    _write_rollout(home, _SID, tmp_path / "ws1")  # and its conversation
    assert thread.read_text() == f"{_SID}\n"

    # The recreated session's create: fresh, nothing adopted, and the
    # generated pane command really removes the stale recording.
    state: dict[str, object] = {}
    create = _sh_harness_integration(tmp_path, state).start(_op_ctx(_ShellTarget(home)))  # type: ignore[arg-type]
    assert state == {}
    assert "starting new session s1" in _echo(create)
    fragment = _inner(create).split("; exec codex ", 1)[0]
    subprocess.run(
        ["sh", "-c", fragment],
        env={"HOME": str(home), "PATH": os.environ["PATH"]},
        capture_output=True,
        text=True,
        check=True,
    )
    assert not thread.exists()

    # The next resume sees no recording. It still finds the dead session's
    # rollout by source and cwd, which is layer 2 doing its documented
    # heuristic job, so pin what it must NOT do: resume the dead id as a
    # BOUND one, silently, off a recording create was responsible for
    # clearing. The leaf it reaches instead announces itself as a
    # heuristic adoption, which is the visibility the design promises.
    harness_integration = _sh_harness_integration(tmp_path, {})
    harness_integration.resume(_op_ctx(_ShellTarget(home)))  # type: ignore[arg-type]
    assert harness_integration.launch_note() == (
        f"Identified this session's Codex conversation from Codex's own on-disk state: {_SID}. Resuming..."
    )


# -- readiness probes codex ---------------------------------------------------


def test_readiness_probes_codex() -> None:
    harness_integration = _harness_integration()
    target = _FakeTarget()  # command -v codex -> default ok
    harness_integration.preflight(RunContext(operation_scope=_session_scope(), admin_target=target))
    assert any("command -v codex" in cmd for cmd in target.commands)


def test_readiness_missing_codex_is_a_typed_error() -> None:
    harness_integration = _harness_integration()
    target = _FakeTarget({"command -v codex": _FakeResult(1)})
    ctx = RunContext(operation_scope=_session_scope(), admin_target=target)
    with pytest.raises(StateError):
        harness_integration.preflight(ctx)
