"""The retired presence-shapes, refused by name with their exact rewrite.

Three platforms used to express a mode CHOICE by writing or omitting an
optional block. Each is a required tagged union now, so EVERY manifest
written against the old spelling fails, including ones that are wrong in
no other way. These tests pin the messages that carry an operator across
that break: the rewrite is rendered, not merely implied.

Release-scoped with :mod:`agentworks.capabilities.retired_shapes`. When
that module is deleted, this file goes with it and the generic
"unknown field" / "is required" messages become the answer.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentworks.capabilities.config import validate_capability_config
from agentworks.capabilities.retired_shapes import RETIRED_SHAPE_HINT
from agentworks.errors import ConfigError
from agentworks.plugins.aws.platform import EC2Platform
from agentworks.plugins.azure.platform import AzureVMPlatform
from agentworks.schema import RefOwner
from agentworks.source_location import SourceLocation

# Importing the two plugin platforms SEATS them, which is what makes
# ``azure-vm`` and ``aws-ec2`` selectable below.
_SEATED = (AzureVMPlatform, EC2Platform)

OWNER = RefOwner(kind="vm-site", name="dev")

_AZURE = {"subscription_id": "s", "resource_group": "g", "region": "eastus"}
_AWS = {"region": "us-east-1"}

_WHERE = SourceLocation(file=Path("/etc/agentworks/resources/sites.yaml"), line=12)
"""A declaration site, for the tests about framing. Outside ``$HOME`` on
purpose, so the rendered path is stable rather than depending on whose
machine the suite runs on."""


def _refuse(platform: str, blob: dict[str, object], location: SourceLocation | None = None) -> ConfigError:
    with pytest.raises(ConfigError) as exc:
        validate_capability_config(
            kind="vm-platform",
            config={"name": platform, **blob},
            owner=OWNER,
            location=location,
        )
    return exc.value


@pytest.mark.parametrize(
    ("platform", "blob", "rewrite"),
    [
        pytest.param(
            "lima",
            {"vm_host": "me@gpu-box"},
            "placement: {mode: ssh, host: ...}",
            id="lima-vm-host",
        ),
        pytest.param(
            "azure-vm",
            {**_AZURE, "service_principal": {"tenant_id": "t", "client_id": "c", "secret": "az-sp"}},
            "auth: {mode: service-principal, tenant_id: ..., client_id: ..., secret: ...}",
            id="azure-service-principal",
        ),
        pytest.param(
            "aws-ec2",
            {**_AWS, "credentials": {"access_key_id": "AKIA", "access_key_secret": "aws-key"}},
            "auth: {mode: access-key, access_key_id: ..., access_key_secret: ...}",
            id="aws-credentials",
        ),
    ],
)
def test_a_written_retired_field_prints_its_exact_rewrite(platform: str, blob: dict[str, object], rewrite: str) -> None:
    """The operator WROTE the old block, so the error names it and prints
    the replacement, values elided as ``...`` (the same elision the
    retired sibling shape uses). The keys come from the document, so a
    principal that named no secret is not told to add one."""
    error = _refuse(platform, blob)
    message = str(error)
    assert rewrite in message
    assert "no longer a supported field" in message
    assert error.hint == RETIRED_SHAPE_HINT


def test_the_rewrite_carries_only_the_keys_the_operator_wrote() -> None:
    """Rendered from the document rather than from a template: a service
    principal with no ``secret`` gets a rewrite with no ``secret`` in it,
    so applying the error's own output does not silently add a field."""
    error = _refuse("azure-vm", {**_AZURE, "service_principal": {"tenant_id": "t", "client_id": "c"}})
    assert "auth: {mode: service-principal, tenant_id: ..., client_id: ...}" in str(error)
    assert "secret" not in str(error)


@pytest.mark.parametrize(
    ("platform", "blob", "retired", "rewrite"),
    [
        pytest.param("lima", {}, "vm_host", "placement: {mode: local}", id="lima-local"),
        pytest.param("azure-vm", _AZURE, "service_principal", "auth: {mode: ambient}", id="azure-ambient"),
        pytest.param("aws-ec2", _AWS, "credentials", "auth: {mode: ambient}", id="aws-ambient"),
    ],
)
def test_an_absent_retired_field_explains_that_a_line_is_being_added(
    platform: str, blob: dict[str, object], retired: str, rewrite: str
) -> None:
    """The trap case, and the one most operators hit: the manifest never
    wrote the retired field at all, so a bare "auth is required" reads as
    "you deleted something".

    The message has to do three things, and each is asserted: name the
    required field, say what omitting the old one USED to mean (so the
    operator can confirm the arm is the one they were relying on), and
    print the one line to add.
    """
    error = _refuse(platform, blob)
    message = str(error)
    assert "is required and this resource does not declare it" in message
    assert f"Omitting '{retired}' used to mean" in message
    assert "nothing was deleted from your document" in message
    assert rewrite in message
    assert error.hint == RETIRED_SHAPE_HINT


@pytest.mark.parametrize(
    ("platform", "blob"),
    [
        pytest.param("lima", {"placement": {"mode": "local"}, "vm_host": "me@box"}, id="lima"),
        pytest.param("azure-vm", {**_AZURE, "auth": {"mode": "ambient"}, "service_principal": {}}, id="azure-vm"),
        pytest.param("aws-ec2", {**_AWS, "auth": {"mode": "ambient"}, "credentials": {}}, id="aws-ec2"),
    ],
)
def test_a_half_migrated_document_gets_the_ordinary_unknown_field_error(platform: str, blob: dict[str, object]) -> None:
    """A document carrying BOTH the union and the stray old key is a
    half-applied migration, not a pre-migration one. The model layer's
    unknown-key error is already the precise answer, and printing a
    rewrite would tell the operator to write what they have written."""
    message = str(_refuse(platform, blob))
    assert "unknown field" in message
    assert "no longer a supported field" not in message


def _frame_of(message: str) -> str:
    """The ``<file>:<line>: <kind>/<name>`` an operator scans a manifest
    error for, taken off the front of ``message``.

    Stops at the owner rather than at the punctuation after it: the bridge
    writes a dot before a field path and a colon before a whole-document
    problem, and which of those follows is not what this is about.
    """
    head, marker, _rest = message.partition(OWNER.display)
    assert marker, f"no owner frame in {message!r}"
    return head + marker


@pytest.mark.parametrize(
    ("platform", "blob"),
    [
        pytest.param("lima", {"vm_host": "me@gpu-box"}, id="written"),
        pytest.param("lima", {}, id="absent"),
    ],
)
def test_a_retired_shape_refusal_is_framed_like_the_errors_beside_it(platform: str, blob: dict[str, object]) -> None:
    """A refusal that runs BEFORE validation is still an error about a
    document, and it reaches an operator in the same list as the ones that
    run after. Framed differently it reads as being about something else,
    and the operator whose azure, aws, and lima sites all broke at once is
    the last one who should have to guess which file to open.

    The expectation is read off a NEIGHBOR rather than spelled here: the
    unknown-field error for the same owner at the same location is framed
    by the error bridge, so this asserts the two agree rather than
    asserting a string that would go stale the day the frame changes.
    """
    neighbor = _refuse(platform, {"placement": {"mode": "local"}, "bogus": 1}, _WHERE)
    frame = _frame_of(str(neighbor))

    # Not vacuous: a frame with no file and no line would be shared by two
    # unlocated errors just as happily.
    assert "sites.yaml:12" in frame

    assert str(_refuse(platform, blob, _WHERE)).startswith(frame)


def test_a_platform_with_no_retired_shape_is_untouched() -> None:
    """The declaration is opt-in, so a platform that never broke its
    config validates exactly as before: proxmox has always required its
    token fields, and wsl2 takes no configuration at all."""
    validate_capability_config(kind="vm-platform", config={"name": "wsl2"}, owner=OWNER)
    message = str(_refuse("proxmox", {"api_url": "https://pve:8006", "node": "n", "token_id": "t"}))
    assert "template_vmid: is required" in message
    assert "no longer a supported field" not in message
