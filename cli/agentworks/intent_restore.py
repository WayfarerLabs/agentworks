"""How a command refused after it recorded a new desired state says how to put the old one back.

Commands record intent under a brief lock and take their final lock set
later, so a refusal there leaves the new intent recorded. That is the
contract, and the refusal says so: it carries the ``set-desired-state``
command that restores the intent it replaced, or ``()`` when that intent
has no public form (a legacy NULL, which ``set-desired-state`` cannot
write).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from agentworks.db import DesiredRuntimeState
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


def restore_wording(restore: tuple[str, ...]) -> str:
    """What a refusal after recorded intent adds: that the intent stays recorded, and how to restore the old one."""
    if restore:
        return f"the new intent stays recorded; restore the previous one with '{' '.join(restore)}'"
    return "the new intent stays recorded"


def refused_after_intent(busy: NativeLockBusyError, restore: tuple[str, ...] | None) -> None:
    """Mark ``busy``, a final set's refusal, with the intent its command recorded, if it changed one."""
    if restore is None:
        return
    busy.restore_intent = restore
    wording = restore_wording(restore)
    busy.add_hint(f"{wording[0].upper()}{wording[1:]}.")
