"""The codex ``notify`` recorder: the shell artifact that tells Agentworks
which codex conversation a session's pane is in.

This is the primary half of the codex integration's session addressing (the
decision tree that consumes it lives in ``harness_integration.py``). Codex
mints its own session ids and offers no way to pin one, so instead of
inferring which rollout on disk belongs to a session, every generated launch
passes ``-c notify=[<recorder>, <destination>]`` and codex reports the id
itself: it runs the recorder after every completed turn with the turn's
``thread-id``, and the recorder writes that uuid where the next op will read
it.

Everything here is target-side text: paths are ``$HOME``-relative tails that
the LAUNCHING shell expands (never this process, which does not know the
launch user's home), and each builder returns one shell word or one command
fragment for splicing into the pane command.

Verified 2026-08-04 against codex-cli 0.146.0, driving real completed turns:

- codex hands a notify program ONE argv argument, the JSON payload, AFTER any
  extra elements of the ``notify`` array. ``notify=["<prog>","<extra>"]`` runs
  ``<prog> <extra> <payload>``, which is what lets ONE shared recorder serve
  every session: ``$1`` is the session's destination file, ``$2`` the payload.
- ``--strict-config`` accepts the multi-element array and rejects a bare
  string, so the shape is drift-guarded like every other emitted config key.
- codex spawns the program itself, with no shell, so the paths in the array
  must be absolute by the time codex sees them.
"""

from __future__ import annotations

import shlex

# Our own per-user directory on the launch target, holding the recorder
# script and the per-session ``.thread`` files it writes.
AGW_CODEX_TAIL = ".agentworks/codex"
# The filename carries the recorder's CONTRACT VERSION, not the Agentworks
# version: ``$1``-destination-plus-``$2``-payload is an interface codex holds
# a path to for the life of a session. A future recorder that takes different
# arguments gets ``-v2``, so an Agentworks upgrade mid-session can never
# reshape the script a running codex is about to invoke.
_RECORDER_TAIL = f"{AGW_CODEX_TAIL}/record-thread-v1.sh"
# The atomic-provisioning staging path; the launching shell appends its own
# ``$$`` so two concurrent launches under one user cannot collide.
_RECORDER_STAGE_TAIL = f"{AGW_CODEX_TAIL}/.record-thread-v1.sh."

# The recorder script, provisioned verbatim by every launch (see
# ``provision_fragment`` for why an identical-content overwrite is the right
# shape for a per-user file). POSIX sh with no jq/python assumptions, and
# deliberately free of single quotes, so each line survives ``shlex.quote``
# without an ``'\''`` thicket in the generated pane command.
#
# Three rules earn their lines:
#
# - **Only record a payload carrying a ``client`` key.** A subagent's
#   completed turn fires the PARENT's notify hook with the SUBAGENT's
#   thread-id and no ``client`` key; recording it would bind the session to a
#   subagent conversation, the exact splice this design kills. The
#   discriminator is codex-internal and undocumented; re-verify on codex major
#   bumps.
# - **Read the thread-id STRUCTURALLY, and take the FIRST one in the payload.**
#   The payload is split on JSON field and object boundaries (``tr``) so the
#   pattern can anchor on a whole field, then ``head -n 1`` keeps the first
#   match. Both halves matter. Anchoring is what stops a text field from
#   forging the needle: an assistant message quoting ``"thread-id":"<uuid>"``
#   arrives JSON-escaped as ``\\"thread-id\\":\\"...``, which no longer starts
#   the field, and the same holds for the ``client`` needle. First-match is
#   what stops a NESTED structural ``thread-id`` (codex is free to add one)
#   from winning, as the previous greedy ``.*`` form would have let it.
#
#   Be precise about what first-match buys, because it is BYTE ORDER, not
#   nesting depth: it is the payload's own thread-id because 0.146.0 emits
#   that field before any nested object, and a future codex nesting one
#   EARLIER would bind that instead. Re-verify on codex major bumps. The
#   failure mode is a recoverable wrong binding rather than a corrupt one:
#   the id names a real conversation, so the operator sees the wrong one in
#   the pane and the picker recovers it.
# - **Never break the turn.** Every path exits 0. Codex ignores a notify
#   failure anyway, but relying on that would put the operator's turn at the
#   mercy of our bookkeeping.
_UUID_BRE = "[0-9a-fA-F]\\{8\\}-[0-9a-fA-F]\\{4\\}-[0-9a-fA-F]\\{4\\}-[0-9a-fA-F]\\{4\\}-[0-9a-fA-F]\\{12\\}"
_RECORDER_LINES: tuple[str, ...] = (
    "#!/bin/sh",
    "# Agentworks codex harness integration: records the codex thread-id of each",
    "# completed turn so the next session op can resume that conversation.",
    "# Provisioned (and overwritten) by every Agentworks codex launch: edit the",
    "# generator in agentworks/plugins/codex/recorder.py, not this file.",
    "# $1 is the destination file; $2 is the agent-turn-complete JSON payload.",
    '[ -n "$1" ] && [ -n "$2" ] || exit 0',
    # A subagent turn fires the parent's notify without a client key.
    'case "$2" in *"\\"client\\":"*) ;; *) exit 0 ;; esac',
    # One field per line, then match a WHOLE field, then take the first.
    f't=$(printf %s "$2" | tr ",{{}}" "\\n\\n\\n" '
    f'| sed -n "s/^\\"thread-id\\":\\"\\({_UUID_BRE}\\)\\"$/\\1/p" | head -n 1)',
    '[ -n "$t" ] || exit 0',
    # Atomic: a reader never sees a half-written id, and last write wins.
    'printf "%s\\n" "$t" 2>/dev/null > "$1.$$" && mv -f "$1.$$" "$1" 2>/dev/null || rm -f "$1.$$" 2>/dev/null',
    "exit 0",
)


def home_word(tail: str) -> str:
    """A ``$HOME``-relative path as ONE shell word, with ``$HOME`` expanded
    by the target-side shell and the tail ``shlex.quote``-d (adjacent words
    concatenate in sh, so the pair stays a single path token even for a
    session name that would need quoting)."""
    return '"$HOME"/' + shlex.quote(tail)


def thread_tail(session_name: str) -> str:
    """The recorder's destination for one session, ``$HOME``-relative.

    A session name is REUSABLE, so the path alone guarantees nothing: a
    deleted session's file outlives it and the next namesake finds it. What
    makes this safe is the two rules around it, each with its own tests. The
    file is only ever written with an id codex itself reported for a
    conversation live in that session's pane, and it is read only by
    ``resume``, never by ``create``, which deletes it instead (see
    ``CodexIntegration.start``).
    """
    return f"{AGW_CODEX_TAIL}/{session_name}.thread"


def notify_value_word(session_name: str) -> str:
    """The ``notify`` config value as ONE shell word: a TOML array of the
    recorder path and this session's destination file.

    Quoting has three layers to survive (this word, the ``sh -c`` wrapper,
    and the pane's own ``$SHELL -lic`` wrapper), so it is built the way
    :func:`home_word` is: fixed skeleton pieces ``shlex.quote``-d, ``"$HOME"``
    left shell-active in between, everything concatenated into a single word.
    Naive nesting (a double-quoted TOML array inside the generated command)
    mangles the array; this shape reaches codex as one argv token whose
    ``$HOME`` the launching shell expanded, which the recorder needs because
    codex spawns the program by absolute path with no shell of its own.
    """
    return (
        shlex.quote('notify=["')
        + home_word(_RECORDER_TAIL)
        + shlex.quote('","')
        + home_word(thread_tail(session_name))
        + shlex.quote('"]')
    )


def provision_fragment() -> str:
    """The shell fragment that installs the recorder before ``exec codex``:
    stage it, make it executable, then ``mv`` it into place (so codex can
    never see a half-written or non-executable recorder), removing the
    staging file if any step fails.

    Every launch overwrites it with byte-identical content, which is what
    keeps a per-USER file inside the session-scoped effects rule (the
    harness-integration contract's "A Note on Scope"): no session can change
    what another session's recorder does, and a recorder from an older
    Agentworks cannot outlive an upgrade. Provisioning failure is not fatal by
    design: codex treats a missing notify program as a silent no-op, so the
    launch proceeds and the session falls back to discovery.
    """
    stage = home_word(_RECORDER_STAGE_TAIL) + '"$$"'
    recorder = home_word(_RECORDER_TAIL)
    lines = " ".join(shlex.quote(line) for line in _RECORDER_LINES)
    return (
        f"mkdir -p {home_word(AGW_CODEX_TAIL)} && printf '%s\\n' {lines} > {stage} "
        f"&& chmod +x {stage} && mv -f {stage} {recorder} || rm -f {stage}"
    )
