"""The shared "resource is now empty" offer/report helper.

When deleting a session (or, per issue #287, removing a session from a
console) leaves an underlying resource with nothing left in it, we handle
that resource with one consistent shape:

- interactive (no ``--yes``): OFFER to delete it, regardless of provenance.
- ``--yes`` and this command CREATED the resource: auto-delete it, no prompt.
- ``--yes`` otherwise: REPORT it is now empty and name the manual delete
  command, but leave it in place.
- if the delete itself raises :class:`AgentworksError`: warn and continue
  rather than aborting a command whose primary work already succeeded (the
  session / member is already gone).

:func:`cleanup_now_empty_resource` is the single home for that shape. It was
extracted from three near-identical copies in the sessions manager (the
console-cascade block plus the workspace and agent cleanups); issue #287's
console ``remove-sessions`` empty-console offer is the intended next consumer
and should call it as-is with ``created=False`` (a console is never
session-created, so it never auto-deletes under ``--yes``).

The module imports only :mod:`agentworks.output` and
:class:`agentworks.errors.AgentworksError`, so both ``sessions.manager`` and
``sessions.multi_console`` can import it without an import cycle.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks import output
from agentworks.errors import AgentworksError

if TYPE_CHECKING:
    from collections.abc import Callable


def cleanup_now_empty_resource(
    *,
    kind: str,
    name: str,
    created: bool,
    delete: Callable[[], None],
    manual_command: str,
    yes: bool,
    empty_clause: str,
    report_clause: str,
) -> None:
    """Offer, auto-delete, or report a resource that is now empty.

    The caller has already determined the resource is empty (and, where
    relevant, that it is a genuine cleanup candidate); this helper only owns
    the offer/report/auto-delete decision and the warn-and-continue guard
    around the delete.

    Args:
        kind: The lowercase resource noun (``"console"`` / ``"workspace"`` /
            ``"agent"``). Capitalized for sentence-initial messages and used
            verbatim in the lowercase "empty <kind>" phrasings.
        name: The resource's name.
        created: Whether THIS command created the resource. Under ``--yes``
            this is the sole gate for auto-delete; pass ``False`` for a
            console (never session-created), which routes it to report-but-keep
            under ``--yes`` without a special case.
        delete: Performs the delete (already bound with ``yes=True`` and the
            real deleter). Any :class:`AgentworksError` it raises is caught and
            downgraded to a warning; other exceptions propagate as bugs.
        manual_command: The command to name in report / failure messages, e.g.
            ``"agw workspace delete ws-1"``.
        yes: The command's ``--yes`` flag.
        empty_clause: The emptiness phrase for the interactive OFFER prompt,
            e.g. ``"now has no sessions"`` (workspace / agent) or
            ``"has no configured sessions left"`` (console).
        report_clause: The emptiness phrase for the ``--yes`` REPORT warning,
            e.g. ``"now has no sessions"`` or ``"now has no configured
            sessions"``. Kept separate from ``empty_clause`` because the
            console offer and report wordings differ from each other.
    """
    noun = kind.capitalize()

    def _run_delete() -> None:
        try:
            delete()
        except AgentworksError as exc:
            output.warn(f"Could not delete empty {kind} '{name}': {exc}. Remove it with '{manual_command}'.")

    if not yes:
        # Interactive: offer regardless of provenance. Created resources carry
        # a short provenance cue so the operator recognizes what they made.
        provenance = "(created with this session) " if created else ""
        if output.confirm(f"{noun} '{name}' {provenance}{empty_clause}. Delete it?"):
            _run_delete()
    elif created:
        output.detail(f"Deleting {kind} '{name}' (created with this session)...")
        _run_delete()
    else:
        output.warn(f"{noun} '{name}' {report_clause}; delete it with '{manual_command}' if it is no longer needed.")
