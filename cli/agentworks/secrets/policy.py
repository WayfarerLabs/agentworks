"""Explicit interaction authority for secret-consuming operations."""

from __future__ import annotations

from enum import StrEnum


class InteractionPolicy(StrEnum):
    """Whether an operation permits sources that may require interaction."""

    ALLOW = "allow"
    REFUSE = "refuse"
