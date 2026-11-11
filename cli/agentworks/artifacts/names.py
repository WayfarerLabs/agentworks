"""Canonical artifact names shared by declarations, captures and native inputs."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, TypeGuard

if TYPE_CHECKING:
    from agentworks.artifacts.model import ArtifactInput

ARTIFACT_NAME_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
MAX_ARTIFACT_NAME_LENGTH = 64
_NAME = re.compile(ARTIFACT_NAME_PATTERN)


def is_artifact_name(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and len(value) <= MAX_ARTIFACT_NAME_LENGTH and _NAME.fullmatch(value) is not None


def mcp_native_name(item: ArtifactInput) -> str:
    """Readable stable native key, independent of content and receiving facet."""
    return f"agentworks-{item.content.name[:24].rstrip('-')}-{item.origin_identity[:20]}"
