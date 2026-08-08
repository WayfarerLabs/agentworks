"""The retired presence-shapes, refused by name with their exact rewrite.

Three platforms used to express a mode CHOICE by writing or omitting an
optional block. Each is a tagged union now, defaulting to the mode
omission used to select, so only a manifest that WROTE the old field
(with a value or as an explicit null) fails; one that omitted it loads
on the default, and the first tests below hold that non-break to be
real. The rest pin the messages that carry the writing documents across
the break: the rewrite is rendered, not merely implied.

Release-scoped with :mod:`agentworks.capabilities.retired_shapes`. When
that module is deleted, this file goes with it and the generic
"unknown field" message becomes the answer.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from agentworks.capabilities.config import (
    capability_config_model,
    registered_implementations,
    validate_capability_config,
)
from agentworks.capabilities.git_credential.base import GitCredentialProvider
from agentworks.capabilities.retired_shapes import (
    GIT_TOKEN_NULL_HINT,
    RETIRED_SHAPE_HINT,
    RetiredPresenceShape,
)
from agentworks.errors import ConfigError
from agentworks.plugins.aws.platform import EC2Platform
from agentworks.plugins.azure.platform import AzureVMPlatform
from agentworks.schema import RefOwner, iter_field_docs
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


def _declared_shapes() -> dict[str, RetiredPresenceShape]:
    """Every seated vm-platform's declaration, keyed by platform name."""
    return {
        name: shape
        for name, impl in registered_implementations("vm-platform").items()
        if isinstance(shape := getattr(impl, "retired_shape", None), RetiredPresenceShape)
    }


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


@pytest.mark.parametrize("value", [pytest.param("oops", id="scalar"), pytest.param({}, id="empty-table")])
def test_a_retired_value_with_no_keys_elides_the_arm_s_fields_rather_than_promising_none(value: object) -> None:
    """A rewrite that names a mode and no fields is a document the model
    rejects for the three keys that arm requires.

    Both spellings reach it: a value that is not a table at all
    (``service_principal: "oops"``) and a table with nothing in it. In
    neither case is there a key to transcribe, so the bare ``...`` says
    the arm's own fields go here, which is true, instead of saying
    nothing, which reads as "there are none".
    """
    message = str(_refuse("azure-vm", {**_AZURE, "service_principal": value}))

    assert "auth: {mode: service-principal, ...}" in message, message


#: The three declarations' subjects, as ``(platform, base config, retired
#: field, the mode omitting it used to select)``. The set is pinned by
#: ``test_the_three_platforms_that_crossed_the_break_are_the_ones_declaring_it``.
_ABSENT_CASES = [
    pytest.param("lima", {}, "vm_host", "local", id="lima-local"),
    pytest.param("azure-vm", _AZURE, "service_principal", "ambient", id="azure-ambient"),
    pytest.param("aws-ec2", _AWS, "credentials", "ambient", id="aws-ambient"),
]


@pytest.mark.parametrize(("platform", "base", "retired", "absent_mode"), _ABSENT_CASES)
def test_a_manifest_that_wrote_nothing_loads_on_the_declared_default(
    platform: str, base: dict[str, object], retired: str, absent_mode: str
) -> None:
    """The non-break, held to be real by execution: a document that never
    wrote the retired field selects nothing by its absence and lands on
    the union's declared default, which is the same mechanism omission
    always meant. An earlier revision made this an error; the operator
    ruling that reversed it is recorded at the union sites.
    """
    shape = _declared_shapes()[platform]
    validated = validate_capability_config(kind="vm-platform", config={"name": platform, **base}, owner=OWNER)
    assert validated is not None
    resolved = getattr(validated, shape.union_field)
    assert getattr(resolved, "mode", None) == absent_mode, resolved


@pytest.mark.parametrize(("platform", "base", "retired", "absent_mode"), _ABSENT_CASES)
def test_an_explicit_null_selects_the_mode_omission_selected(
    platform: str, base: dict[str, object], retired: str, absent_mode: str
) -> None:
    """``vm_host: null`` was a LOCAL site, not an SSH one.

    All three retired fields were optional, so a written ``null`` did
    byte-for-byte what omitting the key did: the ambient credential chain
    for azure and aws, ``limactl`` on this machine for lima. Reading the
    choice off key MEMBERSHIP sent each of these operators to the other
    arm, and for lima that is not an imprecise rewrite but the opposite
    placement: an operator whose VMs ran here was told to write
    ``mode: ssh``.

    That mattered more than its size, which is why it is asserted from
    both ends: the arm they were relying on is named, and the arm they
    were NOT is absent from the message rather than merely outranked by
    it.
    """
    shape = _declared_shapes()[platform]
    message = str(_refuse(platform, {**base, retired: None}))

    assert f"{shape.union_field}: {{mode: {absent_mode}}}" in message, message
    assert shape.present_mode not in message, message
    assert "no longer a supported field" not in message, message


@pytest.mark.parametrize(("platform", "base", "retired", "absent_mode"), _ABSENT_CASES)
def test_a_null_document_is_told_to_delete_the_line_because_adding_one_is_not_enough(
    platform: str, base: dict[str, object], retired: str, absent_mode: str
) -> None:
    """The null case's edit has a deletion in it, and the assertion is
    that the edit WORKS rather than that the words are there.

    Adding the union while leaving the null line in place answers the
    very next load with ``vm_host: unknown field``, so the advice,
    applied verbatim, has to be run here, and the version that only adds
    has to be shown failing, or "delete the null line" is prose nothing
    holds to account.
    """
    shape = _declared_shapes()[platform]
    message = str(_refuse(platform, {**base, retired: None}))
    assert "delete the null line" in message, message

    applied: dict[str, object] = yaml.safe_load(shape.absent_rewrite)
    assert applied == {shape.union_field: {"mode": absent_mode}}, applied

    # The advice, applied: the null line deleted and the union written.
    validate_capability_config(kind="vm-platform", config={"name": platform, **base, **applied}, owner=OWNER)

    # The half-applied edit: the union added, the null line kept.
    with pytest.raises(ConfigError, match=f"{retired}: unknown field"):
        validate_capability_config(
            kind="vm-platform",
            config={"name": platform, **base, retired: None, **applied},
            owner=OWNER,
        )


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
        pytest.param("lima", {"vm_host": None}, id="null"),
    ],
)
def test_a_retired_shape_refusal_is_framed_like_the_errors_beside_it(platform: str, blob: dict[str, object]) -> None:
    """A refusal that runs BEFORE validation is still an error about a
    document, and it reaches an operator in the same list as the ones that
    run after. Framed differently it reads as being about something else,
    and an operator crossing a break is the last one who should have to
    guess which file to open.

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


# -- The declarations against the live unions they rewrite to ----------------
#
# ``RetiredPresenceShape`` hand-authors knowledge of BOTH sides of the
# break: the old field's name and the live union's field, modes, and (for
# lima) the field a scalar's value moves into. The rewrites above pin the
# rendered strings, but a renamed mode would leave the declaration and
# those pinned strings stale TOGETHER: the error would confidently print a
# rewrite the model rejects. So the declarations are also compared to the
# live models structurally, with no literal spelled on both sides.
#
# ``_declared_shapes`` is the read they share, and it sits with the other
# helpers at the top because the message tests above read declarations too.


def test_the_three_platforms_that_crossed_the_break_are_the_ones_declaring_it() -> None:
    """Membership before structure: a structural check over 'whatever the
    registry returned' is satisfied by an empty registry, and unseated
    plugins is exactly how two of the three subjects go missing. The
    ``_SEATED`` import above is what makes azure and aws answerable here
    at all."""
    assert set(_declared_shapes()) == {"lima", "azure-vm", "aws-ec2"}


def test_each_declaration_matches_the_live_union_it_rewrites_to() -> None:
    """Every name a declaration authors is checked against the model it
    describes: the union field exists and DEFAULTS to the declared
    absent mode (the fact the null rewrite and the loads-on-default
    behavior both rest on), both modes are tags a document can actually
    select, a scalar's destination field is a real field of the arm the
    rewrite names, and the retired field is not still live (a
    declaration for a field the model kept would refuse valid
    documents)."""
    for name, shape in _declared_shapes().items():
        model = capability_config_model("vm-platform", name)
        assert model is not None, f"{name}: declares a retired shape and no config model"
        docs = {doc.path: doc for doc in iter_field_docs(model)}
        union_doc = docs.get((shape.union_field,))
        assert union_doc is not None, f"{name}: '{shape.union_field}' is not a field of {model.__name__}"
        arms = {arm.tag: arm.doc.model for arm in union_doc.union_arms}
        assert not union_doc.required, f"{name}: '{shape.union_field}' is required, so omission would break"
        assert type(union_doc.default) is arms.get(shape.absent_mode), (
            f"{name}: '{shape.union_field}' defaults to {union_doc.default!r}, not the "
            f"'{shape.absent_mode}' arm the declaration names as what omission meant"
        )
        for mode in (shape.present_mode, shape.absent_mode):
            ordered_arms = sorted(arms, key=lambda tag: "" if tag is None else tag)
            assert mode in arms, f"{name}: mode '{mode}' selects no arm of '{shape.union_field}' (live: {ordered_arms})"
        if shape.scalar_field is not None:
            arm_model = arms[shape.present_mode]
            assert shape.scalar_field in arm_model.model_fields, (
                f"{name}: '{shape.scalar_field}' is not a field of {arm_model.__name__}, "
                f"so the rendered rewrite names a key the arm rejects"
            )
        assert shape.retired_field not in model.model_fields, (
            f"{name}: '{shape.retired_field}' is still a live field of {model.__name__}"
        )


def test_the_present_arm_of_every_declaration_has_fields_for_the_marker_to_stand_for() -> None:
    """The invariant ``rewrite_for``'s elided ``...`` rests on.

    The marker claims there is more to write. That is true only while
    every declared ``present_mode`` names an arm with fields beyond the
    tag, which is no accident (a retired field carried a mode choice BY
    carrying fields) but is enforced nowhere else. An arm that lost its
    fields would leave the marker pointing at nothing, and the rewrite
    for a keyless value would go back to being unappliable.

    The tag field is found by what it IS (the field closed to exactly
    this mode) rather than by spelling ``mode``, so the discriminator can
    be renamed without this quietly passing on a subtraction that no
    longer subtracts anything.
    """
    for name, shape in _declared_shapes().items():
        model = capability_config_model("vm-platform", name)
        assert model is not None
        union_doc = next(doc for doc in iter_field_docs(model) if doc.path == (shape.union_field,))
        arm = {arm.tag: arm.doc.model for arm in union_doc.union_arms}[shape.present_mode]
        beyond_the_tag = [doc.path for doc in iter_field_docs(arm) if doc.choices != (shape.present_mode,)]
        assert beyond_the_tag, (
            f"{name}: the '{shape.present_mode}' arm has no fields beyond its tag, so the elided "
            f"'...' in its rewrite stands for nothing"
        )


def test_a_platform_with_no_retired_shape_is_untouched() -> None:
    """The declaration is opt-in, so a platform that never broke its
    config validates exactly as before: proxmox has always required its
    token fields, and wsl2 takes no configuration at all."""
    validate_capability_config(kind="vm-platform", config={"name": "wsl2"}, owner=OWNER)
    message = str(_refuse("proxmox", {"api_url": "https://pve:8006", "node": "n", "token_id": "t"}))
    assert "template_vmid: is required" in message
    assert "no longer a supported field" not in message


# -- Git token null: the one written spelling the nested union retires -------


@pytest.mark.parametrize(
    ("provider", "base"),
    [pytest.param("github", {}, id="github"), pytest.param("azdo", {"org": "acme"}, id="azdo")],
)
def test_git_token_null_prints_an_exact_working_rewrite(provider: str, base: dict[str, object]) -> None:
    """Both historical stored-token providers declare the retirement.

    The scalar token spelling and omission still load. Explicit null is
    the one written old shape that changed, so it names a replacement that
    preserves the old default-secret behavior and validates when applied.
    """
    owner = RefOwner(kind="git-credential", name="dev")
    with pytest.raises(ConfigError) as excinfo:
        validate_capability_config(
            kind="git-credential-provider",
            config={"name": provider, **base, "token": None},
            owner=owner,
            location=_WHERE,
        )
    error = excinfo.value
    assert str(error).startswith("/etc/agentworks/resources/sites.yaml:12: git-credential/dev")
    assert "replace the null line with the explicit choice: token: {mode: stored}" in str(error)
    assert error.hint == GIT_TOKEN_NULL_HINT

    rewrite = yaml.safe_load("token: {mode: stored}")
    validate_capability_config(
        kind="git-credential-provider",
        config={"name": provider, **base, **rewrite},
        owner=owner,
    )


def test_a_future_git_provider_does_not_inherit_the_release_specific_retirement() -> None:
    """Only implementations that accepted ``token: null`` may diagnose
    it as retired; the provider contract itself has no such history."""

    class FutureProvider(GitCredentialProvider):
        pass

    assert GitCredentialProvider.retired_shape is None
    assert FutureProvider.retired_shape is None
