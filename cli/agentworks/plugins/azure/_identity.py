"""Exact Azure VM identity validation at provider and persisted-state boundaries."""

from __future__ import annotations

import re

from agentworks.errors import ValidationError

MAX_RESOURCE_ID_BYTES = 2048
_RESOURCE_ID = re.compile(
    r"/subscriptions/([^/\x00-\x1f\x7f]+)/resourceGroups/([^/\x00-\x1f\x7f]+)/"
    r"providers/Microsoft\.Compute/virtualMachines/([^/\x00-\x1f\x7f]+)"
)
_VM_ID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def parse_resource_id(resource_id: object) -> tuple[str, str, str]:
    """Validate a complete literal ARM VM path from recovery or provider input.

    Preserve the activation boundary's accepted set and error semantics. Literal
    components are not URL-decoded, case-folded or normalized here.
    """
    if type(resource_id) is not str:
        raise ValidationError("Azure activation resource identity is invalid")
    try:
        match = _RESOURCE_ID.fullmatch(resource_id)
        if (
            match is not None
            and len(resource_id.encode("utf-8")) <= MAX_RESOURCE_ID_BYTES
            and all(component not in {".", ".."} for component in match.groups())
        ):
            return match[1], match[2], match[3]
    except UnicodeEncodeError:
        pass
    raise ValidationError("Azure activation resource identity is invalid")


def canonical_vm_id(vm_id: object) -> str:
    """Canonicalize a persisted or observed hyphenated 128-bit VM identifier.

    Azure promises 128 bits, not a particular UUID version or variant.
    """
    if type(vm_id) is not str or _VM_ID.fullmatch(vm_id) is None:
        raise ValidationError("Azure VM unique identity is invalid")
    return vm_id.lower()
