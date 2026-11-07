"""Paths owned by Agentworks' managed Linux-user model."""

from __future__ import annotations


def managed_home(username: str) -> str:
    """Return the home directory used when provisioning a managed Linux user."""
    return f"/home/{username}"
