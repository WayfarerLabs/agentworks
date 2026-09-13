"""Canonical artifact names shared by declarations, captures and native inputs."""

from __future__ import annotations

import re
from typing import TypeGuard

ARTIFACT_NAME_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
MAX_ARTIFACT_NAME_LENGTH = 64
_NAME = re.compile(ARTIFACT_NAME_PATTERN)


def is_artifact_name(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and len(value) <= MAX_ARTIFACT_NAME_LENGTH and _NAME.fullmatch(value) is not None
