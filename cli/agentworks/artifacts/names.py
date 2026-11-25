"""Canonical artifact names shared by declarations, captures and native inputs."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, TypeGuard

if TYPE_CHECKING:
    from agentworks.artifacts.model import ArtifactInput, ArtifactInputs

ARTIFACT_NAME_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
MAX_ARTIFACT_NAME_LENGTH = 64
_NAME = re.compile(ARTIFACT_NAME_PATTERN)


def is_artifact_name(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and len(value) <= MAX_ARTIFACT_NAME_LENGTH and _NAME.fullmatch(value) is not None


def delivery_name(inputs: ArtifactInputs, item: ArtifactInput, *, base: str | None = None) -> str:
    """Spell a native name from its canonical name and original receiving relationship."""
    name = item.content.name if base is None else base
    return f"{item.origin.component}-{name}" if item.origin.owner in inputs.deferred else name
