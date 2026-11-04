"""Operation-local setup failures reported after independent work completes."""

from __future__ import annotations

from dataclasses import dataclass

from agentworks.errors import StateError


@dataclass(frozen=True)
class SetupFailure:
    """One failed integration attempt, with sanitized runtime diagnostics."""

    owner_kind: str
    owner_name: str
    facet: str
    integration: str
    reason: str
    hint: str | None = None


def raise_setup_failures(failures: list[SetupFailure]) -> None:
    """Fail the command once its independent setup work and state commits finish."""
    if not failures:
        return
    details = []
    for failure in failures:
        detail = (
            f"{failure.owner_kind}/{failure.owner_name} facet={failure.facet} "
            f"integration={failure.integration}: {failure.reason}"
        )
        if failure.hint:
            detail += f" Repair: {failure.hint}"
        details.append(detail)
    raise StateError("Setup failed:\n" + "\n".join(details))
