"""Compact artifact application and routing progress, grouped by artifact type."""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.artifacts.model import ArtifactType

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agentworks.artifacts.application import ArtifactDeferral, ArtifactSkip, OwnedArtifactFile
    from agentworks.artifacts.model import ArtifactInput, ArtifactInputs


def _names(items: Sequence[ArtifactInput]) -> str:
    duplicates = Counter(item.content.name for item in items)
    shown = items if len(items) <= 3 else items[:2]
    names = [
        item.content.name
        if duplicates[item.content.name] == 1
        else f"{item.content.name} [{item.origin.component} {item.origin.resource_name}]"
        for item in shown
    ]
    if len(items) > 3:
        names.append("...")
    return ", ".join(names)


def report_application(
    inputs: ArtifactInputs,
    *,
    integration: str,
    owner: str,
    files: Sequence[OwnedArtifactFile] = (),
    deferred: Sequence[ArtifactDeferral] = (),
    skipped: Sequence[ArtifactSkip] = (),
    terminal: bool = False,
) -> None:
    """Report confirmed publication and explicit non-delivery without counting package members."""
    items = tuple(inputs.items())
    omitted = {origin for entry in skipped for origin in entry.origins}
    applied = {origin for file in files for origin in file.origins} - omitted
    routes = {entry.input_id: entry for entry in deferred}
    for artifact_type in ArtifactType:
        delivered = tuple(
            item for item in items if item.content.type is artifact_type and item.origin_identity in applied
        )
        if delivered:
            noun = artifact_type.value if len(delivered) == 1 else artifact_type.map_name
            output.info(f"Applying {len(delivered)} {noun} ({_names(delivered)}) via {integration} at {owner}")
        grouped: dict[tuple[str, str, str], list[ArtifactInput]] = {}
        for item in items:
            if item.content.type is artifact_type and item.identity in routes:
                route = routes[item.identity]
                origin = item.origin
                source = (
                    f"{origin.component} {origin.resource_kind}/{origin.resource_name} "
                    f"(producer {origin.producer}, bundle {origin.bundle})"
                    if terminal
                    else ""
                )
                grouped.setdefault((route.destination, route.reason, source), []).append(item)
        for (destination, reason, source), group in grouped.items():
            noun = artifact_type.value if len(group) == 1 else artifact_type.map_name
            action = "Unhandled" if terminal else "Deferring"
            target = f"from {source}" if terminal else f"to {destination}"
            output.warn(
                f"{action} {len(group)} {noun} ({_names(group)}) via {integration} at {owner} {target}: {reason}"
            )
    if deferred and not terminal:
        destinations = {entry.destination for entry in deferred}
        timing = {
            "user": "User deferrals are reconsidered when each actual user's setup next runs.",
            "workspace": (
                "Workspace deferrals are reconsidered at creation; existing workspaces cannot refresh artifacts."
            ),
            "session": (
                "Session deferrals are reconsidered at the next managed start or restart; "
                "running sessions are not refreshed."
            ),
        }
        output.info(" ".join(message for destination, message in timing.items() if destination in destinations))
        output.info("Delivery depends on the receiving integration and any required enabled_workarounds.")
