"""The ``codex`` harness: config vocabulary, the resume-vs-launch probe
(both directions), stored-anchor discovery (adopt / fresh / raise, and the
no-anchor no-probe rule), the flag mapping and ``extra_args`` passthrough,
the visible decision, and that readiness probes ``codex``.

Two stub layers, per what each behavior actually lives in:

- ``_FakeTarget`` (exit-code stubs) drives the harness-side forks: which
  probe runs when, how each exit code is classified, what lands in the
  state blob and the pane string.
- ``_ShellTarget`` EXECUTES the generated probe against a real scratch
  filesystem through ``sh``, because discovery's crux (the marker mtime
  bound and the session-cwd filter) is implemented target-side in the
  probe command itself; an exit-code stub cannot pin that a foreign-cwd
  rollout is filtered out rather than adopted. No real ``codex`` binary
  anywhere; the rollout files are hand-written fixtures.

See ``codex-harness-decisions.md`` for the pinned mechanics these mirror.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import time
from typing import TYPE_CHECKING

import pytest

from agentworks.capabilities.base import OperationScope, RunContext, ScopeLevel
from agentworks.errors import ConfigError, StateError
from agentworks.plugins.codex.harness import CodexHarness
from tests.conftest import _FakeResult, _FakeTarget

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

_SID = "939b1597-7c61-4ace-80f4-14617b7b4257"  # a fixed stored uuid
_OTHER_SID = "11111111-2222-4333-8444-555555555555"
_ROLLOUT = f"/home/me/.codex/sessions/2026/08/01/rollout-2026-08-01T12-00-00-{_SID}.jsonl"
_OTHER_ROLLOUT = f"/home/me/.codex/sessions/2026/08/01/rollout-2026-08-01T12-05-00-{_OTHER_SID}.jsonl"

# A stored discovery anchor, as a fresh launch would have minted it.
_ANCHOR = ".agentworks/codex/s1-deadbeefdeadbeefdeadbeefdeadbeef.launch"

# Substring keys for the two op-time probes: the rollout-presence probe
# carries the stored id's glob; the discovery probe carries the marker path.
_ROLLOUT_PROBE = f"*-{_SID}.jsonl"
_DISCOVERY_PROBE = ".launch"


def _harness(
    config: Mapping[str, object] | None = None,
    *,
    session_name: str = "s1",
    workspace_path: str = "/srv/ws1",
    state: dict[str, object] | None = None,
    admin: bool = True,
) -> CodexHarness:
    return CodexHarness(
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


def test_dependencies_imply_no_reference() -> None:
    """``codex`` implies no edge, and ``dependencies`` is total: it
    returns ``()`` for the known fields and even for a malformed blob."""
    assert (
        CodexHarness.dependencies(
            "session-template/codex",
            {"model": "gpt-5", "sandbox": "workspace-write", "extra_args": ["--foo"]},
        )
        == ()
    )
    # Never raises, even on config that ``validate`` would reject.
    assert CodexHarness.dependencies("session-template/codex", {"model": 3, "sandbx": "typo"}) == ()


def test_validate_accepts_the_five_fields_and_empty_config() -> None:
    assert (
        CodexHarness.validate(
            "session-template/codex",
            {
                "model": "gpt-5",
                "sandbox": "workspace-write",
                "approval_policy": "on-request",
                "profile": "work",
                "extra_args": ["--foo"],
            },
        )
        is None
    )
    assert CodexHarness.validate("session-template/codex", {}) is None


def test_validate_rejects_unknown_field() -> None:
    with pytest.raises(ConfigError, match="unknown codex harness field"):
        CodexHarness.validate("session-template/codex", {"sandbx": "typo"})


@pytest.mark.parametrize("field_name", ["model", "sandbox", "approval_policy", "profile"])
def test_validate_rejects_non_string_flag_fields(field_name: str) -> None:
    with pytest.raises(ConfigError, match=f"{field_name} must be a string"):
        CodexHarness.validate("session-template/codex", {field_name: 3})


def test_validate_rejects_non_list_extra_args() -> None:
    with pytest.raises(ConfigError, match="extra_args must be a list of strings"):
        CodexHarness.validate("session-template/codex", {"extra_args": "just-a-string"})


def test_construct_revalidates_config() -> None:
    with pytest.raises(ConfigError, match="unknown codex harness field"):
        _harness({"nope": 1})


# -- the stored-id probe: present -> resume, absent -> launch fresh ----------


def test_present_rollout_resumes() -> None:
    target = _FakeTarget({_ROLLOUT_PROBE: _FakeResult(0)})  # found
    command = _harness().start(_op_ctx(target))
    assert f"resume {_SID}" in command
    assert "tui.resume_cwd=current" in command
    assert "resuming session s1" in command
    assert ".launch" not in command  # no marker business on the plain resume path


def test_absent_rollout_launches_fresh_and_drops_the_stale_id() -> None:
    """An archived or deleted rollout is not resumable (the pinned
    archived policy): the harness launches fresh with the fourth visible
    decision (stale, not plain fresh) and DROPS the stale id, so the next
    op's discovery can adopt the codex-minted replacement."""
    state: dict[str, object] = {"session_id": _SID}
    target = _FakeTarget({_ROLLOUT_PROBE: _FakeResult(1)})  # not found
    harness = _harness(state=state)
    command = harness.start(_op_ctx(target))
    assert "resume" not in command
    assert "archived or gone; starting new session s1" in command
    assert "touch" in command and ".launch" in command
    assert "session_id" not in state  # stale id dropped for rediscovery
    assert isinstance(state.get("discovery_marker"), str)  # the new anchor is stored


def test_probe_that_could_not_execute_raises_rather_than_guessing() -> None:
    """A non-{0,1} exit (an SSH failure's 255, a shell that could not
    start) means the probe never ran. Guessing "fresh" would drop the
    stored id and orphan a resumable conversation; the op raises a typed
    error naming the target instead."""
    target = _FakeTarget({_ROLLOUT_PROBE: _FakeResult(255)})
    with pytest.raises(StateError, match="could not probe") as exc:
        _harness().start(_op_ctx(target))
    assert "exit 255" in str(exc.value)
    assert exc.value.entity_name == "s1"


def test_probe_keeps_find_failure_distinct_from_a_clean_no_match() -> None:
    """The inner command's structure: a missing sessions dir short-circuits
    to the clean no-match exit (1, fresh), while a find that FAILED without
    printing a match exits 6, which the exit-code fork raises on rather
    than folding into "no rollout"."""
    target = _FakeTarget({_ROLLOUT_PROBE: _FakeResult(0)})
    _harness().start(_op_ctx(target))
    (probe_cmd,) = target.commands
    assert "[ -d " in probe_cmd  # dir-missing is a clean no-match, not a find failure
    assert "exit 6" in probe_cmd  # find failure stays distinguishable
    failing = _FakeTarget({_ROLLOUT_PROBE: _FakeResult(6)})
    with pytest.raises(StateError, match="could not probe"):
        _harness().start(_op_ctx(failing))


def test_missing_launch_target_raises_rather_than_guessing() -> None:
    """No launch target means no probe can run; unlike claude-code (whose
    fresh launch keeps its minted id), a codex fresh launch drops state
    and replaces the discovery anchor, so the op refuses to guess."""
    with pytest.raises(StateError, match="no launch target"):
        _harness().start(RunContext())


def test_probe_is_rooted_at_codex_home_and_finds_by_stored_id() -> None:
    target = _FakeTarget({_ROLLOUT_PROBE: _FakeResult(0)})
    _harness().start(_op_ctx(target))
    (probe_cmd,) = target.commands
    assert f"*-{_SID}.jsonl" in probe_cmd
    assert "find" in probe_cmd
    # Rooted at the CLI's state dir with its documented override.
    assert "CODEX_HOME" in probe_cmd


def test_start_and_restart_are_symmetric() -> None:
    target = _FakeTarget({_ROLLOUT_PROBE: _FakeResult(0)})
    harness = _harness()
    assert harness.start(_op_ctx(target)) == harness.restart(_op_ctx(target))


# -- discovery: anchored on the STORED marker only ----------------------------


def test_brand_new_session_performs_no_discovery() -> None:
    """The anti-namesake rule: a blob with no stored anchor has
    definitively nothing to discover, so the op makes NO transport call at
    all and launches fresh. A session recreated under a deleted session's
    name can therefore never adopt the dead session's conversation off a
    leftover marker file."""
    state: dict[str, object] = {}
    target = _FakeTarget()
    command = _harness(state=state).start(_op_ctx(target))
    assert target.commands == []  # no probe ran
    assert "starting new session s1" in command
    assert "session_id" not in state
    assert isinstance(state.get("discovery_marker"), str)  # the anchor for NEXT time


def test_fresh_launch_mints_a_nonce_anchor_and_stores_it() -> None:
    """The minted anchor is per-launch (a nonce, not just the session
    name) and the pane command touches exactly the stored path."""
    state: dict[str, object] = {}
    command = _harness(state=state).start(_op_ctx(_FakeTarget()))
    anchor = state["discovery_marker"]
    assert isinstance(anchor, str)
    assert anchor.startswith(".agentworks/codex/s1-")
    assert anchor.endswith(".launch")
    assert len(anchor) > len(".agentworks/codex/s1-.launch")  # a real nonce
    inner = shlex.split(command)[2]
    assert f'touch "$HOME"/{anchor}' in inner
    # A second harness's fresh launch mints a DIFFERENT anchor.
    other: dict[str, object] = {}
    _harness(state=other).start(_op_ctx(_FakeTarget()))
    assert other["discovery_marker"] != anchor


def test_fresh_launch_removes_the_previous_anchors_marker() -> None:
    """A prior fresh launch that never got used left its marker file; the
    next fresh launch replaces the anchor and the pane removes the old
    file so no dead marker accumulates."""
    state: dict[str, object] = {"discovery_marker": _ANCHOR}
    target = _FakeTarget({_DISCOVERY_PROBE: _FakeResult(0, stdout="")})  # zero candidates
    command = _harness(state=state).start(_op_ctx(target))
    inner = shlex.split(command)[2]
    assert f'rm -f "$HOME"/{_ANCHOR}' in inner
    new_anchor = state["discovery_marker"]
    assert isinstance(new_anchor, str) and new_anchor != _ANCHOR
    assert f'touch "$HOME"/{new_anchor}' in inner


def test_discovery_adopts_a_single_candidate_and_resumes_it() -> None:
    """One rollout newer than the stored marker (already cwd-filtered
    target-side): its uuid is adopted into the state blob (the manager
    persists it after the op), the consumed anchor is cleared, and the
    resume pane command removes the marker file."""
    state: dict[str, object] = {"discovery_marker": _ANCHOR}
    target = _FakeTarget({_DISCOVERY_PROBE: _FakeResult(0, stdout=f"{_ROLLOUT}\n")})
    harness = _harness(state=state)
    command = harness.start(_op_ctx(target))
    assert state == {"session_id": _SID}  # adopted; anchor consumed
    assert harness.state == {"session_id": _SID}  # in place, via the property
    assert f"resume {_SID}" in command
    assert "adopted a discovered codex session" in command
    inner = shlex.split(command)[2]
    assert f'rm -f "$HOME"/{_ANCHOR}' in inner  # the consumed marker file goes too


def test_discovery_zero_candidates_launches_fresh() -> None:
    """Marker present but no matching rollout newer than it: the human
    never durably used the previous fresh launch in this workspace, so
    launching fresh again loses nothing (with a replacement anchor)."""
    state: dict[str, object] = {"discovery_marker": _ANCHOR}
    target = _FakeTarget({_DISCOVERY_PROBE: _FakeResult(0, stdout="")})
    command = _harness(state=state).start(_op_ctx(target))
    assert "session_id" not in state
    assert "starting new session s1" in command
    assert state["discovery_marker"] != _ANCHOR  # replaced, not reused


def test_discovery_without_a_sessions_dir_launches_fresh() -> None:
    """The probe's one definitive-fresh exit (3): codex never ran on this
    target, so there is nothing to adopt."""
    state: dict[str, object] = {"discovery_marker": _ANCHOR}
    target = _FakeTarget({_DISCOVERY_PROBE: _FakeResult(3)})
    command = _harness(state=state).start(_op_ctx(target))
    assert "session_id" not in state
    assert "starting new session s1" in command


def test_discovery_with_a_missing_marker_file_raises() -> None:
    """An anchor stored but its file gone (the fresh pane never ran, or
    someone removed it): the anchor's account of history is broken, so
    the op raises with the recovery in the hint instead of guessing
    fresh over a possibly-undiscovered conversation."""
    state: dict[str, object] = {"discovery_marker": _ANCHOR}
    target = _FakeTarget({_DISCOVERY_PROBE: _FakeResult(4)})
    with pytest.raises(StateError, match="launch marker .* is missing") as exc:
        _harness(state=state).start(_op_ctx(target))
    assert f"touch ~/{_ANCHOR}" in (exc.value.hint or "")
    assert state == {"discovery_marker": _ANCHOR}  # nothing adopted, anchor kept


def test_discovery_with_an_unresolvable_workspace_raises() -> None:
    state: dict[str, object] = {"discovery_marker": _ANCHOR}
    target = _FakeTarget({_DISCOVERY_PROBE: _FakeResult(5)})
    with pytest.raises(StateError, match="could not resolve the workspace directory"):
        _harness(state=state).start(_op_ctx(target))


def test_discovery_multiple_candidates_raises_naming_the_ids() -> None:
    """Two matching rollouts newer than the marker: adopting the wrong id
    would silently splice one session's conversation into another, so the
    op raises a typed error naming both candidates."""
    state: dict[str, object] = {"discovery_marker": _ANCHOR}
    target = _FakeTarget({_DISCOVERY_PROBE: _FakeResult(0, stdout=f"{_ROLLOUT}\n{_OTHER_ROLLOUT}\n")})
    with pytest.raises(StateError, match="refusing to guess which one") as exc:
        _harness(state=state).start(_op_ctx(target))
    assert _SID in str(exc.value)
    assert _OTHER_SID in str(exc.value)
    assert state == {"discovery_marker": _ANCHOR}  # nothing adopted


def test_discovery_probe_failure_raises_rather_than_guessing() -> None:
    """A probe that FAILED (a find error's 6, an SSH failure's 255) is not
    a probe that found nothing: guessing "fresh" would replace the anchor
    over an undiscovered rollout and orphan it."""
    for code in (6, 255):
        target = _FakeTarget({_DISCOVERY_PROBE: _FakeResult(code)})
        with pytest.raises(StateError, match="could not probe"):
            _harness(state={"discovery_marker": _ANCHOR}).start(_op_ctx(target))


def test_discovery_ignores_login_shell_noise_lines() -> None:
    """The probe runs through a login shell whose dotfiles may echo;
    stdout lines that are not shaped like a rollout path are ignored, so
    noise cannot misdiagnose the probe as a no-uuid error or a phantom
    candidate."""
    state: dict[str, object] = {"discovery_marker": _ANCHOR}
    noisy = f"Welcome to devbox!\n{_ROLLOUT}\nmise activated\n"
    target = _FakeTarget({_DISCOVERY_PROBE: _FakeResult(0, stdout=noisy)})
    command = _harness(state=state).start(_op_ctx(target))
    assert state["session_id"] == _SID  # the one real line was adopted
    assert f"resume {_SID}" in command


def test_discovery_unrecognized_rollout_name_raises() -> None:
    """A rollout-SHAPED line without an embedded uuid cannot be adopted
    OR safely ignored; the op refuses to guess what it is."""
    weird = "/home/me/.codex/sessions/2026/08/01/rollout-weird.jsonl"
    target = _FakeTarget({_DISCOVERY_PROBE: _FakeResult(0, stdout=f"{weird}\n")})
    with pytest.raises(StateError, match="does not embed a session id"):
        _harness(state={"discovery_marker": _ANCHOR}).start(_op_ctx(target))


def test_adopted_id_is_resumed_verbatim_on_the_next_op() -> None:
    """The adoption round trip: an id discovered on one op is read back
    from the state blob and resumed directly on the next."""
    state: dict[str, object] = {"discovery_marker": _ANCHOR}
    discover = _FakeTarget({_DISCOVERY_PROBE: _FakeResult(0, stdout=f"{_ROLLOUT}\n")})
    harness = _harness(state=state)
    harness.start(_op_ctx(discover))
    assert state == {"session_id": _SID}

    resume = _FakeTarget({_ROLLOUT_PROBE: _FakeResult(0)})
    command = _harness(state=state).restart(_op_ctx(resume))
    assert f"resume {_SID}" in command
    assert "resuming session s1" in command
    # The second op probed the stored id; it did NOT re-run discovery.
    assert all(".launch" not in cmd for cmd in resume.commands)


def test_wrong_typed_stored_id_is_swept_and_treated_as_absent() -> None:
    """A malformed stored value (the blob is only as trustworthy as the
    DB it came from) is swept out of the namespace, not merely skipped:
    with no anchor either, the op is a plain no-probe fresh launch."""
    state: dict[str, object] = {"session_id": 7}
    target = _FakeTarget()
    command = _harness(state=state).start(_op_ctx(target))
    assert "starting new session s1" in command
    assert target.commands == []  # no anchor: no probe at all
    assert "session_id" not in state  # the garbage was swept, not left behind


# -- discovery probe semantics, executed through a real sh --------------------
#
# The marker mtime bound and the session-cwd filter live in the generated
# probe command, target-side; these tests EXECUTE that command against a
# scratch filesystem (fake $HOME, hand-written rollout fixtures, no codex
# binary) so the filter's behavior is pinned, not just its spelling.


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
    """A scratch ``$HOME`` with the codex sessions tree, the marker dir
    (with ``_ANCHOR``'s file present and back-dated), and a workspace
    directory the harness under test points at (``tmp_path/ws1``)."""
    home = tmp_path / "home"
    (home / ".codex/sessions/2026/08/01").mkdir(parents=True)
    (home / ".agentworks/codex").mkdir(parents=True)
    (tmp_path / "ws1").mkdir()
    marker = home / _ANCHOR
    marker.touch()
    past = time.time() - 100
    os.utime(marker, (past, past))
    return home


def _write_rollout(home: Path, sid: str, cwd: Path, *, ts: str = "2026-08-01T12-00-00") -> Path:
    """A minimal rollout fixture: the filename embeds ``sid`` and the
    first JSONL line carries session_meta with ``cwd`` (compact JSON, the
    shape the grep filter matches)."""
    path = home / ".codex/sessions/2026/08/01" / f"rollout-{ts}-{sid}.jsonl"
    meta = f'{{"timestamp":"{ts}","type":"session_meta","payload":{{"id":"{sid}","cwd":"{os.path.realpath(cwd)}"}}}}'
    path.write_text(meta + "\n")
    return path


def _sh_harness(tmp_path: Path, state: dict[str, object]) -> CodexHarness:
    return _harness(state=state, workspace_path=str(tmp_path / "ws1"))


def test_sh_probe_adopts_the_rollout_recorded_in_our_workspace(codex_home: Path, tmp_path: Path) -> None:
    _write_rollout(codex_home, _SID, tmp_path / "ws1")
    state: dict[str, object] = {"discovery_marker": _ANCHOR}
    command = _sh_harness(tmp_path, state).start(_op_ctx(_ShellTarget(codex_home)))  # type: ignore[arg-type]
    assert state["session_id"] == _SID
    assert f"resume {_SID}" in command


def test_sh_probe_does_not_adopt_a_foreign_workspaces_rollout(codex_home: Path, tmp_path: Path) -> None:
    """The foreign-adoption regression pin: exactly ONE rollout is newer
    than our marker, but it was recorded in a DIFFERENT directory (another
    session of the same launch user), so the cwd filter excludes it and
    the op launches fresh instead of splicing that conversation into this
    session. Under marker-count-only discovery this candidate would have
    been adopted."""
    foreign_ws = tmp_path / "elsewhere"
    foreign_ws.mkdir()
    _write_rollout(codex_home, _OTHER_SID, foreign_ws)
    state: dict[str, object] = {"discovery_marker": _ANCHOR}
    command = _sh_harness(tmp_path, state).start(_op_ctx(_ShellTarget(codex_home)))  # type: ignore[arg-type]
    assert "session_id" not in state  # NOT adopted
    assert "starting new session s1" in command


def test_sh_probe_adopts_ours_and_filters_the_foreign_one(codex_home: Path, tmp_path: Path) -> None:
    """Ours and a foreign-cwd rollout both newer than the marker: the
    filter leaves exactly ours, so this adopts instead of raising a
    spurious multiple-candidates error."""
    foreign_ws = tmp_path / "elsewhere"
    foreign_ws.mkdir()
    _write_rollout(codex_home, _SID, tmp_path / "ws1")
    _write_rollout(codex_home, _OTHER_SID, foreign_ws, ts="2026-08-01T12-05-00")
    state: dict[str, object] = {"discovery_marker": _ANCHOR}
    _sh_harness(tmp_path, state).start(_op_ctx(_ShellTarget(codex_home)))  # type: ignore[arg-type]
    assert state["session_id"] == _SID


def test_sh_probe_two_matching_rollouts_raise(codex_home: Path, tmp_path: Path) -> None:
    _write_rollout(codex_home, _SID, tmp_path / "ws1")
    _write_rollout(codex_home, _OTHER_SID, tmp_path / "ws1", ts="2026-08-01T12-05-00")
    state: dict[str, object] = {"discovery_marker": _ANCHOR}
    with pytest.raises(StateError, match="refusing to guess which one"):
        _sh_harness(tmp_path, state).start(_op_ctx(_ShellTarget(codex_home)))  # type: ignore[arg-type]


def test_sh_probe_ignores_rollouts_older_than_the_marker(codex_home: Path, tmp_path: Path) -> None:
    rollout = _write_rollout(codex_home, _SID, tmp_path / "ws1")
    ancient = time.time() - 1000  # older than the back-dated marker
    os.utime(rollout, (ancient, ancient))
    state: dict[str, object] = {"discovery_marker": _ANCHOR}
    command = _sh_harness(tmp_path, state).start(_op_ctx(_ShellTarget(codex_home)))  # type: ignore[arg-type]
    assert "session_id" not in state
    assert "starting new session s1" in command


def test_sh_probe_missing_marker_file_raises(codex_home: Path, tmp_path: Path) -> None:
    (codex_home / _ANCHOR).unlink()
    state: dict[str, object] = {"discovery_marker": _ANCHOR}
    with pytest.raises(StateError, match="launch marker .* is missing"):
        _sh_harness(tmp_path, state).start(_op_ctx(_ShellTarget(codex_home)))  # type: ignore[arg-type]


def test_sh_probe_missing_workspace_dir_raises(codex_home: Path, tmp_path: Path) -> None:
    state: dict[str, object] = {"discovery_marker": _ANCHOR}
    harness = _harness(state=state, workspace_path=str(tmp_path / "nonexistent"))
    with pytest.raises(StateError, match="could not resolve the workspace directory"):
        harness.start(_op_ctx(_ShellTarget(codex_home)))  # type: ignore[arg-type]


def test_sh_rollout_probe_forks_on_a_real_filesystem(codex_home: Path, tmp_path: Path) -> None:
    """The stored-id existence probe through the same real-sh layer:
    present resumes, absent launches fresh via the stale path."""
    _write_rollout(codex_home, _SID, tmp_path / "ws1")
    present = _sh_harness(tmp_path, {"session_id": _SID})
    assert f"resume {_SID}" in present.start(_op_ctx(_ShellTarget(codex_home)))  # type: ignore[arg-type]

    absent = _sh_harness(tmp_path, {"session_id": _OTHER_SID})
    assert "archived or gone" in absent.start(_op_ctx(_ShellTarget(codex_home)))  # type: ignore[arg-type]


# -- launch_note: the four decisions ------------------------------------------


def test_launch_note_reports_resume() -> None:
    target = _FakeTarget({_ROLLOUT_PROBE: _FakeResult(0)})
    harness = _harness()
    assert harness.launch_note() is None  # nothing decided before the op
    harness.start(_op_ctx(target))
    assert harness.launch_note() == "Existing Codex session found. Resuming..."


def test_launch_note_reports_fresh_start() -> None:
    harness = _harness(state={})
    harness.start(_op_ctx(_FakeTarget()))
    assert harness.launch_note() == "No existing Codex session. Starting a new one..."


def test_launch_note_reports_adoption() -> None:
    target = _FakeTarget({_DISCOVERY_PROBE: _FakeResult(0, stdout=f"{_ROLLOUT}\n")})
    harness = _harness(state={"discovery_marker": _ANCHOR})
    harness.start(_op_ctx(target))
    assert harness.launch_note() == ("Discovered the Codex session from the previous launch. Adopting and resuming...")


def test_launch_note_reports_the_stale_id_drop() -> None:
    target = _FakeTarget({_ROLLOUT_PROBE: _FakeResult(1)})
    harness = _harness(state={"session_id": _SID})
    harness.start(_op_ctx(target))
    assert harness.launch_note() == ("Previous Codex session is archived or gone. Starting a new one...")


# -- the managed flags and extra_args ----------------------------------------


def test_config_fields_map_to_their_short_flags() -> None:
    target = _FakeTarget({_ROLLOUT_PROBE: _FakeResult(0)})
    command = _harness(
        {
            "model": "gpt-5",
            "sandbox": "workspace-write",
            "approval_policy": "on-request",
            "profile": "work",
        }
    ).start(_op_ctx(target))
    assert "-m gpt-5" in command
    assert "-s workspace-write" in command
    assert "-a on-request" in command
    assert "-p work" in command


def test_extra_args_appended_verbatim_last_and_quoted() -> None:
    target = _FakeTarget({_ROLLOUT_PROBE: _FakeResult(0)})
    command = _harness({"model": "gpt-5", "extra_args": ["--foo", "bar baz"]}).start(_op_ctx(target))
    # One argv token stays one token: "bar baz" is quoted, not re-split.
    assert shlex.quote("bar baz") in command
    # Appended last: after the managed -m flag.
    assert command.index("-m gpt-5") < command.index("--foo")


def test_extra_args_with_shell_metacharacters_cannot_inject() -> None:
    """``extra_args`` is operator-supplied and NOT name-validated, so an
    adversarial value with quotes/metacharacters must be ``shlex.quote``d
    into one inert argv token, never shell-active."""
    payload = "a'; touch /tmp/pwned #"
    target = _FakeTarget({_ROLLOUT_PROBE: _FakeResult(0)})
    command = _harness({"extra_args": ["-c", payload]}).start(_op_ctx(target))

    # The command is `sh -c '<inner>'`; the payload is nested-quoted (once
    # into the argv, once into the sh -c wrapper). Peeling both quoting
    # layers back with shlex must yield the payload as exactly ONE inert
    # token, never a `touch` command the outer shell would run.
    outer = shlex.split(command)
    assert outer[:2] == ["sh", "-c"]
    inner_tokens = shlex.split(outer[2])
    assert payload in inner_tokens
    assert "touch" not in inner_tokens  # not a standalone command word


# -- the returned pane string shape ------------------------------------------


def test_fresh_string_is_a_single_sh_c_that_echoes_touches_then_execs() -> None:
    """A single ``sh -c`` (so it survives the pane's ``exec`` wrapping):
    echo the visible decision, touch the minted discovery marker, then
    exec codex with NO positional prompt (no wrapper-authored turn ever
    appears in the conversation)."""
    state: dict[str, object] = {}
    command = _harness(state=state).start(_op_ctx(_FakeTarget()))
    assert command.startswith("sh -c ")
    assert "echo " in command
    inner = shlex.split(command)[2]
    assert 'mkdir -p "$HOME"/.agentworks/codex' in inner
    assert f'touch "$HOME"/{state["discovery_marker"]}' in inner
    assert inner.endswith("exec codex")  # no prompt, no trailing tokens


def test_resume_string_is_a_single_sh_c_that_echoes_then_execs() -> None:
    target = _FakeTarget({_ROLLOUT_PROBE: _FakeResult(0)})
    command = _harness().start(_op_ctx(target))
    assert command.startswith("sh -c ")
    assert "echo " in command
    assert f"exec codex resume {_SID} -c tui.resume_cwd=current" in command
    assert ".launch" not in command  # markers are a fresh/adoption concern


def test_generated_pieces_carry_no_template_var_tokens() -> None:
    """The core template-var substitution raises on unknown ``{{word}}``
    tokens over the WHOLE returned string; the generated skeleton must
    stay clear of doubled braces on every path."""
    resume = _FakeTarget({_ROLLOUT_PROBE: _FakeResult(0)})
    adopt = _FakeTarget({_DISCOVERY_PROBE: _FakeResult(0, stdout=f"{_ROLLOUT}\n")})
    stale = _FakeTarget({_ROLLOUT_PROBE: _FakeResult(1)})
    assert "{{" not in _harness(state={}).start(_op_ctx(_FakeTarget()))
    assert "{{" not in _harness().start(_op_ctx(resume))
    assert "{{" not in _harness(state={"discovery_marker": _ANCHOR}).start(_op_ctx(adopt))
    assert "{{" not in _harness(state={"session_id": _SID}).start(_op_ctx(stale))


def test_session_name_needing_quotes_stays_one_marker_path_word() -> None:
    """The session name lands in the marker path; a name needing quoting
    is ``shlex.quote``d into the path token rather than splitting it."""
    state: dict[str, object] = {}
    command = _harness(state=state, session_name="s one").start(_op_ctx(_FakeTarget()))
    inner = shlex.split(command)[2]
    anchor = state["discovery_marker"]
    assert isinstance(anchor, str)
    assert f"touch \"$HOME\"/'{anchor}'" in inner


# -- readiness probes codex ---------------------------------------------------


def test_readiness_probes_codex() -> None:
    harness = _harness()
    target = _FakeTarget()  # command -v codex -> default ok
    harness.preflight(RunContext(operation_scope=_session_scope(), admin_target=target))
    assert any("command -v codex" in cmd for cmd in target.commands)


def test_readiness_missing_codex_is_a_typed_error() -> None:
    harness = _harness()
    target = _FakeTarget({"command -v codex": _FakeResult(1)})
    ctx = RunContext(operation_scope=_session_scope(), admin_target=target)
    with pytest.raises(StateError, match="'codex' harness.*requires 'codex'"):
        harness.preflight(ctx)
