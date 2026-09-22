"""VM instance-marker value invariants."""

from __future__ import annotations

import re

import pytest

from agentworks.errors import ValidationError
from agentworks.vms.identity import (
    VM_INSTANCE_MARKER_PATH,
    new_vm_instance_marker,
    validate_vm_instance_marker,
)


def test_new_instance_marker_is_canonical_and_non_secret() -> None:
    marker = new_vm_instance_marker()
    assert re.fullmatch(r"[0-9a-f]{32}", marker)
    assert validate_vm_instance_marker(marker) == marker
    assert VM_INSTANCE_MARKER_PATH == "/var/lib/agentworks/instance-id"


@pytest.mark.parametrize("marker", ("", "A" * 32, "0" * 31, "g" * 32, None, 3))
def test_instance_marker_rejects_noncanonical_values(marker: object) -> None:
    with pytest.raises(ValidationError):
        validate_vm_instance_marker(marker)
