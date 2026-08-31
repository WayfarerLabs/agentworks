"""Managed VM checkpoint listing facts and presentation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from agentworks import output

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agentworks.db import VMCheckpointRow
    from agentworks.machine_output import JsonObject


class CheckpointRestoreStatus(StrEnum):
    """Current restore eligibility derived from lifecycle and desired state."""

    AVAILABLE = "available"
    RESUME_REQUIRED = "resume-required"
    DECLARATIONS_CHANGED = "declarations-changed"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class CheckpointListing:
    """One persisted checkpoint plus its current restore eligibility."""

    checkpoint: VMCheckpointRow
    restore_status: CheckpointRestoreStatus


def checkpoint_listing_data(listings: Sequence[CheckpointListing]) -> JsonObject:
    """Project checkpoint rows into stable machine-output facts."""

    return {
        "checkpoints": [
            {
                "restore_status": listing.restore_status.value,
                "vm_name": listing.checkpoint.vm_name,
                "name": listing.checkpoint.name,
                "provider_identifier": listing.checkpoint.provider_identifier,
                "state": listing.checkpoint.state.value,
                "purpose": listing.checkpoint.purpose,
                "capture_release": listing.checkpoint.capture_release.value,
                "source_release": (
                    None if listing.checkpoint.source_release is None else listing.checkpoint.source_release.value
                ),
                "target_release": (
                    None if listing.checkpoint.target_release is None else listing.checkpoint.target_release.value
                ),
                "created_at": listing.checkpoint.created_at,
            }
            for listing in listings
        ]
    }


def render_checkpoint_listing(listings: Sequence[CheckpointListing]) -> None:
    """Render the compact human checkpoint table."""

    if not listings:
        output.info("No managed VM checkpoints found.")
        return
    header = (
        f"{'VM':<20} {'CHECKPOINT':<37} {'STATE':<10} {'RESTORE':<21} "
        f"{'PURPOSE':<15} {'CAPTURE':<10} {'TRANSITION':<20} CREATED"
    )
    output.info(header)
    output.info("-" * len(header))
    for listing in listings:
        row = listing.checkpoint
        transition = (
            "-"
            if row.source_release is None or row.target_release is None
            else f"{row.source_release.value}->{row.target_release.value}"
        )
        output.info(
            f"{output.truncate(row.vm_name, 20):<20} {row.name:<37} {row.state.value:<10} "
            f"{listing.restore_status.value:<21} {row.purpose:<15} {row.capture_release.value:<10} "
            f"{transition:<20} {row.created_at}"
        )
