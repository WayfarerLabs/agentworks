"""Exact Azure VM identity validation at provider and persisted-state boundaries."""

from __future__ import annotations

import re
from typing import Literal

from agentworks.errors import ValidationError

MAX_RESOURCE_ID_BYTES = 2048
_RESOURCE_ID = re.compile(
    r"/subscriptions/([^/\x00-\x1f\x7f]+)/resourceGroups/([^/\x00-\x1f\x7f]+)/"
    r"providers/Microsoft\.Compute/virtualMachines/([^/\x00-\x1f\x7f]+)"
)
_VM_ID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
ObservedResourceKind = Literal["virtualMachines", "networkInterfaces", "publicIPAddresses", "networkSecurityGroups"]


def resource_name_equal(left: str, right: str) -> bool:
    """Compare ASCII casing without merging undocumented Unicode equivalents."""
    return left.translate(_ASCII_LOWER) == right.translate(_ASCII_LOWER)


def parse_observed_resource_id(resource_id: object, kind: ObservedResourceKind) -> tuple[str, str, str]:
    """Validate a literal fixed-kind ARM ID received from an owned provider read.

    Azure can vary returned casing. Keep literal component bytes for SDK name
    selection; never decode URL syntax or apply Unicode normalization.
    """
    if type(resource_id) is not str:
        raise ValidationError("Azure observed resource identity is invalid")
    try:
        bounded = len(resource_id.encode("utf-8")) <= MAX_RESOURCE_ID_BYTES
    except UnicodeEncodeError:
        bounded = False
    parts = resource_id.split("/")
    provider = "Microsoft.Compute" if kind == "virtualMachines" else "Microsoft.Network"
    if (
        bounded
        and len(parts) == 9
        and parts[0] == ""
        and all(part and part not in {".", ".."} for part in parts[1:])
        and not any(ord(char) < 32 or ord(char) == 127 for char in resource_id)
        and all(
            resource_name_equal(parts[index], expected)
            for index, expected in (
                (1, "subscriptions"),
                (3, "resourceGroups"),
                (5, "providers"),
                (6, provider),
                (7, kind),
            )
        )
    ):
        return parts[2], parts[4], parts[8]
    raise ValidationError("Azure observed resource identity is invalid")


def observed_resource_id_equal(left: object, right: object, kind: ObservedResourceKind) -> bool:
    """Validate both provider paths before comparing their literal identities."""
    left_parts = parse_observed_resource_id(left, kind)
    right_parts = parse_observed_resource_id(right, kind)
    return all(resource_name_equal(a, b) for a, b in zip(left_parts, right_parts, strict=True))


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
