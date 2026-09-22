"""Core-owned identity facts for an Agentworks-created VM."""

from __future__ import annotations

import re
import secrets

from agentworks.errors import ValidationError

VM_INSTANCE_MARKER_PATH = "/var/lib/agentworks/instance-id"
"""Protected guest file holding the core-generated VM instance marker."""

_VM_INSTANCE_MARKER_RE = re.compile(r"[0-9a-f]{32}\Z")


def new_vm_instance_marker() -> str:
    """Return a fresh, non-secret marker for one newly created VM."""
    return secrets.token_hex(16)


def validate_vm_instance_marker(value: object) -> str:
    """Validate an untrusted persisted or externally supplied VM marker."""
    if not isinstance(value, str) or not _VM_INSTANCE_MARKER_RE.fullmatch(value):
        raise ValidationError("VM instance marker must be 32 lowercase hexadecimal characters")
    return value
