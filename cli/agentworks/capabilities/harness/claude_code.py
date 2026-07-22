"""The ``claude-code`` harness: run Claude Code as the session workload,
resuming its transcript when one exists and launching fresh otherwise.

Config vocabulary (all optional): ``permission_mode`` and ``model`` map to
the ``--permission-mode`` / ``--model`` flags verbatim, and ``extra_args``
is a list of raw argv tokens appended last (the operator escape hatch for
any flag the harness does not model). See ``claude-code-lld.md``.

Addressing uses a stored per-session Claude session id (a v4 uuid) kept in
the harness-state blob under ``session_id``: minted once on the first
``start`` and read back on every ``restart``, because the session manager
persists the blob to the session row after each op. Resume-vs-launch is an
op-time existence probe for that id's transcript on disk (slug-independent,
so it does not reconstruct Claude's brittle cwd-slug directory name): a
transcript present means the session is resumable, so ``--resume``; absent
means launch fresh with ``--session-id``. That file-presence boundary was
empirically confirmed to equal Claude's own resume boundary, so neither a
blind resume of a nonexistent session nor a resume of an unresumable stub
is possible.
"""

from __future__ import annotations

import shlex
import uuid
from typing import TYPE_CHECKING, ClassVar

from agentworks.capabilities.harness.base import Harness, require_commands
from agentworks.errors import ConfigError, StateError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.capabilities.base import RunContext
    from agentworks.resources.reference import ConfigReference
    from agentworks.transports import Transport

_CLAUDE_CODE_FIELDS = {
    "permission_mode",
    "model",
    "extra_args",
    "pass_oauth_token",
    "oauth_token_secret",
}

# The env var Claude Code reads a long-lived OAuth token from
# (``claude setup-token``, one-year lifetime). Exact spelling, per
# code.claude.com/docs/en/authentication.
_OAUTH_TOKEN_ENV_VAR = "CLAUDE_CODE_OAUTH_TOKEN"

# The declared secret the token maps to when ``oauth_token_secret`` is
# not set explicitly.
_DEFAULT_OAUTH_TOKEN_SECRET = "claude-code-oauth-token"


def _oauth_secret_name(config: Mapping[str, object]) -> str:
    """The secret name the OAuth token is mapped to: the explicit
    ``oauth_token_secret`` when set to a non-empty string, else the
    default. Shared by :meth:`ClaudeCodeHarness.validate_config` (which
    DECLARES the reference) and the instance's
    :meth:`ClaudeCodeHarness.env_contributions` (which READS the value),
    so both name the same secret.
    """
    name = config.get("oauth_token_secret")
    if isinstance(name, str) and name:
        return name
    return _DEFAULT_OAUTH_TOKEN_SECRET

# The transcript's config root. ``CLAUDE_CONFIG_DIR`` is the CLI's own
# override env var (confirmed present in the v2.1.205 binary); the default
# is ``$HOME/.claude``. Expanded by the target-side shell inside the
# find probe, never here.
_PROJECTS_DIR = "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/projects"


class ClaudeCodeHarness(Harness):
    """Runs Claude Code, resuming or launching fresh per on-disk state."""

    name: ClassVar[str] = "claude-code"
    description: ClassVar[str] = "Run Claude Code, resuming its session when one exists"

    # Set by _resume_or_launch on each start/restart; drives launch_note().
    # None until the op runs (nothing decided yet).
    _resumed: bool | None = None

    @classmethod
    def validate_config(
        cls, owner: str, config: Mapping[str, object]
    ) -> tuple[ConfigReference, ...]:
        """Shape-and-vocabulary only (FRD R4): unknown fields raise; each
        present field is type-checked. The ``--permission-mode`` / ``--model``
        CHOICE sets are Claude-owned and drift between releases, so the
        VALUE is forwarded unvalidated (an invalid one surfaces as Claude's
        own startup error in the pane).

        Returns the config-implied resource references: when
        ``pass_oauth_token`` is enabled, a single secret reference for the
        (defaulted) token secret; otherwise ``()``. The conditionality is
        automatic, because the declaration derives from config, so an
        unmapped token secret fails at preflight for free (issue #220).
        """
        unknown = sorted(set(config) - _CLAUDE_CODE_FIELDS)
        if unknown:
            raise ConfigError(
                f"{owner}: unknown claude-code harness field(s): "
                f"{', '.join(unknown)}"
            )
        for field_name in ("permission_mode", "model"):
            value = config.get(field_name)
            if value is not None and not isinstance(value, str):
                raise ConfigError(f"{owner}.{field_name} must be a string")
        extra_args = config.get("extra_args")
        if extra_args is not None and (
            not isinstance(extra_args, list)
            or not all(isinstance(item, str) for item in extra_args)
        ):
            raise ConfigError(
                f"{owner}.extra_args must be a list of strings"
            )
        pass_oauth_token = config.get("pass_oauth_token")
        if pass_oauth_token is not None and not isinstance(pass_oauth_token, bool):
            raise ConfigError(f"{owner}.pass_oauth_token must be a boolean")
        oauth_token_secret = config.get("oauth_token_secret")
        if oauth_token_secret is not None and not isinstance(oauth_token_secret, str):
            raise ConfigError(f"{owner}.oauth_token_secret must be a string")
        # An orphan secret name (a name with nothing consuming it) is a
        # misconfiguration, surfaced loudly. This also catches the
        # child-wins inheritance wrinkle: a child setting
        # ``pass_oauth_token = false`` over a parent that set
        # ``oauth_token_secret`` yields exactly this error on the merged
        # blob (issue #220), which is honest, intended behavior.
        if "oauth_token_secret" in config and pass_oauth_token is not True:
            raise ConfigError(
                f"{owner}.oauth_token_secret is set but pass_oauth_token is "
                f"not true; a token secret name with nothing consuming it is "
                f"a misconfiguration. Enable pass_oauth_token, or drop "
                f"oauth_token_secret."
            )
        if pass_oauth_token is True:
            # Function-local runtime import (the layering pattern the other
            # secret-declaring capabilities use, e.g. proxmox): importing
            # the resources package at module load would pull sessions in
            # transitively and trip the capability-layer import guard.
            from agentworks.resources.reference import ConfigReference

            return (
                ConfigReference(
                    kind="secret",
                    name=_oauth_secret_name(config),
                    usage=f"the {_OAUTH_TOKEN_ENV_VAR} env var",
                ),
            )
        return ()

    def start(self, ctx: RunContext) -> str:
        """The pane command for ``session create``: resume the stored
        session if its transcript exists, else launch fresh."""
        return self._resume_or_launch(ctx)

    def restart(self, ctx: RunContext) -> str:
        """The pane command for ``session restart``: symmetric with
        :meth:`start`. The orchestrator kills the old tmux BEFORE calling
        this (R7), so the probe decides resume-vs-launch with the old
        process already dead."""
        return self._resume_or_launch(ctx)

    def launch_note(self) -> str | None:
        if self._resumed is None:
            return None
        return (
            "Existing Claude Code session found. Resuming..."
            if self._resumed
            else "No existing Claude Code session. Starting a new one..."
        )

    def env_contributions(self, ctx: RunContext) -> dict[str, str]:
        """When ``pass_oauth_token`` is enabled, deliver the mapped
        secret's value as ``CLAUDE_CODE_OAUTH_TOKEN`` so a harness-launched
        Claude skips the interactive login step. Empty otherwise.

        The value was already resolved by the graph's boundary pass;
        ``ctx.secret`` reads it from the scoped view (the name was declared
        by :meth:`validate_config`, so the session node's ``secret_refs()``
        covers it, and the hook never touches a resolver). Riding the env
        channel (not the pane command string) keeps the token out of
        ``/proc/*/cmdline``, ``ps``, and tmux's ``pane_start_command``.
        """
        if self.config.get("pass_oauth_token") is not True:
            return {}
        return {_OAUTH_TOKEN_ENV_VAR: ctx.secret(_oauth_secret_name(self.config))}

    def _resume_or_launch(self, ctx: RunContext) -> str:
        """Read (or mint) the stored session id, probe the launch target
        for its transcript, and return the single ``sh -c`` pane command
        that echoes the visible decision and ``exec``s ``claude``."""
        sid = self._session_id()
        launch_target = ctx.admin_target() if self._admin else ctx.agent_target()
        resume = launch_target is not None and self._transcript_exists(launch_target, sid)
        self._resumed = resume

        if resume:
            identity = ["--resume", sid]
            msg = (
                f"agentworks harness (claude-code): resuming session "
                f"{self._session_name}"
            )
        else:
            identity = ["--session-id", sid]
            msg = (
                f"agentworks harness (claude-code): starting new session "
                f"{self._session_name}"
            )
        tokens = [*identity, "--name", self._session_name, *self._config_flags()]
        argv = " ".join(shlex.quote(token) for token in tokens)
        # A single ``sh -c`` so the whole thing survives the ``exec``
        # wrapping the tmux pane applies (``exec`` takes one simple
        # command): the login shell execs this sh, which echoes then
        # execs claude, so the pane becomes Claude. The message and the
        # generated argv carry no ``{{word}}`` tokens, so the core
        # template-var substitution does not mangle them.
        inner = f"echo {shlex.quote(msg)}; exec claude {argv}"
        return f"sh -c {shlex.quote(inner)}"

    def _session_id(self) -> str:
        """The stored Claude session id, minted (and recorded in the state
        blob) on first use. A v4 uuid: Claude accepts any valid uuid at
        ``--session-id``, and global uniqueness keeps the transcript probe
        slug-independent. The manager persists ``self.state`` after the op,
        so a minted id survives to the next restart."""
        sid = self._state.get("session_id")
        if not isinstance(sid, str):
            # If the op raises after this mint but before the manager
            # persists the blob, the id is lost. That window is benign: it
            # can only happen on a pre-migration session's FIRST restart
            # (a create always persists its minted id with the new row);
            # there, neither the old nor a re-minted id has a transcript,
            # so the retry launches fresh either way, no history is lost.
            sid = str(uuid.uuid4())
            self._state["session_id"] = sid
        return sid

    def _config_flags(self) -> list[str]:
        """The managed flags then ``extra_args``, each an argv token.
        ``extra_args`` is appended verbatim last so it can carry any flag
        the harness does not model (FRD R4)."""
        tokens: list[str] = []
        permission_mode = self.config.get("permission_mode")
        if isinstance(permission_mode, str):
            tokens += ["--permission-mode", permission_mode]
        model = self.config.get("model")
        if isinstance(model, str):
            tokens += ["--model", model]
        extra_args = self.config.get("extra_args")
        if isinstance(extra_args, list):
            tokens += [item for item in extra_args if isinstance(item, str)]
        return tokens

    def _transcript_exists(self, transport: Transport, sid: str) -> bool:
        """True iff the stored session's transcript (``<sid>.jsonl``) exists
        under the projects dir on the launch target. Slug-independent
        (``find`` matches under ANY project directory); shell-neutral
        (``find ... -print -quit | grep -q .`` has no glob-nomatch
        divergence). Runs through ``$SHELL -lic`` like the readiness probe.

        On restart the orchestrator has already killed the old session, but
        no flush wait is needed: Claude writes transcript turns to the
        ``.jsonl`` incrementally as work happens (not flushed on exit), so a
        killed session's history is already on disk when this probe runs.

        The exit code is read, not just ``.ok``, to keep a probe that could
        not EXECUTE from masquerading as "no transcript": ``grep -q`` exits
        0 for a match and 1 for none, so any other exit (an SSH failure's
        255, a shell that could not start) means the probe never ran. Guessing
        "fresh" there would launch ``--session-id <reserved-uuid>``, which
        Claude rejects as already-in-use on a real session's restart and the
        pane fails to start, so a probe failure raises instead of guessing."""
        needle = shlex.quote(f"{sid}.jsonl")
        inner = f"find {_PROJECTS_DIR} -name {needle} -print -quit 2>/dev/null | grep -q ."
        result = transport.run(f'"$SHELL" -lic {shlex.quote(inner)}', check=False)
        if result.returncode == 0:
            return True  # transcript on disk: resume
        if result.returncode == 1:
            return False  # the probe ran, no match: launch fresh
        raise StateError(
            f"session '{self._session_name}': could not probe for the Claude "
            f"transcript on {self._target_label} (exit {result.returncode}); "
            f"refusing to guess resume-vs-launch.",
            entity_kind="session",
            entity_name=self._session_name,
            hint="Retry once the launch target is reachable.",
        )

    def _probe_target(self, transport: Transport) -> None:
        """Readiness proves only that ``claude`` is installed; it never
        inspects session state (detection is an op-time concern)."""
        require_commands(
            ("claude",),
            transport,
            harness_name=self.name,
            template_name=self.owner_name,
            session_name=self._session_name,
            target_label=self._target_label,
        )
