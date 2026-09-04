"""The ``codex`` harness integration: run Codex as the session workload, resuming its
rollout when one exists and launching fresh otherwise.

Config vocabulary (all optional): ``goal``, ``initial_prompt``, and ``agent``
seed a fresh conversation; ``developer_instructions`` configures every Codex
process launch, including explicit resume and the picker;
``model``, ``sandbox``, ``approval_policy``,
and ``profile`` map to the ``-m`` / ``-s`` / ``-a`` / ``-p`` flags verbatim;
``network`` (bool) forwards to the ``sandbox_workspace_write.network_access``
config key via ``-c``; ``approvals_reviewer`` (str) forwards to the
``approvals_reviewer`` config key via ``-c`` (who adjudicates approval
escalations: codex documents ``user``, the default, and ``auto_review``, its
risk-based reviewer subagent); ``reasoning_effort`` (str) and ``vim_mode``
(bool) forward to ``model_reasoning_effort`` and ``tui.vim_mode_default`` via
``-c``; ``writable_dirs`` (list) emits one ``--add-dir`` per entry
(union-merged across template inheritance, like ``shell``'s
``required_commands``); ``web_search`` accepts a Codex-owned mode string via
``-c``, while legacy ``true`` keeps emitting ``--search`` and ``false`` keeps
emitting no override;
``disable_strict_config`` (bool, default false) suppresses the
``--strict-config`` the harness integration otherwise always emits; and ``extra_args`` is
a list of raw argv tokens emitted after generated options (the operator escape hatch for any
flag the harness integration does not model). ``extra_args`` lands after the
generated ``-c notify`` override, so an operator who sets their own ``notify``
there deliberately disables the id binding below (documented on the field, and
the fallback layers still hold). The integration contract and worked-example guidance
live in ``agentworks/capabilities/harness_integration/README.md``; this module keeps the
Codex-specific command and state invariants next to their implementation.

Addressing is discover-and-store (the harness integration guide's rule 1, second form):
codex offers no ``--session-id`` analog, so the harness integration never mints an id.
Instead it stores the codex-minted session uuid in its state namespace under
``session_id``, and gets that uuid from codex ITSELF through three layers
(pinned 2026-08-04 in the decisions doc, after a production incident in which
inference over every rollout in the workspace directory produced 14
indistinguishable candidates and a bricked resume: codex SUBAGENTS write
sibling rollouts with the same cwd as their parent):

1. **The notify binding (primary).** Every generated launch provisions a
   recorder script under ``~/.agentworks/codex/`` and passes
   ``-c notify=[<recorder>, <destination>]``. Codex runs the recorder after
   every completed turn, handing it the turn's ``thread-id``; the recorder
   writes that uuid to ``~/.agentworks/codex/<session-name>.thread``. The next
   op reads the file and adopts the uuid, so resume is deterministic rather
   than inferred. Last-write-wins on purpose: a picker-esc fresh conversation
   rebinds the session to the conversation actually in the pane. The recorder
   itself, and every target-side path around it, lives in ``recorder.py``.
2. **Source-filtered discovery (the fallback).** Reached whenever no id is
   bound: nothing recorded and nothing stored, OR a bound id just dropped
   because its rollout was archived or deleted. Candidates are rollouts
   under the sessions tree whose ``session_meta`` line carries BOTH
   ``"source":"cli"`` and this session's canonicalized workspace cwd. A
   subagent's ``source`` is a JSON object and an ``exec`` session's is
   ``"exec"``, so both are structurally excluded, matching what codex's own
   picker shows by default. Exactly one candidate is adopted.
3. **The picker (ambiguity is a human decision, not an error).** Several
   candidates launch bare ``codex resume``, codex's own cwd-scoped picker,
   with the recorder attached: the operator picks their conversation (or esc
   for a fresh one) and the next completed turn binds the id through layer 1.

**Only an ordinary start can adopt an id; ``create`` is always fresh.** Both
identity channels above are name-derived on the launch target (the recorder
file is ``<session-name>.thread``, and discovery matches the workspace
directory), so at create time either would hand a brand-new session the
conversation of a deleted predecessor that shared its name or its workspace. A
create means a brand-new session row, which by definition owns no codex
conversation yet, so forced-fresh start reads only the recorder identity it
must reject and clears that recording in the launch command. That closes the recorder channel outright: a recreated
namesake can never resume the dead conversation as a BOUND id. It NARROWS but
does not close layer 2, whose candidate set is the workspace: if the dead
session's rollout is the only interactive one there, the first later start can still
adopt it, announced (see ``start`` for why that is tolerable and what closing
it would cost). The split is the correct semantics for deferred discovery
rather than an asymmetry to work around.

Resume-vs-launch for a bound id is an op-time existence probe for the id's
rollout file on disk: the rollout-file boundary was empirically confirmed to
equal codex's own resume boundary. An archived rollout (moved to
``archived_sessions/`` by ``codex archive``) is deliberately treated as
not-resumable: auto-unarchiving would silently reverse an explicit operator
action. Not-resumable means the bound id is DROPPED and layers 2/3 decide, not
that the session starts fresh: the next op can adopt a different conversation
in the same workspace, or open the picker, and only a workspace with no
candidate launches fresh. The archived history stays recoverable manually
(``codex unarchive``), and the leaf reports the drop alongside whatever
replaced it.

Every layer degrades into the next instead of failing: a recorder that never
runs (codex ignores a missing notify program silently) costs determinism, not
correctness, and an over-strict layer-2 filter degrades to the picker.

The wrong-adoption path that remains is layer 2 having exactly one candidate
that is not this session's conversation, because layer 2's whole filter is the
workspace directory. Two ways in, and the ORDINARY one matters more than the
exotic one: a session deleted and replaced in the same workspace (its rollout
outlives it), or a foreign interactive codex session the same user launched
manually there. Either way it surfaces in the pane as a visibly wrong
conversation rather than silently (the adoption leaf echoes the uuid it chose
on both operator surfaces), the session has typed nothing yet so nothing of its
own is lost, and the picker plus notify rebinding recover it.
"""

from __future__ import annotations

import json
import re
import shlex
from typing import TYPE_CHECKING, Annotated, ClassVar, Literal, NamedTuple

from pydantic import Field

from agentworks.capabilities.harness_integration.base import (
    HarnessIntegration,
    HarnessLaunchIntent,
    HarnessStart,
    quote_literal_argv,
    require_commands,
)
from agentworks.errors import StateError
from agentworks.plugins.codex.recorder import home_word, notify_value_word, provision_fragment, thread_tail
from agentworks.schema import AgwModel, MergeStrategy
from agentworks.topics import TopicProse

if TYPE_CHECKING:
    from agentworks.capabilities.base import RunContext
    from agentworks.transports import Transport


class CodexConfig(AgwModel):
    """What a session template tells the ``codex`` integration.

    String choices are Codex-owned sets that drift between releases, so
    they forward unvalidated. Codex validates some values at startup, but
    not every set is an enum there (0.147.0 accepts arbitrary reasoning
    effort strings). The integration emits ``--strict-config`` by default
    to catch unknown config keys and wrong value types. Native
    ``developer_instructions`` config and the positional ``--`` boundary were
    rechecked with Codex CLI 0.149.1.
    """

    name: Literal["codex"]
    """The harness integration this config is for."""

    model: str | None = None
    """Forwarded as ``-m``."""

    sandbox: str | None = None
    """Forwarded as ``-s``."""

    approval_policy: str | None = None
    """Forwarded as ``-a``."""

    profile: str | None = None
    """Forwarded as ``-p``."""

    network: bool | None = None
    """Whether the workspace-write sandbox may reach the network. Both
    directions forward explicitly, so ``false`` overrides a profile or a
    ``config.toml`` that enabled it."""

    approvals_reviewer: str | None = None
    """Forwarded as a codex config override, TOML-encoded."""

    reasoning_effort: str | None = None
    """Forwarded to Codex's ``model_reasoning_effort`` config key,
    TOML-encoded. Values are Codex-owned and forward unvalidated; Codex
    0.147.0 does not reject an unknown effort string at config load. A
    child template's declared value replaces its parent's."""

    goal: str | None = None
    """A native persistent goal requested through the first prompt of a fresh
    conversation. Codex exposes ``/goal`` but no documented startup flag."""

    initial_prompt: str | None = None
    """The operator's first prompt for a fresh conversation. It follows any
    generated setup and is not replayed on resume."""

    agent: str | None = None
    """The declared ``name`` of a Codex custom agent whose
    ``developer_instructions`` should guide a fresh primary thread. Codex has
    no primary-thread agent selector, so this is prompt-mediated."""

    developer_instructions: str | None = None
    """Additional model-readable instructions injected natively on every
    Codex process launch, including explicit resume and the picker. This does
    not apply model, reasoning, sandbox, MCP, skills, or other agent
    configuration."""

    vim_mode: bool = False
    """Start Codex's composer in Vim normal mode. Omitted when false, so
    the target's own configuration remains authoritative by default. A
    child template's declared value replaces its parent's."""

    writable_dirs: list[str] = Field(default_factory=list)
    """Extra directories the sandbox may write, each forwarded as
    ``--add-dir``. Inheritance combines parent and child entries."""

    web_search: bool | str | None = None
    """A Codex-owned web-search mode forwarded via ``-c``. For backward
    compatibility, ``true`` passes ``--search`` (live search) and ``false``
    emits no override; ``false`` therefore inherits target config, while
    ``disabled`` forces search off. A child template's declared value
    replaces its parent's."""

    disable_strict_config: bool | None = None
    """Turn OFF ``--strict-config``, for a target whose own
    ``config.toml`` a newer codex wrote."""

    extra_args: Annotated[list[str], MergeStrategy.REPLACE] = Field(default_factory=list)
    """Raw argv tokens appended verbatim after every generated CLI option,
    so they can add unmodeled arguments or override managed ``-c`` values.
    Codex 0.147.0 takes the last value for a repeated ``-c`` key. On a fresh
    launch with an initial input, only the ``--`` prompt separator and prompt
    value follow these option tokens. Overriding ``notify`` disables
    deterministic id binding, leaving discovery and the picker as fallbacks.
    A child template's declared list replaces its parent's."""


# Config field -> the codex flag it forwards to, in emission order. The
# choice sets (sandbox modes, approval policies, model names) are
# codex-owned and drift between releases, so values forward unvalidated;
# an invalid one surfaces as codex's own startup error in the pane.
_FLAG_FIELDS: tuple[tuple[str, str], ...] = (
    ("model", "-m"),
    ("sandbox", "-s"),
    ("approval_policy", "-a"),
    ("profile", "-p"),
)

# The codex config key ``network`` forwards to (via ``-c``). Codex-owned
# and could drift; a renamed key is SILENTLY ignored by a non-strict
# codex (verified against 0.146.0), which is exactly why the harness integration
# emits ``--strict-config`` by default: with it, drift surfaces as
# codex's own unknown-field startup error in the pane instead of a
# session that silently has no network. Re-verify on codex major bumps.
_NETWORK_KEY = "sandbox_workspace_write.network_access"


def _toml_basic_string(value: str) -> str:
    """Encode ``value`` as a quoted TOML basic string for a ``-c`` override.

    Escaping is encoding, not validation: the value still reaches codex
    verbatim. It is load-bearing for two reasons (verified against
    0.146.0): codex parses ``-c key=value`` as a TOML DOCUMENT splice, so
    an unescaped newline in the value silently defines additional config
    keys (accepted even under ``--strict-config``), and an unescaped
    quote makes the value fail TOML parsing into the raw-string fallback.
    Escaped, both arrive as one literal value and fail codex's own enum
    check loudly instead.
    """
    out: list[str] = []
    for ch in value:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 0x20 or ch == "\x7f":
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


# The codex config key ``approvals_reviewer`` forwards to (via ``-c``;
# codex exposes no dedicated flag for it, so the strict-config default is
# the drift guard here too). Values are codex-owned and forward
# unvalidated: 0.146.0 documents `user` (the default: escalations prompt
# the human) and `auto_review` (codex's risk-based reviewer subagent
# adjudicates), plus the legacy `guardian_subagent`.
_APPROVALS_REVIEWER_KEY = "approvals_reviewer"
_REASONING_EFFORT_KEY = "model_reasoning_effort"
_VIM_MODE_KEY = "tui.vim_mode_default"
_WEB_SEARCH_KEY = "web_search"


# The rollout root. ``CODEX_HOME`` is the CLI's own override env var
# (honored by codex-cli 0.146.0); the default is ``$HOME/.codex``.
# Expanded by the target-side shell inside the probes, never here; kept
# double-quoted because it is interpolated into shell commands as one word.
_SESSIONS_DIR = '"${CODEX_HOME:-$HOME/.codex}/sessions"'
# The same path spelled for a human in an error hint (no shell quoting).
_SESSIONS_DIR_DISPLAY = "$CODEX_HOME/sessions, by default ~/.codex/sessions"

# Rollout files are ``sessions/<Y>/<M>/<D>/rollout-<timestamp>-<uuid>.jsonl``
# with the codex-minted session uuid embedded verbatim at the tail. Discovery
# takes candidate identity from the FILENAME and never from the rollout's
# ``session_meta.session_id``, which is the PARENT's uuid in a subagent
# rollout (verified 2026-08-04 against 0.146.0).
_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_ROLLOUT_ID_RE = re.compile(rf"rollout-.+-({_UUID})\.jsonl$")
_RECORDED_ID_RE = re.compile(rf"^{_UUID}$")

# The literal ``session_meta`` needle that keeps a subagent (whose
# ``source`` is a JSON OBJECT) and an ``exec`` session (``"exec"``) out of
# the candidate set. Codex serializes the first rollout line as compact
# JSON, so the literal matches without parsing. That compactness is a
# verified codex behavior, not an assumption we control: a
# pretty-printed ``session_meta`` would match nothing, which costs the
# fallback (never a mis-adoption). Re-verify on codex major bumps.
_CLI_SOURCE_NEEDLE = '"source":"cli"'

# What the discovery probe prints for a candidate whose first line would
# not read. Deliberately not rollout-path-shaped, so the stdout parse
# cannot confuse it with an answer, and deliberately not silence: it
# counts as an unnamed candidate and sends the operator to the picker.
_UNREADABLE_SENTINEL = "?agw-unreadable-rollout"

# The marker-era launch-anchor shape (2026-08-01 through 2026-08-04),
# kept only to validate a legacy blob value before the pane removes that
# path. See ``_take_legacy_marker``.
_LEGACY_MARKER_RE = re.compile(r"^\.agentworks/codex/[A-Za-z0-9_.-]+\.launch$")


def _probe_hint(returncode: int) -> str:
    """The recovery hint for a probe that could not answer, forked on WHY.

    A hint that names an action the operator cannot take is worse than no
    hint: ``_FIND_FAILED_EXIT`` means ``find`` ran and failed against the
    on-disk sessions tree, so telling them to wait for the target to
    become reachable would point at a healthy component. Everything else
    in the non-{0,1} band (an SSH failure's 255, a shell that would not
    start) really is reachability.
    """
    if returncode == _FIND_FAILED_EXIT:
        return (
            f"Check that the codex sessions directory ({_SESSIONS_DIR_DISPLAY}) is readable by the "
            f"launch user and not damaged, then retry."
        )
    return "Retry once the launch target is reachable."


def _awk_string(value: str) -> str:
    """``value`` as an awk string literal, for splicing into a generated
    awk program. Only backslash and double quote need escaping; the
    needles this encodes contain neither newlines nor control characters.
    """
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


# The probes' distinct exit codes, chosen apart from find's own 1 and the
# shell's 2 so a probe that FAILED can never masquerade as a definitive
# answer (rule 4: a probe that could not run is not a probe that found
# nothing). 3 is the one definitive-fresh sentinel; 4/5/6 are raise codes
# that name their failed precondition.
_NO_SESSIONS_DIR_EXIT = 3  # sessions dir absent: codex never ran here
_CWD_RESOLVE_EXIT = 4  # workspace dir could not be canonicalized: raise
_FIND_FAILED_EXIT = 5  # find itself failed (not a mere no-match): raise
_READ_FAILED_EXIT = 6  # the recorder file exists but would not read: raise


class _Layer2(NamedTuple):
    """What source-filtered discovery found: the ids it could name, plus
    whether it also saw a candidate it could NOT name (a rollout that
    passed the filter but whose filename does not embed a uuid). An
    unnamed candidate cannot be adopted, and ignoring it could turn a
    genuinely ambiguous workspace into a confident single adoption, so it
    forces the picker and lets the human decide."""

    ids: tuple[str, ...]
    unnamed: bool


class CodexIntegration(HarnessIntegration):
    """Runs Codex, resuming or launching fresh per on-disk state."""

    contract_version: ClassVar[int] = 2
    name: ClassVar[str] = "codex"
    description: ClassVar[str] = "Run Codex, resuming its session when one exists"
    config_model: ClassVar[type[CodexConfig]] = CodexConfig
    prose: ClassVar[TopicProse | None] = TopicProse(
        title="Codex",
        overview="""
        Runs Codex as the session's workload, resuming its conversation when one exists
        and launching fresh when none does.

        Codex records its sessions per working directory rather than per name, so
        matching one to an agentworks session is a heuristic. When it adopts a
        conversation it says which one, so the choice can be checked rather than
        assumed.

        Ships as the opt-in `codex` system plugin, and needs the `codex` CLI on the
        session's target.
        """,
    )

    # Set by start / _resume_or_launch on each op; drives the HarnessStart note.
    # None until the op runs (nothing decided yet).
    _decision: Literal["resumed", "adopted", "picker", "stale", "fresh"] | None = None
    # The uuid the "adopted" leaf settled on, so both operator surfaces can
    # NAME it. Adoption is the one leaf that is explicitly a heuristic, and
    # the whole point of announcing it is that the operator can check it
    # against the conversation the pane comes up in.
    _adopted_id: str | None = None
    # Whether this op DROPPED a bound id whose rollout was gone before
    # deciding. The leaf that follows composes both facts, because the
    # drop is news on its own: it is the operator's `codex archive`
    # taking effect, and it is why the pane may come up in a different
    # conversation than last time.
    _dropped_stale: bool = False

    @property
    def config(self) -> CodexConfig:
        """This session's validated codex config."""
        return self._config_as(CodexConfig)

    def start(
        self,
        ctx: RunContext,
        *,
        intent: HarnessLaunchIntent = HarnessLaunchIntent.CONTINUE,
    ) -> HarnessStart:
        """Choose an ordinary continuation or a deliberately fresh launch.

        ``session create`` mints a brand-new session row, so by definition
        no codex conversation belongs to this session yet: there is nothing
        to resume and nothing legitimate to adopt. Create reads the recorder
        identity it must reject, but performs no rollout probe or discovery;
        it therefore requires the owning launch target without adopting from it.

        Both of this integration's identity channels are name-derived on
        the target (the recorder file is ``<session-name>.thread``;
        discovery matches the workspace directory), so at create time
        either would hand a new session the conversation of a deleted
        predecessor that shared its name or its workspace.

        For the RECORDER channel that is closed outright: create reads only
        the identity it must reject, records that rejection, and the fresh
        command deletes the file, so no later op can adopt a dead namesake's
        id as a BOUND one. The DISCOVERY channel
        is narrowed, not closed, and this is the honest limit of what
        ships: if the dead session's rollout is the only interactive one in
        the workspace, this session's first later start can still adopt it
        through layer 2. That is tolerable rather than invisible: the
        adoption is announced on both surfaces and names the uuid, the
        candidate is a prior Agentworks conversation in the same workspace
        rather than a stranger's, and the new session has typed nothing
        yet, so nothing of its own is lost. Closing it needs a
        creation-time floor on candidate age, which is clock-skew sensitive
        between controller and target (a wrong floor would exclude the
        session's OWN rollout and orphan it), so it is recorded as a
        follow-up in the decisions doc rather than guessed at here.

        This is the correct semantics for deferred discovery rather than an
        asymmetry to apologize for: a fresh launch resumes nothing, while its
        effective workload config may provide initial input.
        """
        self._decision = None
        self._adopted_id = None
        self._dropped_stale = False
        command = self._start_fresh(ctx) if intent.starts_fresh else self._resume_or_launch(ctx)
        return HarnessStart(command, self._decision_note(intent=intent))

    def _decision_note(self, *, intent: HarnessLaunchIntent) -> str | None:
        """The console line for the op that just ran, with a forced-fresh
        policy line or one per ordinary decision leaf (operator-decided
        2026-08-04: the console must say what is happening, in the same
        resume vocabulary as the pane echo).

        A dropped stale binding is composed INTO the leaf it precedes
        rather than replacing it: an operator who ran ``codex archive``
        deliberately and then lands in a different conversation needs both
        halves, that their previous binding is gone and what took its
        place. Only the drop-then-nothing-found case is the bare
        archived-or-gone line.
        """
        if self._decision is None:
            return None
        if intent is HarnessLaunchIntent.FORCE_NEW:
            note = "Fresh Codex session requested. Starting a new one without resuming prior state..."
            if disclosure := self._fresh_setup_disclosure():
                note = f"{note} {disclosure}"
            return note
        if intent is HarnessLaunchIntent.CREATE:
            note = "Starting a new Codex session..."
            if disclosure := self._fresh_setup_disclosure():
                note = f"{note} {disclosure}"
            return note
        dropped = "Previous Codex conversation is archived or gone. " if self._dropped_stale else ""
        if self._decision == "resumed":
            return "Existing Codex session found. Resuming..."
        if self._decision == "adopted":
            which = (
                "a different Codex conversation in this workspace" if dropped else "this session's Codex conversation"
            )
            return f"{dropped}Identified {which} from Codex's own on-disk state: {self._adopted_id}. Resuming..."
        if self._decision == "picker":
            note = (
                f"{dropped}Could not identify this session's Codex conversation with confidence. "
                f"Codex's session picker is opening in the pane; picking one binds this session to "
                f"that conversation from its next turn, and esc starts a fresh conversation instead."
            )
            if disclosure := self._picker_workload_disclosure():
                note = f"{note} {disclosure}"
            return note
        if self._decision == "stale":
            note = "Previous Codex session is archived or gone. Starting a new one..."
        else:
            note = "No existing Codex session. Starting a new one..."
        if disclosure := self._fresh_setup_disclosure():
            note = f"{note} {disclosure}"
        return note

    def _start_fresh(self, ctx: RunContext) -> str:
        """Reject the visible recorder binding and launch bare Codex."""
        launch_target = ctx.admin_target() if self._admin else ctx.agent_target()
        if launch_target is None:
            raise StateError(
                "codex fresh start requires its owning launch target",
                entity_kind="session",
                entity_name=self._session_name,
            )
        recorded = self._recorded_thread_id(launch_target)
        self._state.pop("session_id", None)
        self._state["fresh_pending"] = recorded
        self._decision = "fresh"
        return self._fresh_command(
            msg=f"agentworks harness integration (codex): starting new session {self._session_name}",
            legacy_marker=self._take_legacy_marker(),
        )

    def _resume_or_launch(self, ctx: RunContext) -> str:
        """The RESUME decision, from codex's own durable state on the launch
        target (:meth:`start` runs this only for a continuation intent).
        Five leaves:

        - a BOUND id (recorded by the notify recorder, or stored by an
          earlier op) whose rollout exists: resume it (``resumed``);
        - a bound id whose rollout is gone (archived or deleted): drop the
          stale id and fall through to discovery, which lands on
          ``adopted``, ``picker``, or ``stale``. Note what that means for
          the archived policy: dropping the id is NOT the same as starting
          fresh. Layers 2/3 then decide, so an archived conversation can be
          followed by adopting a DIFFERENT one in the same workspace, or by
          the picker; only the nothing-found case launches fresh. Whichever
          it is, the leaf reports the drop as well as the outcome;
        - nothing bound, one source-filtered candidate in this workspace:
          adopt its uuid and resume it (``adopted``);
        - nothing bound, several candidates: launch codex's own session
          picker and let the human decide (``picker``);
        - nothing bound, no candidate: launch fresh (``fresh``).

        The recorder's id WINS over a differing stored one: it is what
        codex reported for the conversation most recently live in this
        session's pane, so a picker-esc fresh conversation rebinds the
        session to what the operator is actually looking at.

        A stored ``session_id`` of the wrong type is garbage this harness integration
        never wrote (the blob is only as trustworthy as the DB it came
        from): it is swept out of the namespace rather than left to
        confuse a later read. Every path returns a single ``sh -c`` pane
        command that echoes the visible decision, provisions the recorder,
        and ``exec``s ``codex``.
        """
        launch_target = ctx.admin_target() if self._admin else ctx.agent_target()
        if launch_target is None:
            # Unlike claude-code (which keeps its minted id either way, so
            # guessing "fresh" is lossless), a codex fresh launch drops the
            # bound id; guessing here could orphan a resumable
            # conversation, so raise. Create needs no target at all, since
            # it decides nothing (see start()).
            raise StateError(
                f"session '{self._session_name}': the op context carries no "
                f"launch target to probe codex session state on; refusing "
                f"to guess resume-vs-launch.",
                entity_kind="session",
                entity_name=self._session_name,
                hint="Retry once the launch target is reachable.",
            )
        legacy_marker = self._take_legacy_marker()
        if "fresh_pending" in self._state:
            pending = self._state["fresh_pending"]
            valid_pending = pending is None or (isinstance(pending, str) and _RECORDED_ID_RE.fullmatch(pending))
            recorded = self._recorded_thread_id(launch_target)
            if not valid_pending:
                from agentworks import output

                output.warn(
                    f"session '{self._session_name}': malformed Codex fresh-pending state; "
                    "launching fresh instead of adopting prior state"
                )
                self._state.pop("session_id", None)
                self._state["fresh_pending"] = recorded
                self._decision = "fresh"
                return self._fresh_command(
                    msg=f"agentworks harness integration (codex): starting new session {self._session_name}",
                    legacy_marker=legacy_marker,
                )
            if recorded is None or recorded == pending:
                self._state.pop("session_id", None)
                self._decision = "fresh"
                return self._fresh_command(
                    msg=f"agentworks harness integration (codex): starting new session {self._session_name}",
                    legacy_marker=legacy_marker,
                )
            self._state["session_id"] = recorded
            self._state.pop("fresh_pending", None)
        sid = self._bound_session_id(launch_target)
        if sid is not None:
            if self._rollout_exists(launch_target, sid):
                self._decision = "resumed"
                return self._resume_command(
                    sid,
                    msg=f"agentworks harness integration (codex): resuming session {self._session_name}",
                    legacy_marker=legacy_marker,
                )
            # The rollout is gone (archived or deleted): not resumable, by
            # the pinned archived policy. Drop the stale id and let
            # discovery decide, so an archived conversation does not block
            # adopting the one actually in this workspace.
            self._state.pop("session_id", None)
            self._dropped_stale = True
        # The drop is composed into whichever leaf follows, never swallowed
        # by it: the operator archived that conversation on purpose and
        # needs to know both that the binding is gone and what replaced it.
        dropped = "previous codex conversation archived or gone; " if self._dropped_stale else ""
        found = self._discover(launch_target)
        if len(found.ids) == 1 and not found.unnamed:
            adopted = found.ids[0]
            self._state["session_id"] = adopted
            self._adopted_id = adopted
            self._decision = "adopted"
            which = (
                "a different codex conversation in this workspace" if dropped else "this session's codex conversation"
            )
            return self._resume_command(
                adopted,
                msg=f"agentworks harness integration (codex): {dropped}identified {which} "
                f"from codex's on-disk state ({adopted}); resuming session {self._session_name}",
                legacy_marker=legacy_marker,
            )
        if found.ids or found.unnamed:
            self._decision = "picker"
            picker_disclosure = self._picker_workload_disclosure()
            warning = f" {picker_disclosure}" if picker_disclosure is not None else ""
            return self._picker_command(
                msg=f"agentworks harness integration (codex): {dropped}could not identify this "
                f"session's codex conversation with confidence; opening codex's session picker. "
                f"Pick one to bind session {self._session_name} to it from its next turn, or press "
                f"esc to start a fresh conversation.{warning}",
                legacy_marker=legacy_marker,
            )
        if self._dropped_stale:
            self._decision = "stale"
            return self._fresh_command(
                msg=f"agentworks harness integration (codex): previous codex session archived or "
                f"gone; starting new session {self._session_name}",
                legacy_marker=legacy_marker,
            )
        self._decision = "fresh"
        return self._fresh_command(
            msg=f"agentworks harness integration (codex): starting new session {self._session_name}",
            legacy_marker=legacy_marker,
        )

    def _take_legacy_marker(self) -> str | None:
        """Retire the marker-era ``discovery_marker`` blob key, returning
        the path it held IF it still looks like one this harness
        integration wrote, so the next pane command can ``rm -f`` it.

        Compatibility with the 2026-08-01 through 2026-08-04 marker scheme,
        which anchored discovery on a nonce ``~/.agentworks/codex/<name>-
        <nonce>.launch`` file. Nothing reads it any more: the key is
        deleted on the first op that touches the blob and its file is
        cleaned up best-effort, so no dead marker outlives its blob entry.

        The value is shape-checked against that scheme before it becomes
        an ``rm -f`` argument, for the same reason the wrong-typed
        ``session_id`` above is swept: the blob is only as trustworthy as
        the DB row it came from. ``shlex.quote`` makes an arbitrary value
        inert as SHELL syntax, but a perfectly ordinary relative path
        (``../../.ssh/authorized_keys``) is something ``rm -f`` would
        simply follow. An unrecognized value therefore loses only its
        cleanup: the key is retired either way, so nothing re-reads it.
        """
        marker = self._state.pop("discovery_marker", None)
        if isinstance(marker, str) and _LEGACY_MARKER_RE.match(marker):
            return marker
        return None

    def _bound_session_id(self, transport: Transport) -> str | None:
        """The id bound to this session, or ``None`` when nothing is: the
        uuid codex last recorded through the notify recorder if there is
        one, else the id a previous op stored.

        The recorder's id wins over a differing stored one (last write
        wins): it names the conversation most recently live in this
        session's pane. Adopting it into the blob is what makes a
        picker-esc fresh conversation, or any conversation codex minted
        after our launch, resumable deterministically next time.

        A stored value that is not a uuid is swept rather than used: the
        blob is only as trustworthy as the DB row it came from, and
        handing codex ``resume <garbage>`` would fail the pane opaquely
        where falling through to discovery self-heals the session. Codex
        ids are UUIDv7, so every id this integration ever stored passes.
        """
        stored = self._state.get("session_id")
        if "session_id" in self._state and not (isinstance(stored, str) and _RECORDED_ID_RE.match(stored)):
            del self._state["session_id"]  # sweep garbage; never this harness integration's write
            stored = None
        recorded = self._recorded_thread_id(transport)
        if recorded is not None:
            if recorded != stored:
                self._state["session_id"] = recorded
            return recorded
        return stored if isinstance(stored, str) else None

    def _pane_command(
        self,
        *,
        msg: str,
        head: tuple[str, ...],
        legacy_marker: str | None,
        clear_binding: bool = False,
        fresh: bool = False,
    ) -> str:
        """The pane command shared by all three launch forms: echo the
        visible decision, clean up after the retired marker scheme,
        provision the notify recorder, then ``exec codex`` with ``head``
        (the form's own leading argv) followed by the config flags.

        A single ``sh -c`` so the whole thing survives the ``exec``
        wrapping the tmux pane applies (``exec`` takes one simple
        command). The message and the generated argv carry no ``{{word}}``
        tokens, so the core template-var substitution does not mangle them.
        """
        parts = [f"echo {shlex.quote(msg)}"]
        if legacy_marker is not None:
            parts.append(f"rm -f {home_word(legacy_marker)}")
        if clear_binding:
            # A fresh launch has no conversation bound yet, so a leftover
            # recording must not outlive it and re-report a dead id.
            parts.append(f"rm -f {home_word(thread_tail(self._session_name))}")
        parts.append(provision_fragment())
        parts.append(f"exec codex {self._codex_argv(head, fresh=fresh)}".rstrip())
        return f"sh -c {shlex.quote('; '.join(parts))}"

    def _resume_command(self, sid: str, *, msg: str, legacy_marker: str | None) -> str:
        """The resume pane command: ``codex resume <sid>`` with
        ``-c tui.resume_cwd=current`` pinning the cross-cwd picker off
        deterministically (the pane has already cd-ed to the workspace
        dir, so "current" is always the right answer)."""
        return self._pane_command(
            msg=msg,
            head=("resume", sid, "-c", "tui.resume_cwd=current"),
            legacy_marker=legacy_marker,
        )

    def _picker_command(self, *, msg: str, legacy_marker: str | None) -> str:
        """The ambiguity pane command: bare ``codex resume``, codex's own
        cwd-scoped session picker (which already hides ``exec`` and
        subagent sessions), with our managed flags and the recorder
        attached. Verified 2026-08-04 against 0.146.0 under the Agentworks
        pane wrapper: the picker renders below our echoed decision line,
        enter resumes the selection, and esc starts a NEW conversation in
        the same process, inheriting the command line's ``-c`` overrides
        so the recorder binds either way.

        ``-c tui.resume_cwd=current`` scopes the list to this workspace
        directory, the same pin the explicit-id form uses."""
        return self._pane_command(
            msg=msg,
            head=("resume", "-c", "tui.resume_cwd=current"),
            legacy_marker=legacy_marker,
        )

    def _fresh_command(self, *, msg: str, legacy_marker: str | None) -> str:
        """The fresh-launch pane command, with a positional prompt only when
        this instance declares fresh-conversation workload input.

        It also removes any leftover recording (``clear_binding``). A fresh
        launch means nothing is bound yet, so a stale ``.thread`` file must
        not survive it and re-report a dead id on the next op. On the
        create path that is what closes the recorder channel completely
        (see :meth:`start`): create adopts nothing, and it also clears a
        dead namesake's recording so no FOLLOWING ``resume`` can bind it
        either."""
        if disclosure := self._fresh_setup_disclosure():
            msg = f"{msg}; {disclosure}"
        return self._pane_command(
            msg=msg,
            head=(),
            legacy_marker=legacy_marker,
            clear_binding=True,
            fresh=True,
        )

    def _codex_argv(self, head: tuple[str, ...], *, fresh: bool = False) -> str:
        """The quoted argv text after ``codex``: the form's leading tokens,
        managed flags, the ``notify`` and developer-instruction overrides,
        ``extra_args``, then any separated fresh prompt.

        The ``notify`` override lands after every managed flag and before
        ``extra_args``, which is what makes an operator ``notify`` in
        ``extra_args`` win (later codex ``-c`` overrides replace earlier
        ones). That deliberately disables the id binding, so it is
        documented on the field; discovery and the picker still hold.
        """
        parts = [shlex.quote(token) for token in (*head, *self._managed_flags())]
        parts += ["-c", notify_value_word(self._session_name)]
        if self.config.developer_instructions is not None:
            value = _toml_basic_string(self.config.developer_instructions)
            parts += ["-c", quote_literal_argv(f"developer_instructions={value}")]
        parts += [shlex.quote(token) for token in self._extra_arg_tokens()]
        if fresh and (prompt := self._fresh_prompt()) is not None:
            parts += ["--", quote_literal_argv(prompt)]
        return " ".join(parts)

    def _fresh_prompt(self) -> str | None:
        """The operator prompt, optionally preceded by compatibility setup.

        Codex has a native persistent goal but no documented startup flag,
        and its custom-agent selection applies to spawned agents rather than
        the primary thread. The first prompt asks Codex to bridge those two
        gaps. Setup values are JSON data so arbitrary operator text stays
        distinct from the instructions that interpret it.
        """
        agent = self.config.agent
        setup = {
            key: value
            for key, value in (
                ("agent", agent),
                ("goal", self.config.goal),
            )
            if value is not None
        }
        if not setup:
            return self.config.initial_prompt

        steps: list[str] = ["Complete this Agentworks setup before doing any other work in this fresh conversation."]
        if agent is not None:
            steps.append(
                "Locate the Codex custom agent whose declared name exactly matches setup.agent. "
                "Read and follow its developer_instructions in this primary thread. If it is missing "
                "or unreadable, report that problem instead of substituting another agent. Only its "
                "developer_instructions apply here; do not claim its model, reasoning, sandbox, MCP, "
                "skills, or other configuration was applied."
            )
        if self.config.goal is not None:
            steps.append(
                "Create a native persistent Codex goal whose objective is exactly setup.goal. Omit "
                "token_budget rather than inventing one."
            )
        setup_json = json.dumps(setup, ensure_ascii=False, separators=(",", ":"))
        parts = ["\n".join(f"{index}. {step}" for index, step in enumerate(steps, start=1)), setup_json]
        if self.config.initial_prompt is not None:
            parts.append(
                "After completing the setup above, continue with this operator initial prompt:\n"
                f"{self.config.initial_prompt}"
            )
        return "\n\n".join(parts)

    def _fresh_setup_disclosure(self) -> str | None:
        """An operator warning for fresh setup that Codex must interpret."""
        mediated: list[str] = []
        if self.config.agent is not None:
            mediated.append("custom-agent identity")
        if self.config.goal is not None:
            mediated.append("goal creation")
        if not mediated:
            return None
        joined = ", ".join(mediated)
        return f"Agentworks is requesting {joined} through Codex's initial prompt; verify the result in the TUI."

    def _picker_workload_disclosure(self) -> str | None:
        """Warn when picker Esc cannot conditionally receive fresh input."""
        if not any(
            value is not None
            for value in (
                self.config.goal,
                self.config.initial_prompt,
                self.config.agent,
            )
        ):
            return None
        return (
            "Selecting an existing conversation resumes it normally. Pressing esc creates a fresh "
            "Codex conversation without applying the configured goal, agent, or initial prompt "
            "because Codex exposes no conditional picker input."
        )

    def _managed_flags(self) -> list[str]:
        """The flags the harness integration models, as argv tokens, in emission order.

        ``--strict-config`` is emitted by DEFAULT (operator-decided
        2026-08-03): the harness integration owns the emitted config surface, and
        strictness turns codex-owned key drift (``_NETWORK_KEY``) into a
        loud startup error instead of a silently ignored override. It
        also hardens the target user's own ``config.toml``; that is
        deliberate and documented, and ``disable_strict_config: true``
        is the sanctioned off-switch for a config codex must tolerate
        (e.g. one written by a newer codex than the target runs).
        """
        tokens: list[str] = []
        if self.config.disable_strict_config is not True:
            tokens.append("--strict-config")
        for field_name, flag in _FLAG_FIELDS:
            value = getattr(self.config, field_name)
            if value is not None:
                tokens += [flag, value]
        if self.config.network is not None:
            # Both directions forward explicitly: `false` overrides a
            # profile or config.toml that enabled network access.
            tokens += ["-c", f"{_NETWORK_KEY}={'true' if self.config.network else 'false'}"]
        if self.config.approvals_reviewer is not None:
            # Encoded as a TOML basic string: see _toml_basic_string for
            # why raw interpolation would be a silent-injection hole.
            tokens += ["-c", f"{_APPROVALS_REVIEWER_KEY}={_toml_basic_string(self.config.approvals_reviewer)}"]
        if self.config.reasoning_effort is not None:
            tokens += ["-c", f"{_REASONING_EFFORT_KEY}={_toml_basic_string(self.config.reasoning_effort)}"]
        if self.config.vim_mode:
            tokens += ["-c", f"{_VIM_MODE_KEY}=true"]
        for item in self.config.writable_dirs:
            tokens += ["--add-dir", item]
        if self.config.web_search is True:
            tokens.append("--search")
        elif isinstance(self.config.web_search, str):
            tokens += ["-c", f"{_WEB_SEARCH_KEY}={_toml_basic_string(self.config.web_search)}"]
        return tokens

    def _extra_arg_tokens(self) -> list[str]:
        """``extra_args`` as raw option tokens after generated options."""
        return list(self.config.extra_args)

    def _recorded_thread_id(self, transport: Transport) -> str | None:
        """The uuid the notify recorder last wrote for this session, or
        ``None`` when nothing valid is recorded.

        Absent file means nothing is bound: a session whose codex has not
        completed a turn yet, or one whose recorder never ran (codex
        ignores a missing notify program silently). Content that is not a
        single uuid is likewise treated as nothing bound: the recorder
        only ever writes one, so anything else is not ours to interpret,
        and the fresh path clears it. Only a file that EXISTS and would
        not read raises, since that is a probe that could not run rather
        than an answer.

        The stdout parse tolerates login-shell noise the same way the
        discovery probe does: only uuid-shaped lines are considered.
        """
        target = home_word(thread_tail(self._session_name))
        inner = f"[ -f {target} ] || exit 1; cat {target} 2>/dev/null || exit {_READ_FAILED_EXIT}"
        result = transport.run(f'"$SHELL" -lic {shlex.quote(inner)}', check=False)
        if result.returncode == 1:
            return None  # nothing recorded yet: not bound
        if result.returncode != 0:
            recording = f"~/{thread_tail(self._session_name)}"
            raise StateError(
                f"session '{self._session_name}': could not read the recorded "
                f"codex thread id ({recording}) on "
                f"{self._target_label} (exit {result.returncode}); refusing to "
                f"guess resume-vs-launch.",
                entity_kind="session",
                entity_name=self._session_name,
                hint=(
                    # The file is there and unreadable, so "retry when the
                    # target is reachable" would send the operator in a
                    # circle. Deleting it is safe and sufficient: the next
                    # resume falls through to discovery and the picker, and
                    # codex rebinds the id on the session's next turn.
                    f"Remove {recording} on the launch target (or fix its permissions) and retry; "
                    f"the next resume identifies the conversation from codex's own state instead, "
                    f"so nothing is orphaned."
                    if result.returncode == _READ_FAILED_EXIT
                    else "Retry once the launch target is reachable."
                ),
            )
        recorded = [line.strip() for line in result.stdout.splitlines() if _RECORDED_ID_RE.match(line.strip())]
        if len(recorded) != 1:
            return None  # empty, noise-only, or not something the recorder wrote
        return recorded[0]

    def _rollout_exists(self, transport: Transport, sid: str) -> bool:
        """True iff the bound session's rollout
        (``rollout-<timestamp>-<sid>.jsonl``, hence the ``*-<sid>.jsonl``
        glob) exists under the sessions dir on the launch target.
        Shell-neutral (the glob is quoted through to find); runs through
        ``$SHELL -lic`` like the readiness probe. ``archived_sessions/``
        is deliberately NOT probed: an archived session reports
        not-resumable and the caller drops the bound id and falls through to
        discovery, rather than silently reversing ``codex archive``. Note
        that not-resumable is not the same as fresh: layers 2/3 decide what
        happens next.

        The exit code is read, not just ``.ok``, so a probe that could not
        EXECUTE never masquerades as "no rollout". The inner command keeps
        find's own failure distinguishable from a clean no-match: a
        missing sessions dir (codex never ran here) exits 1 up front; a
        printed match exits 0 (a found rollout is definitive even if find
        also stumbled elsewhere); a find that FAILED without printing one
        exits with the find-failed code rather than folding into "no
        rollout". Anything but {0, 1} (that code, an SSH failure's 255, a
        shell that could not start) raises: guessing "fresh" would drop
        the bound id and orphan a resumable conversation."""
        needle = shlex.quote(f"*-{sid}.jsonl")
        inner = (
            f"[ -d {_SESSIONS_DIR} ] || exit 1; "
            f"out=$(find {_SESSIONS_DIR} -name {needle} -print -quit 2>/dev/null); rc=$?; "
            f'[ -n "$out" ] && exit 0; [ "$rc" -eq 0 ] || exit {_FIND_FAILED_EXIT}; exit 1'
        )
        result = transport.run(f'"$SHELL" -lic {shlex.quote(inner)}', check=False)
        if result.returncode == 0:
            return True  # rollout on disk: resume
        if result.returncode == 1:
            return False  # the probe ran, no match: not resumable
        raise StateError(
            f"session '{self._session_name}': could not probe for the codex "
            f"rollout on {self._target_label} (exit {result.returncode}); "
            f"refusing to guess resume-vs-launch.",
            entity_kind="session",
            entity_name=self._session_name,
            hint=_probe_hint(result.returncode),
        )

    def _discover(self, transport: Transport) -> _Layer2:
        """Layer-2 discovery: the rollouts in this session's workspace
        directory that could be a human's interactive codex conversation.

        A candidate's first JSONL line (its ``session_meta``) must carry
        BOTH ``"source":"cli"`` and this session's canonicalized workspace
        cwd. The source needle is what keeps codex SUBAGENTS out: a
        subagent's rollout sits in the same sessions tree with the same
        cwd as its parent, and its ``source`` is a JSON OBJECT
        (``{"subagent":...}``, the guardian reviewer included) rather than
        the string ``"cli"``; an ``exec`` session stamps ``"exec"``. Both
        are structurally excluded, which is exactly the set codex's own
        resume picker shows by default (verified 2026-08-04 against
        0.146.0). Identity comes from the FILENAME, never from
        ``session_meta.session_id``, which holds the PARENT's uuid in a
        subagent rollout.

        One purposeful round-trip. The target-side command exits with a
        distinct code per precondition so a probe that FAILED can never
        masquerade as a definitive answer: sessions dir absent is the one
        definitive-empty exit; the workspace dir failing to canonicalize
        raises; find failing raises (for an ENUMERATION a partial listing
        is dangerous, since a missed candidate could turn "several" into a
        confident single adoption, so unlike the existence probe no output
        is trusted from a failed find).

        The cwd filter compares each rollout's ``session_meta`` against
        the workspace directory canonicalized TARGET-side via
        ``cd <workspace> && pwd -P``, so a logical-vs-physical symlink
        mismatch cannot exclude our own rollout. Verified against
        codex-cli 0.146.0 (decisions doc, "What the CLI actually
        provides"): codex serializes the session cwd as a PHYSICAL path,
        even when launched from a symlinked directory, in compact JSON
        (``"cwd":"<path>"`` with no spaces), which a literal match finds
        without parsing. The matched path is not JSON-escaped: a workspace
        path carrying a JSON-special character (a quote, a backslash)
        would fail the match and degrade to a fresh launch, never a
        mis-adoption; workspace names are validated to a safe character
        set, so this stays theoretical. The stdout parse also tolerates
        login-shell noise: only lines shaped like a rollout path
        (containing ``/rollout-`` and ending ``.jsonl``) are considered,
        so a dotfile that echoes cannot misdiagnose the probe.

        Every rollout under the tree is inspected: there is deliberately
        no mtime window (the retired marker era had one, and it is what
        let a subagent rollout look like this session's conversation).
        The filter is therefore ONE batched ``awk`` per ``find`` batch
        rather than a process per rollout: an agent user with months of
        codex history has thousands of rollouts, and this runs on every
        resume of a not-yet-bound session. The awk program is BEGIN-only
        and pulls one record per file with ``getline``, so it neither
        spawns per file nor walks a multi-megabyte rollout: the read cost
        per candidate is the one buffered block that record comes from, not
        the file. Two details are load-bearing:

        - The workspace path travels in the ENVIRONMENT, not through
          ``awk -v``, which applies escape processing to its value: a path
          containing a backslash would arrive as a DIFFERENT needle there,
          and a needle that silently changes is the one way this filter
          could mis-adopt rather than merely miss.
        - A file that will not read emits a sentinel that counts as an
          unnamed candidate (the picker), rather than being skipped. Same
          reasoning as raising on a failed ``find``: for an enumeration, a
          dropped candidate can turn "several" into one confident wrong
          adoption.
        """
        workspace = shlex.quote(self._workspace_path)
        awk = (
            f"BEGIN{{s={_awk_string(_CLI_SOURCE_NEEDLE)};"
            f'c="\\"cwd\\":\\"" ENVIRON["agw_ws"] "\\"";'
            f"for(i=1;i<ARGC;i++){{"
            f"r=(getline l < ARGV[i]);close(ARGV[i]);"
            f"if(r<0){{print {_awk_string(_UNREADABLE_SENTINEL)};continue}}"
            f"if(r>0&&index(l,s)&&index(l,c))print ARGV[i]}}}}"
        )
        inner = (
            f"[ -d {_SESSIONS_DIR} ] || exit {_NO_SESSIONS_DIR_EXIT}; "
            f"agw_ws=$(cd {workspace} 2>/dev/null && pwd -P) || exit {_CWD_RESOLVE_EXIT}; "
            f"export agw_ws; "
            f"find {_SESSIONS_DIR} -type f -name {shlex.quote('rollout-*.jsonl')} "
            f"-exec awk {shlex.quote(awk)} {{}} + 2>/dev/null || exit {_FIND_FAILED_EXIT}; "
            f"exit 0"
        )
        result = transport.run(f'"$SHELL" -lic {shlex.quote(inner)}', check=False)
        if result.returncode == _NO_SESSIONS_DIR_EXIT:
            return _Layer2((), False)  # codex never ran on this target
        if result.returncode == _CWD_RESOLVE_EXIT:
            raise StateError(
                f"session '{self._session_name}': could not resolve the "
                f"workspace directory ({self._workspace_path}) on "
                f"{self._target_label} to filter codex rollouts by; refusing "
                f"to guess resume-vs-launch.",
                entity_kind="session",
                entity_name=self._session_name,
                hint="Check that the workspace directory exists on the launch target and retry.",
            )
        if result.returncode != 0:
            raise StateError(
                f"session '{self._session_name}': could not probe for codex "
                f"rollouts on {self._target_label} (exit {result.returncode}); "
                f"refusing to guess resume-vs-launch.",
                entity_kind="session",
                entity_name=self._session_name,
                hint=_probe_hint(result.returncode),
            )
        ids: list[str] = []
        unnamed = False
        for line in result.stdout.splitlines():
            path = line.strip()
            if path == _UNREADABLE_SENTINEL:
                unnamed = True  # a candidate we could not read: the human decides
                continue
            if "/rollout-" not in path or not path.endswith(".jsonl"):
                continue  # login-shell dotfile noise, not a probe answer
            match = _ROLLOUT_ID_RE.search(path)
            if match is None:
                unnamed = True  # a candidate we cannot name: the human decides
                continue
            if match.group(1) not in ids:
                ids.append(match.group(1))
        return _Layer2(tuple(ids), unnamed)

    def _probe_target(self, transport: Transport) -> None:
        """Readiness proves only that ``codex`` is installed; it never
        inspects session state (detection is an op-time concern)."""
        require_commands(
            ("codex",),
            transport,
            harness_integration_name=self.name,
            template_name=self.owner_name,
            session_name=self._session_name,
            target_label=self._target_label,
        )
