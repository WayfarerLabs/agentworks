"""Tests for ``CapabilityBlock``, the tagged table a hosting kind holds.

The four properties below are what let a declared row carry the table the
operator actually writes instead of a naming string and a sibling blob:
the extras survive, they survive RE-validation, the whole table is
readable back, and the tag's absence reads as a missing field rather than
as a shape error.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError

from agentworks.schema import AgwModel, CapabilityBlock, RefOwner, config_error_from

OWNER = RefOwner(kind="vm-site", name="lab")


class Host(AgwModel):
    """A hosting kind's spec: one closed-world field holding an open
    table."""

    platform: CapabilityBlock


def _message(caught: PydanticValidationError) -> str:
    """What an operator reads for ``caught``, unlocated.

    Unlocated because these assertions are about the block's own shape,
    not about where it was declared; the location framing is pinned in
    ``tests/schema/test_errors.py``.
    """
    return str(config_error_from(caught, model_cls=Host, owner=OWNER))


def test_the_capabilitys_own_keys_survive_validation() -> None:
    block = CapabilityBlock.model_validate({"name": "lima", "vm_host": "h", "nested": {"a": 1}})

    assert block.name == "lima"
    assert block.config == {"vm_host": "h", "nested": {"a": 1}}


def test_the_capabilitys_own_keys_survive_re_validation() -> None:
    """``revalidate_instances="always"`` re-checks a nested instance, and
    a re-check that dropped the extras would silently empty every
    capability blob the moment a row was rebuilt."""
    host = Host.model_validate({"platform": {"name": "lima", "vm_host": "h"}})

    assert Host.model_validate(host).platform.config == {"vm_host": "h"}


def test_the_whole_table_is_readable_back_as_written() -> None:
    """What the capability core is handed: the tag and the config in one
    mapping, exactly as the operator wrote it."""
    block = CapabilityBlock.model_validate({"name": "proxmox", "token_secret": "s"})

    assert block.tagged == {"name": "proxmox", "token_secret": "s"}


def test_an_omitted_tag_reads_as_a_missing_field() -> None:
    with pytest.raises(PydanticValidationError) as caught:
        Host.model_validate({"platform": {"vm_host": "h"}})

    assert _message(caught.value) == "vm-site/lab.platform.name: is required"


def test_an_empty_tag_is_refused() -> None:
    with pytest.raises(PydanticValidationError) as caught:
        Host.model_validate({"platform": {"name": ""}})

    assert _message(caught.value) == "vm-site/lab.platform.name: must not be empty"


def test_a_non_table_reads_as_a_table_requirement() -> None:
    """The legacy spelling ``platform: lima`` reaches the model as a bare
    string; the message says what shape belongs there."""
    with pytest.raises(PydanticValidationError) as caught:
        Host.model_validate({"platform": "lima"})

    assert _message(caught.value) == "vm-site/lab.platform: must be a table"


def test_the_emitted_schema_leaves_the_capabilitys_half_open() -> None:
    """The host's schema says what the host owns and no more: the
    capability's keys are its own model's business, and 2.7 splices the
    union in."""
    emitted = CapabilityBlock.model_json_schema()

    assert set(emitted["properties"]) == {"name"}
    assert emitted["properties"]["name"]["type"] == "string"
    assert emitted["required"] == ["name"]
    assert emitted["additionalProperties"] is True
