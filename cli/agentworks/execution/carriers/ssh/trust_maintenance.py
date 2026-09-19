"""Operator maintenance of explicitly named SSH trust bundles, without config or DB access."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.errors import ValidationError
from agentworks.execution.carriers.ssh.trust import (
    ManagedSSHTrust,
    SSHTrustFiles,
    SSHTrustStatus,
    block_trust,
    import_trust,
    refresh_trust,
    trust_status,
)
from agentworks.path_rendering import format_host_path

if TYPE_CHECKING:
    from pathlib import Path


def _generation(value: str) -> str | None:
    """Validate the operator's compare-and-replace token before touching the bundle."""
    if value == "none":
        return None
    if re.fullmatch(r"[a-f0-9]{32}", value) is None:
        raise ValidationError("Expected generation must come from describe-ssh-trust")
    return value


def _describe(directory: Path, status: SSHTrustStatus) -> None:
    # Paths are filesystem input and can contain terminal controls. repr keeps
    # each attributed path on one safe line after the common host rendering.
    output.info(f"SSH trust: {format_host_path(directory)!r}")
    output.info(f"State: {'blocked' if status.blocked else 'available'}")
    output.info(f"Generation: {status.generation or 'none'}")
    output.info(f"Authority: {status.authority}")
    for path in status.sources.known_hosts:
        output.info(f"Known-host source: {format_host_path(path)!r}")
    if status.sources.revoked_host_keys is not None:
        output.info(f"Revocation source: {format_host_path(status.sources.revoked_host_keys)!r}")
    else:
        output.info("Revocation source: none")


def import_ssh_trust(
    directory: Path, *, sources: list[Path], authority: str, revoked_host_keys: Path | None = None
) -> None:
    """Create a new bundle from complete explicit snapshots and describe its state."""
    bundle = import_trust(directory, sources=SSHTrustFiles(tuple(sources), revoked_host_keys), authority=authority)
    _describe(directory, trust_status(bundle))


def refresh_ssh_trust(
    directory: Path,
    *,
    sources: list[Path],
    authority: str,
    expected_generation: str,
    revoked_host_keys: Path | None = None,
) -> None:
    """Publish replacement policy only against the observed generation."""
    status = refresh_trust(
        ManagedSSHTrust(directory),
        sources=SSHTrustFiles(tuple(sources), revoked_host_keys),
        authority=authority,
        expected_generation=_generation(expected_generation),
    )
    _describe(directory, status)


def block_ssh_trust(directory: Path, *, expected_generation: str) -> None:
    """Block new admission without deleting or changing the selected generation."""
    status = block_trust(ManagedSSHTrust(directory), expected_generation=_generation(expected_generation))
    _describe(directory, status)


def describe_ssh_trust(directory: Path) -> None:
    """Describe maintenance facts without admitting the bundle for SSH use."""
    _describe(directory, trust_status(ManagedSSHTrust(directory)))
