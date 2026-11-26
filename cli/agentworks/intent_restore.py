"""How a command refused after it recorded a new desired state says how to put the old one back.

Commands record intent under a brief lock and take their final lock set
later, so a refusal there, busy or changed, leaves the new intent
recorded. That is the contract, and the refusal says so: it carries the
``set-desired-state`` command that restores the intent it replaced, or
``()`` when that intent has no public form (a legacy NULL, which
``set-desired-state`` cannot write), as long as the intent it recorded
still stands (:func:`while_recorded`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from agentworks.db import DesiredRuntimeState
    from agentworks.errors import StateConflictError
    from agentworks.native_locks import NativeLockBusyError

type IntentKind = Literal["vm", "session", "console"]


def restore_command(kind: IntentKind, name: str, previous: DesiredRuntimeState | None) -> tuple[str, ...]:
    """The command words that record ``previous`` again for ``kind`` ``name``, or ``()`` for a legacy NULL."""
    return () if previous is None else ("agw", kind, "set-desired-state", name, previous.value)


def replaced_intent(
    kind: IntentKind, name: str, previous: DesiredRuntimeState | None, recorded: DesiredRuntimeState
) -> tuple[str, ...] | None:
    """:func:`restore_command` for an intent ``recorded`` replaced, or ``None`` when it changed nothing."""
    return None if previous is recorded else restore_command(kind, name, previous)


def while_recorded(
    restore: tuple[str, ...] | None, *, recorded: DesiredRuntimeState | None, current: DesiredRuntimeState | None
) -> tuple[str, ...] | None:
    """``restore`` while ``current``, the stored intent, is still the one its command ``recorded``.

    Once another command replaced that intent there is nothing to restore,
    and saying the new intent stays recorded would be false, so the refusal
    carries no words.
    """
    return restore if current is recorded else None


def restore_wording(restore: tuple[str, ...]) -> str:
    """What a refusal after recorded intent adds: that the intent stays recorded, and how to restore the old one."""
    if restore:
        return f"the new intent stays recorded; restore the previous one with '{' '.join(restore)}'"
    return "the new intent stays recorded"


def refused_after_intent(refused: NativeLockBusyError | StateConflictError, restore: tuple[str, ...] | None) -> None:
    """Mark ``refused``, a final set's busy or changed refusal, with the intent its command replaced, if any."""
    if restore is None:
        return
    refused.restore_intent = restore
    wording = restore_wording(restore)
    refused.add_hint(f"{wording[0].upper()}{wording[1:]}.")
