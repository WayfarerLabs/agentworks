"""The ``codex`` harness integration: run Codex as the session workload, resuming its
rollout when one exists and launching fresh otherwise.

Config vocabulary (all optional): ``model``, ``sandbox``, ``approval_policy``,
and ``profile`` map to the ``-m`` / ``-s`` / ``-a`` / ``-p`` flags verbatim;
``network`` (bool) forwards to the ``sandbox_workspace_write.network_access``
config key via ``-c``; ``approvals_reviewer`` (str) forwards to the
``approvals_reviewer`` config key via ``-c`` (who adjudicates approval
escalations: codex documents ``user``, the default, and ``auto_review``, its
risk-based reviewer subagent); ``writable_dirs`` (list) emits one ``--add-dir``
per entry (union-merged across template inheritance, like ``shell``'s
``required_commands``); ``web_search`` (bool) emits ``--search``;
``disable_strict_config`` (bool, default false) suppresses the
``--strict-config`` the harness integration otherwise always emits; and ``extra_args`` is
a list of raw argv tokens appended last (the operator escape hatch for any
flag the harness integration does not model). The integration contract and worked-example guidance
live in ``agentworks/capabilities/harness_integration/README.md``; this module keeps the
Codex-specific command and state invariants next to their implementation.

Addressing is discover-and-store (the harness integration guide's rule 1, second form):
codex offers no ``--session-id`` analog, so the harness integration never mints an id.
Instead it stores the codex-minted session uuid in its state namespace under
``session_id``, populated by DISCOVERY anchored on a stored marker: a fresh
launch mints a nonce marker filename
(``~/.agentworks/codex/<session-name>-<nonce>.launch`` on the launch target),
records it in the state blob under ``discovery_marker``, and touches the file
just before ``exec codex``. On a later op holding an anchor and no id, the
harness integration probes for rollout files newer than that marker whose recorded
session cwd is this session's workspace directory, and adopts the single
candidate's uuid (zero candidates launch fresh again; multiple raise rather
than guess, since adopting the wrong id would splice one session's
conversation into another). A blob with NO stored anchor has definitively
nothing to discover, so no probe runs at all: a brand-new session (or a
namesake recreated after a delete) can never adopt some earlier session's
conversation off a leftover marker file.

Resume-vs-launch for a stored id is an op-time existence probe for the id's
rollout file on disk: the rollout-file boundary was empirically confirmed to
equal codex's own resume boundary. An archived rollout (moved to
``archived_sessions/`` by ``codex archive``) is deliberately treated as
not-resumable: auto-unarchiving would silently reverse an explicit operator
action, so the harness integration drops the stale id and launches fresh, leaving the
archived history recoverable manually.

Discovery has two accepted residual windows: concurrent launches by the same user in the same
working directory can produce multiple candidates and fail loudly rather than guess, and filesystem
mtime granularity can place a rollout on the launch marker boundary. Keeping these constraints here
makes them durable beside the code that enforces the safe failure behavior.
"""

from __future__ import annotations

import re
import shlex
import uuid
from typing import TYPE_CHECKING, ClassVar, Literal

from agentworks.capabilities.harness_integration.base import HarnessIntegration, require_commands
from agentworks.errors import ConfigError, StateError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.capabilities.base import RunContext
    from agentworks.resources.reference import ConfigReference
    from agentworks.transports import Transport

_CODEX_FIELDS = {
    "model",
    "sandbox",
    "approval_policy",
    "profile",
    "network",
    "approvals_reviewer",
    "writable_dirs",
    "web_search",
    "disable_strict_config",
    "extra_args",
}

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

# Every string-typed field: the flag-mapped four plus the -c-forwarded
# approvals_reviewer (validate type-checks them through one loop).
_STR_FIELDS: tuple[str, ...] = (*(name for name, _flag in _FLAG_FIELDS), "approvals_reviewer")


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


def _as_str_list(value: object) -> list[str] | None:
    """Narrow a merge-time list field: an ABSENT value is a clean empty
    list; a fully-string list passes through; anything else returns
    ``None`` (unclean). ``merge_config`` runs on raw declared blobs (the
    resolver merges before the final validate), so an unclean side must
    NOT be filtered into a valid-looking union: laundering would hide the
    bad entry from the merged-blob ``validate`` pass. The caller skips
    the union instead, leaving the raw value for ``validate`` to reject.
    """
    if value is None:
        return []
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return list(value)
    return None


def _append_dedupe(target: list[str], source: list[str]) -> list[str]:
    """Append source items to target, skipping dupes. Preserves order.

    A per-domain copy of the trivial merge helper (``shell.py`` carries
    its own for ``required_commands``), per the sanctioned copy-per-domain
    shape.
    """
    seen = set(target)
    result = list(target)
    for item in source:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


# The rollout root. ``CODEX_HOME`` is the CLI's own override env var
# (honored by codex-cli 0.146.0); the default is ``$HOME/.codex``.
# Expanded by the target-side shell inside the probes, never here; kept
# double-quoted because it is interpolated into shell commands as one word.
_SESSIONS_DIR = '"${CODEX_HOME:-$HOME/.codex}/sessions"'

# The per-session launch-marker directory, under the launch user's home
# (target-side expansion, one shell word).
_MARKER_DIR = '"$HOME"/.agentworks/codex'

# Rollout files are ``sessions/<Y>/<M>/<D>/rollout-<timestamp>-<uuid>.jsonl``
# with the codex-minted session uuid embedded verbatim at the tail; discovery
# extracts it from each candidate path.
_ROLLOUT_ID_RE = re.compile(
    r"rollout-.+-([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\.jsonl$"
)

# The discovery probe's distinct exit codes, chosen apart from find's own 1
# and the shell's 2 so a probe that FAILED can never masquerade as a
# definitive answer (rule 4: a probe that could not run is not a probe that
# found nothing). 3 is the one definitive-fresh sentinel; 4/5/6 are raise
# codes that name their failed precondition.
_NO_SESSIONS_DIR_EXIT = 3  # sessions dir absent: codex never ran here
_MARKER_MISSING_EXIT = 4  # anchor stored but its file is gone: raise
_CWD_RESOLVE_EXIT = 5  # workspace dir could not be canonicalized: raise
_FIND_FAILED_EXIT = 6  # find itself failed (not a mere no-match): raise


class CodexIntegration(HarnessIntegration):
    """Runs Codex, resuming or launching fresh per on-disk state."""

    name: ClassVar[str] = "codex"
    description: ClassVar[str] = "Run Codex, resuming its session when one exists"

    # Set by _resume_or_launch on each start/restart; drives launch_note().
    # None until the op runs (nothing decided yet).
    _decision: Literal["resumed", "adopted", "stale", "fresh"] | None = None

    @classmethod
    def dependencies(cls, owner: str, config: Mapping[str, object]) -> tuple[ConfigReference, ...]:
        """``codex`` implies no resource reference, so its edge set is
        empty (total, non-throwing per the ``dependencies`` contract)."""
        return ()

    @classmethod
    def merge_config(cls, base: Mapping[str, object], child: Mapping[str, object]) -> dict[str, object]:
        """Same-harness integration inheritance merge: scalars and bools child-win via
        the shallow default; ``writable_dirs`` unions append-dedupe (it is
        an additive grant list, like ``shell``'s ``required_commands``: a
        child adding one dir must not silently drop the parent's).
        ``extra_args`` deliberately child-wins (an escape hatch is an
        override, not an accumulation), matching ``claude-code``.

        The union runs only when BOTH sides are clean lists of strings:
        the merge sees raw declared blobs, and filtering a mixed list
        into a valid-looking union would hide the invalid entry from the
        merged-blob ``validate`` pass. An unclean side falls through to
        the shallow merge, so ``validate`` still rejects it."""
        merged = {**base, **child}
        base_dirs = _as_str_list(base.get("writable_dirs"))
        child_dirs = _as_str_list(child.get("writable_dirs"))
        if base_dirs is not None and child_dirs is not None:
            union = _append_dedupe(base_dirs, child_dirs)
            if union:
                merged["writable_dirs"] = union
        return merged

    @classmethod
    def validate(cls, owner: str, config: Mapping[str, object]) -> None:
        """Shape-and-vocabulary only: unknown fields raise; each present
        field is type-checked. The flag VALUES are codex-owned choice sets
        and forward unvalidated (an invalid one surfaces as codex's own
        startup error in the pane).
        """
        unknown = sorted(set(config) - _CODEX_FIELDS)
        if unknown:
            raise ConfigError(f"{owner}: unknown codex harness integration field(s): {', '.join(unknown)}")
        for field_name in _STR_FIELDS:
            value = config.get(field_name)
            if value is not None and not isinstance(value, str):
                raise ConfigError(f"{owner}.{field_name} must be a string")
        for field_name in ("network", "web_search", "disable_strict_config"):
            value = config.get(field_name)
            if value is not None and not isinstance(value, bool):
                raise ConfigError(f"{owner}.{field_name} must be a boolean")
        for field_name in ("writable_dirs", "extra_args"):
            value = config.get(field_name)
            if value is not None and (not isinstance(value, list) or not all(isinstance(item, str) for item in value)):
                raise ConfigError(f"{owner}.{field_name} must be a list of strings")

    def start(self, ctx: RunContext) -> str:
        """The pane command for ``session create``: resume the stored (or
        discovered) session if its rollout exists, else launch fresh."""
        return self._resume_or_launch(ctx)

    def resume(self, ctx: RunContext) -> str:
        """The pane command for ``session resume``: symmetric with
        :meth:`start`. The orchestrator kills the old tmux BEFORE calling
        this, so the probes decide with the old process already dead."""
        return self._resume_or_launch(ctx)

    def launch_note(self) -> str | None:
        if self._decision is None:
            return None
        return {
            "resumed": "Existing Codex session found. Resuming...",
            "adopted": (
                "Discovered the Codex session from the previous launch (best-effort match; "
                "concurrent codex use under this user and workspace can mislead it). "
                "Adopting and resuming..."
            ),
            "stale": "Previous Codex session is archived or gone. Starting a new one...",
            "fresh": "No existing Codex session. Starting a new one...",
        }[self._decision]

    def _resume_or_launch(self, ctx: RunContext) -> str:
        """The op decision, from the tool's own durable state on the launch
        target:

        - stored id whose rollout exists: resume it;
        - stored id whose rollout is gone (archived or deleted): drop the
          stale id and launch fresh (the next op's discovery adopts the
          codex-minted replacement);
        - no stored id but a stored discovery anchor: run discovery
          against that marker, adopting a single candidate, launching
          fresh on none, raising on several;
        - neither: definitively nothing to discover (a brand-new session,
          or a namesake recreated after a delete), so launch fresh with
          no probe at all.

        A stored ``session_id`` of the wrong type is garbage this harness integration
        never wrote (the blob is only as trustworthy as the DB it came
        from): it is swept out of the namespace rather than left to
        confuse a later read. Every path returns a single ``sh -c`` pane
        command that echoes the visible decision and ``exec``s ``codex``.
        """
        launch_target = ctx.admin_target() if self._admin else ctx.agent_target()
        if launch_target is None:
            # Unlike claude-code (which keeps its minted id either way, so
            # guessing "fresh" is lossless), a codex fresh launch drops
            # the stored id and replaces the discovery anchor; guessing
            # here could orphan a resumable conversation, so raise.
            raise StateError(
                f"session '{self._session_name}': the op context carries no "
                f"launch target to probe codex session state on; refusing "
                f"to guess resume-vs-launch.",
                entity_kind="session",
                entity_name=self._session_name,
                hint="Retry once the launch target is reachable.",
            )
        stored = self._state.get("session_id")
        if "session_id" in self._state and not isinstance(stored, str):
            del self._state["session_id"]  # sweep garbage; never this harness integration's write
            stored = None
        sid = stored if isinstance(stored, str) and stored else None
        if sid is not None:
            if self._rollout_exists(launch_target, sid):
                self._decision = "resumed"
                return self._resume_command(
                    sid,
                    msg=f"agentworks harness integration (codex): resuming session {self._session_name}",
                )
            # The rollout is gone (archived or deleted): not resumable, by
            # the pinned archived policy. Drop the stale id so the NEXT
            # op's discovery can adopt the replacement this launch mints.
            del self._state["session_id"]
            self._decision = "stale"
            return self._fresh_command(
                msg=f"agentworks harness integration (codex): previous codex session archived or "
                f"gone; starting new session {self._session_name}"
            )
        anchor = self._state.get("discovery_marker")
        if isinstance(anchor, str) and anchor:
            adopted = self._discover(launch_target, anchor)
            if adopted is not None:
                self._state["session_id"] = adopted
                del self._state["discovery_marker"]  # consumed; the pane rm -f's the file
                self._decision = "adopted"
                return self._resume_command(
                    adopted,
                    msg=f"agentworks harness integration (codex): adopted a discovered codex session; "
                    f"resuming session {self._session_name}",
                    consume_marker=anchor,
                )
        # No anchor (nothing was ever launched fresh here), or discovery
        # came back empty: launch fresh.
        self._decision = "fresh"
        return self._fresh_command(
            msg=f"agentworks harness integration (codex): starting new session {self._session_name}"
        )

    def _marker_word(self, anchor: str) -> str:
        """A stored anchor (a ``$HOME``-relative marker path like
        ``.agentworks/codex/<name>-<nonce>.launch``) as ONE shell word,
        with ``$HOME`` expanded by the target-side shell and the tail
        ``shlex.quote``-d (adjacent words concatenate in sh, so the pair
        stays a single path token even for a name needing quotes)."""
        return '"$HOME"/' + shlex.quote(anchor)

    def _resume_command(self, sid: str, *, msg: str, consume_marker: str | None = None) -> str:
        """The resume pane command: ``codex resume <sid>`` with
        ``-c tui.resume_cwd=current`` pinning the cross-cwd picker off
        deterministically (the pane has already cd-ed to the workspace
        dir, so "current" is always the right answer). On the adoption
        path ``consume_marker`` is the just-consumed anchor, whose file
        the pane removes so no dead marker file outlives its blob entry."""
        tokens = ["resume", sid, "-c", "tui.resume_cwd=current", *self._config_flags()]
        argv = " ".join(shlex.quote(token) for token in tokens)
        cleanup = f"rm -f {self._marker_word(consume_marker)}; " if consume_marker else ""
        # A single ``sh -c`` so the whole thing survives the ``exec``
        # wrapping the tmux pane applies (``exec`` takes one simple
        # command). The message and the generated argv carry no
        # ``{{word}}`` tokens, so the core template-var substitution does
        # not mangle them.
        inner = f"echo {shlex.quote(msg)}; {cleanup}exec codex {argv}"
        return f"sh -c {shlex.quote(inner)}"

    def _fresh_command(self, *, msg: str) -> str:
        """The fresh-launch pane command: mint a NONCE marker filename,
        record it in the state blob as the discovery anchor (the manager
        persists the blob before the pane runs), remove the previous
        anchor's file if one was stored (a prior fresh launch that never
        got used), touch the new marker, then ``exec codex`` with no
        positional prompt, so no wrapper-authored turn ever appears in
        the conversation.

        The nonce ties the on-disk marker to THIS blob entry: a leftover
        marker from a deleted namesake session can never be mistaken for
        ours, because discovery only ever probes the stored anchor. If
        the pane never runs (or the touch fails), the blob holds an
        anchor whose file does not exist, and the next op's discovery
        raises rather than guessing; the error's hint names the
        recovery."""
        old_anchor = self._state.get("discovery_marker")
        anchor = f".agentworks/codex/{self._session_name}-{uuid.uuid4().hex}.launch"
        self._state["discovery_marker"] = anchor
        cleanup = f"rm -f {self._marker_word(old_anchor)}; " if isinstance(old_anchor, str) and old_anchor else ""
        flags = self._config_flags()
        exec_codex = "exec codex" if not flags else "exec codex " + " ".join(shlex.quote(token) for token in flags)
        inner = (
            f"echo {shlex.quote(msg)}; "
            f"{cleanup}mkdir -p {_MARKER_DIR} && touch {self._marker_word(anchor)}; "
            f"{exec_codex}"
        )
        return f"sh -c {shlex.quote(inner)}"

    def _config_flags(self) -> list[str]:
        """The managed flags then ``extra_args``, each an argv token.
        ``extra_args`` is appended verbatim last so it can carry (or
        override) any flag the harness integration does not model.

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
        if self.config.get("disable_strict_config") is not True:
            tokens.append("--strict-config")
        for field_name, flag in _FLAG_FIELDS:
            value = self.config.get(field_name)
            if isinstance(value, str):
                tokens += [flag, value]
        network = self.config.get("network")
        if isinstance(network, bool):
            # Both directions forward explicitly: `false` overrides a
            # profile or config.toml that enabled network access.
            tokens += ["-c", f"{_NETWORK_KEY}={'true' if network else 'false'}"]
        approvals_reviewer = self.config.get("approvals_reviewer")
        if isinstance(approvals_reviewer, str):
            # Encoded as a TOML basic string: see _toml_basic_string for
            # why raw interpolation would be a silent-injection hole.
            tokens += ["-c", f"{_APPROVALS_REVIEWER_KEY}={_toml_basic_string(approvals_reviewer)}"]
        writable_dirs = self.config.get("writable_dirs")
        if isinstance(writable_dirs, list):
            for item in writable_dirs:
                if isinstance(item, str):
                    tokens += ["--add-dir", item]
        if self.config.get("web_search") is True:
            tokens.append("--search")
        extra_args = self.config.get("extra_args")
        if isinstance(extra_args, list):
            tokens += [item for item in extra_args if isinstance(item, str)]
        return tokens

    def _rollout_exists(self, transport: Transport, sid: str) -> bool:
        """True iff the stored session's rollout
        (``rollout-<timestamp>-<sid>.jsonl``, hence the ``*-<sid>.jsonl``
        glob) exists under the sessions dir on the launch target.
        Shell-neutral (the glob is quoted through to find); runs through
        ``$SHELL -lic`` like the readiness probe. ``archived_sessions/``
        is deliberately NOT probed: an archived session reports
        not-resumable and the harness integration launches fresh rather than silently
        reversing ``codex archive``.

        The exit code is read, not just ``.ok``, so a probe that could not
        EXECUTE never masquerades as "no rollout". The inner command keeps
        find's own failure distinguishable from a clean no-match: a
        missing sessions dir (codex never ran here) exits 1 up front; a
        printed match exits 0 (a found rollout is definitive even if find
        also stumbled elsewhere); a find that FAILED without printing one
        exits 6 rather than folding into "no rollout". Anything but
        {0, 1} (the 6, an SSH failure's 255, a shell that could not
        start) raises: guessing "fresh" would drop the stored id and
        orphan a resumable conversation."""
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
            return False  # the probe ran, no match: launch fresh
        raise StateError(
            f"session '{self._session_name}': could not probe for the codex "
            f"rollout on {self._target_label} (exit {result.returncode}); "
            f"refusing to guess resume-vs-launch.",
            entity_kind="session",
            entity_name=self._session_name,
            hint="Retry once the launch target is reachable.",
        )

    def _discover(self, transport: Transport, anchor: str) -> str | None:
        """Op-time discovery of the codex-minted session id, run only when
        the blob holds the stored anchor ``anchor`` and no id: list
        rollout files newer (by mtime) than the anchor's marker file whose
        recorded session cwd is this session's workspace directory, and
        adopt the single candidate's uuid. Returns ``None`` when the probe
        ran and nothing matched (the human never durably used the previous
        fresh launch in this workspace, so launching fresh again loses
        nothing) and raises on anything it cannot vouch for.

        One purposeful round-trip. The target-side command exits with a
        distinct code per precondition so a probe that FAILED can never
        masquerade as a definitive answer: sessions dir absent is the one
        definitive-fresh exit (3); the marker file missing while its
        anchor is stored raises (4: either the fresh pane never ran or
        someone removed the file, and both mean the anchor's account of
        history is broken); the workspace dir failing to canonicalize
        raises (5); find failing raises (6: for an ENUMERATION a partial
        listing is dangerous, since a missed candidate could turn
        "multiple" into a wrong single adoption, so unlike the existence
        probe no output is trusted from a failed find).

        The cwd filter compares each rollout's first JSONL line (its
        session_meta) against the workspace directory canonicalized
        TARGET-side via ``cd <workspace> && pwd -P``, so a logical-vs-
        physical symlink mismatch cannot exclude our own rollout.
        Verified against codex-cli 0.146.0 (decisions doc, "What the CLI
        actually provides"): codex serializes the session cwd as a
        PHYSICAL path, even when launched from a symlinked directory, in
        compact JSON (``"cwd":"<path>"`` with no spaces), which
        ``grep -F`` matches without parsing. The matched path is not
        JSON-escaped: a workspace path carrying a JSON-special character
        (a quote, a backslash) would fail the match and degrade to a
        fresh launch, never a mis-adoption; workspace names are
        validated to a safe character set, so this stays theoretical. The
        stdout parse also tolerates login-shell noise: only lines shaped
        like a rollout path (containing ``/rollout-`` and ending
        ``.jsonl``) are considered, so a dotfile that echoes cannot
        misdiagnose the probe."""
        marker = self._marker_word(anchor)
        workspace = shlex.quote(self._workspace_path)
        inner = (
            f"[ -f {marker} ] || exit {_MARKER_MISSING_EXIT}; "
            f"[ -d {_SESSIONS_DIR} ] || exit {_NO_SESSIONS_DIR_EXIT}; "
            f"w=$(cd {workspace} 2>/dev/null && pwd -P) || exit {_CWD_RESOLVE_EXIT}; "
            f"out=$(find {_SESSIONS_DIR} -type f -name {shlex.quote('rollout-*.jsonl')} "
            f"-newer {marker} -print 2>/dev/null) || exit {_FIND_FAILED_EXIT}; "
            f'[ -n "$out" ] || exit 0; '
            f"printf '%s\\n' \"$out\" | while IFS= read -r f; do "
            f'head -n 1 "$f" 2>/dev/null | grep -F -q "\\"cwd\\":\\"$w\\"" && printf \'%s\\n\' "$f"; '
            f"done; exit 0"
        )
        result = transport.run(f'"$SHELL" -lic {shlex.quote(inner)}', check=False)
        if result.returncode == _NO_SESSIONS_DIR_EXIT:
            return None  # codex never ran on this target: nothing to adopt
        if result.returncode == _MARKER_MISSING_EXIT:
            raise StateError(
                f"session '{self._session_name}': the stored codex launch "
                f"marker (~/{anchor}) is missing on {self._target_label}; "
                f"without it, discovery cannot tell whether a codex "
                f"conversation from the last launch would be orphaned, so "
                f"refusing to guess.",
                entity_kind="session",
                entity_name=self._session_name,
                hint=(
                    f"If this session has no codex conversation worth keeping "
                    f"(or you accept losing an undiscovered one), recreate the "
                    f"marker with `touch ~/{anchor}` on the launch target and "
                    f"retry; the next launch will start fresh."
                ),
            )
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
                hint="Retry once the launch target is reachable.",
            )
        candidates: list[str] = []
        for line in result.stdout.splitlines():
            path = line.strip()
            if "/rollout-" not in path or not path.endswith(".jsonl"):
                continue  # login-shell dotfile noise, not a probe answer
            match = _ROLLOUT_ID_RE.search(path)
            if match is None:
                # A rollout-shaped file without an embedded uuid is not a
                # session this harness integration can adopt OR safely ignore (ignoring
                # could turn "one real candidate" into a wrong adoption of
                # another); refuse to guess.
                raise StateError(
                    f"session '{self._session_name}': the codex rollout probe "
                    f"on {self._target_label} matched a file whose name does "
                    f"not embed a session id ({path!r}); refusing to guess "
                    f"what it is.",
                    entity_kind="session",
                    entity_name=self._session_name,
                    hint="Remove or rename the unexpected file under the codex sessions directory and retry.",
                )
            sid = match.group(1)
            if sid not in candidates:
                candidates.append(sid)
        if not candidates:
            return None  # the probe ran, no matching rollout: launch fresh
        if len(candidates) == 1:
            return candidates[0]
        raise StateError(
            f"session '{self._session_name}': found {len(candidates)} codex "
            f"rollouts newer than this session's launch marker in its "
            f"workspace directory on {self._target_label} "
            f"({', '.join(candidates)}); refusing to guess which one is this "
            f"session's conversation.",
            entity_kind="session",
            entity_name=self._session_name,
            hint=(
                f"Archive the rollouts that belong to other work (codex "
                f"archive <id>) and retry, or remove the marker file "
                f"(~/{anchor} on the launch target) to make the next op raise "
                f"a recoverable marker-missing error instead."
            ),
        )

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
