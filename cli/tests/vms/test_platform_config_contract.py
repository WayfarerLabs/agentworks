"""The declared-config contract on the five VM platforms.

Each platform DECLARES its config as a model; the core validates a blob
against it and extracts the references it implies. Nothing here calls a
platform: that is the point of the flip, so these tests go through the
core entry points and would fail if a platform were reachable at all.

The two halves stay pinned separately, because their contracts differ:
validation THROWS on a malformed blob, and extraction is total and never
raises, emitting every edge whose identity survives.
"""

from __future__ import annotations

import pytest

from agentworks.capabilities.config import (
    capability_config_model,
    capability_config_references,
    resolved_capability_modes,
    validate_capability_config,
)
from agentworks.capabilities.vm_platform import VM_PLATFORM_REGISTRY
from agentworks.capabilities.vm_platform.lima import LimaPlatform
from agentworks.capabilities.vm_platform.wsl2 import WSL2Platform
from agentworks.errors import ConfigError
from agentworks.plugins.aws.platform import EC2Platform
from agentworks.plugins.azure.platform import AzureVMPlatform
from agentworks.plugins.proxmox.platform import ProxmoxPlatform
from agentworks.schema import RefOwner
from agentworks.schema._shape import shape_of
from agentworks.schema.reference import ConfigReference

#: The well-known secret names the three credential-bearing platforms
#: default to. Spelled out rather than imported, so a rename of one has
#: to change this file too: they are operator-facing conventions (the
#: env-var backend reads ``AW_SECRET_<NAME>`` off them), not internals.
AWS_DEFAULT_SECRET = "aws-secret-access-key"
AZURE_DEFAULT_SECRET = "azure-client-secret"
PROXMOX_DEFAULT_SECRET = "proxmox-token"

#: The arm every site that is not testing credentials selects. Written
#: out at every call site's base config even though the unions default to
#: it now, so each case reads whole; the defaulting itself is pinned once,
#: in ``test_the_union_defaults_to_the_mode_omission_used_to_select``.
AMBIENT_AUTH = {"mode": "ambient"}

AZURE_CONFIG = {
    "subscription_id": "0000",
    "resource_group": "agw",
    "region": "eastus",
    "auth": AMBIENT_AUTH,
}
AZURE_SP = {
    "mode": "service-principal",
    "tenant_id": "tenant-0000",
    "client_id": "client-0000",
    "secret": "az-sp",
}
PROXMOX_CONFIG = {
    "api_url": "https://pve:8006",
    "node": "pve1",
    "token_id": "agw@pam!agw",
    "template_vmid": 9000,
}
EC2_CONFIG = {"region": "us-east-1", "auth": AMBIENT_AUTH}
EC2_CREDS = {"mode": "access-key", "access_key_id": "AKIAEXAMPLE", "access_key_secret": "aws-secret"}
LIMA_LOCAL = {"placement": {"mode": "local"}}
LIMA_SSH = {"placement": {"mode": "ssh", "host": "me@box"}}

OWNER = RefOwner(kind="vm-site", name="t")


def _validate(platform: str, blob: dict[str, object]) -> None:
    """The tag is the selector, so a caller assembles the table the host
    row carries rather than naming the platform beside its config."""
    validate_capability_config(kind="vm-platform", config={"name": platform, **blob}, owner=OWNER)


def _refs(platform: str, blob: dict[str, object]) -> tuple[ConfigReference, ...]:
    return capability_config_references(kind="vm-platform", config={"name": platform, **blob}, owner=OWNER)


def test_registry_names_match_classes() -> None:
    assert {
        "lima": LimaPlatform,
        "wsl2": WSL2Platform,
        "azure-vm": AzureVMPlatform,
        "proxmox": ProxmoxPlatform,
        "aws-ec2": EC2Platform,
    } == VM_PLATFORM_REGISTRY
    for name, cls in VM_PLATFORM_REGISTRY.items():
        assert cls.name == name
        assert cls.description


# -- Validation (the throwing shape check) -----------------------------------


def test_lima_accepts_either_placement() -> None:
    _validate("lima", LIMA_LOCAL)
    _validate("lima", LIMA_SSH)


def test_lima_rejects_bad_host_and_unknown_keys() -> None:
    with pytest.raises(ConfigError, match="placement.host: must not be empty"):
        _validate("lima", {"placement": {"mode": "ssh", "host": ""}})
    with pytest.raises(ConfigError, match="host: unknown field; expected one of: name, placement"):
        _validate("lima", {**LIMA_LOCAL, "host": "x"})


def test_wsl2_accepts_no_configuration() -> None:
    _validate("wsl2", {})
    with pytest.raises(ConfigError, match="anything: unknown field"):
        _validate("wsl2", {"anything": 1})


def test_azure_requires_the_three_location_keys_and_defaults_auth() -> None:
    """The three location keys are required; ``auth`` is not, because it
    carries the declared ambient default (pinned with its siblings in
    ``test_the_union_defaults_to_the_mode_omission_used_to_select``)."""
    _validate("azure-vm", AZURE_CONFIG)
    for missing in ("subscription_id", "resource_group", "region"):
        broken = {k: v for k, v in AZURE_CONFIG.items() if k != missing}
        with pytest.raises(ConfigError, match=f"'?{missing}'?[: ].*required"):
            _validate("azure-vm", broken)
    _validate("azure-vm", {k: v for k, v in AZURE_CONFIG.items() if k != "auth"})
    with pytest.raises(ConfigError, match="extra: unknown field"):
        _validate("azure-vm", {**AZURE_CONFIG, "extra": "x"})


def test_azure_service_principal_arm_is_shape_checked() -> None:
    """The ``service-principal`` arm must carry both identifiers, may name
    its secret, and rejects anything else."""
    _validate("azure-vm", {**AZURE_CONFIG, "auth": AZURE_SP})
    # ``secret`` is optional (the default name applies).
    _validate("azure-vm", {**AZURE_CONFIG, "auth": {k: v for k, v in AZURE_SP.items() if k != "secret"}})
    for missing in ("tenant_id", "client_id"):
        broken = {k: v for k, v in AZURE_SP.items() if k != missing}
        with pytest.raises(ConfigError, match=f"auth.{missing}: is required"):
            _validate("azure-vm", {**AZURE_CONFIG, "auth": broken})


@pytest.mark.parametrize(
    ("auth", "match"),
    [
        pytest.param("not-a-table", "auth: must be a table", id="not-a-table"),
        pytest.param({**AZURE_SP, "tenant_id": ""}, "tenant_id: must not be empty", id="empty-tenant"),
        pytest.param({**AZURE_SP, "client_id": 7}, "client_id: must be a string", id="non-string-client"),
        pytest.param({**AZURE_SP, "secret": ""}, "secret: must not be empty", id="empty-secret-name"),
        pytest.param({**AZURE_SP, "secret": 7}, "secret: must be a string", id="non-string-secret-name"),
        # The field is deliberately named `secret` (a NAME), so the
        # value-shaped spelling an operator might reach for is refused.
        pytest.param({**AZURE_SP, "client_secret": "hunter2"}, "unknown field", id="client-secret-value"),
        pytest.param({**AZURE_SP, "certificate": "x"}, "unknown field", id="future-variant-not-yet"),
    ],
)
def test_azure_rejects_malformed_service_principal(auth: object, match: str) -> None:
    with pytest.raises(ConfigError, match=match):
        _validate("azure-vm", {**AZURE_CONFIG, "auth": auth})


def test_aws_ec2_requires_region() -> None:
    _validate("aws-ec2", EC2_CONFIG)
    with pytest.raises(ConfigError, match="region: is required"):
        _validate("aws-ec2", {"auth": AMBIENT_AUTH})
    with pytest.raises(ConfigError, match="extra: unknown field"):
        _validate("aws-ec2", {**EC2_CONFIG, "extra": "x"})


def test_aws_ec2_optional_subnet_id_is_shape_checked() -> None:
    _validate("aws-ec2", {**EC2_CONFIG, "subnet_id": "subnet-1"})
    with pytest.raises(ConfigError, match="subnet_id: must not be empty"):
        _validate("aws-ec2", {**EC2_CONFIG, "subnet_id": ""})


def test_aws_ec2_rejects_the_removed_ami_override() -> None:
    """There is no image knob: the fleet standardizes on Debian bookworm, so an
    ``ami`` key is an unknown field, not a pin."""
    with pytest.raises(ConfigError, match="ami: unknown field"):
        _validate("aws-ec2", {**EC2_CONFIG, "ami": "ami-123"})


def test_aws_ec2_access_key_arm_is_shape_checked() -> None:
    """The ``access-key`` arm must carry access_key_id, may name its
    secret and a role, and rejects anything else."""
    _validate("aws-ec2", {**EC2_CONFIG, "auth": EC2_CREDS})
    _validate("aws-ec2", {**EC2_CONFIG, "auth": {"mode": "access-key", "access_key_id": "AKIA"}})
    _validate("aws-ec2", {**EC2_CONFIG, "auth": {**EC2_CREDS, "assume_role_arn": "arn:x"}})


@pytest.mark.parametrize(
    ("auth", "match"),
    [
        pytest.param("not-a-table", "auth: must be a table", id="not-a-table"),
        pytest.param({"mode": "access-key"}, "access_key_id: is required", id="missing-access-key"),
        pytest.param({**EC2_CREDS, "access_key_id": ""}, "access_key_id: must not be empty", id="empty-access-key"),
        pytest.param({**EC2_CREDS, "access_key_secret": ""}, "must not be empty", id="empty-secret-name"),
        pytest.param({**EC2_CREDS, "access_key_secret": 7}, "must be a string", id="non-string-secret-name"),
        pytest.param({**EC2_CREDS, "assume_role_arn": ""}, "assume_role_arn: must not be empty", id="empty-role"),
        # The field is `access_key_secret` (a NAME), so AWS's own value-shaped
        # term and the old `secret` spelling are both refused as unknown fields.
        pytest.param({**EC2_CREDS, "secret_access_key": "hunter2"}, "unknown field", id="secret-value-spelling"),
        pytest.param({**EC2_CREDS, "secret": "aws-secret"}, "unknown field", id="old-secret-spelling"),
    ],
)
def test_aws_ec2_rejects_malformed_credentials(auth: object, match: str) -> None:
    with pytest.raises(ConfigError, match=match):
        _validate("aws-ec2", {**EC2_CONFIG, "auth": auth})


def test_aws_ec2_rejects_bad_instance_type_arch() -> None:
    bad = {**EC2_CONFIG, "instance_types": [{"cpus": 2, "memory": 4, "type": "x", "arch": "amd64"}]}
    with pytest.raises(ConfigError, match=r"instance_types\[0\].arch: must be one of"):
        _validate("aws-ec2", bad)


def test_a_catalog_entry_is_addressed_by_its_index() -> None:
    """A catalog is a list of tables, so the operator's address for a bad
    entry includes which one it is."""
    bad = {**EC2_CONFIG, "instance_types": [{"cpus": 2, "memory": 4, "type": "a", "arch": "arm64"}, {"cpus": 0}]}
    with pytest.raises(ConfigError, match=r"instance_types\[1\]"):
        _validate("aws-ec2", bad)


def test_a_catalog_must_be_a_list_not_a_tuple_shaped_thing() -> None:
    """Operator-writable sequences are lists: YAML produces one, and
    strict mode accepts nothing else."""
    with pytest.raises(ConfigError, match="vm_sizes: must be a list"):
        _validate("azure-vm", {**AZURE_CONFIG, "vm_sizes": {"cpus": 2}})


def test_proxmox_validation_errors() -> None:
    _validate("proxmox", PROXMOX_CONFIG)
    with pytest.raises(ConfigError, match="node: is required"):
        _validate("proxmox", {k: v for k, v in PROXMOX_CONFIG.items() if k != "node"})
    with pytest.raises(ConfigError, match="token_secret: must not be empty"):
        _validate("proxmox", {**PROXMOX_CONFIG, "token_secret": ""})
    with pytest.raises(ConfigError, match="nodee: unknown field"):
        _validate("proxmox", {**PROXMOX_CONFIG, "nodee": "x"})


# -- The deliberate breaks ---------------------------------------------------


def test_proxmox_no_longer_accepts_a_quoted_template_vmid() -> None:
    """BREAKING, and taken knowingly: the shipped validator did
    ``int(str(...))``, so a quoted number loaded. Strict mode does not
    coerce, and a quoted number where an integer belongs is an operator
    mistake rather than a value to convert."""
    with pytest.raises(ConfigError, match="template_vmid: must be an integer"):
        _validate("proxmox", {**PROXMOX_CONFIG, "template_vmid": "9000"})
    with pytest.raises(ConfigError, match="template_vmid: must be an integer"):
        _validate("proxmox", {**PROXMOX_CONFIG, "template_vmid": "not-a-number"})


def test_proxmox_no_longer_reads_a_string_verify_ssl_as_true() -> None:
    """BREAKING, and the more important half: ``verify_ssl: "no"`` was
    consumed as ``bool("no")``, which is TRUE, so the config did the
    opposite of what it read as. It is an error now.

    The message names the QUOTES, not just the type, and that matters
    more than it used to. The emitted schema deliberately accepts a
    quoted ``"no"`` (under YAML 1.2 it is the same parsed string as the
    valid bare ``no``, so the widening that stopped editors underlining
    the bare form made them silent on this one), so this error is the
    only signal an operator gets. "Must be a boolean" alone reads as a
    contradiction to someone looking at a line that says ``no``.
    """
    _validate("proxmox", {**PROXMOX_CONFIG, "verify_ssl": False})
    with pytest.raises(ConfigError, match="verify_ssl: must be a boolean, and 'no' is quoted") as excinfo:
        _validate("proxmox", {**PROXMOX_CONFIG, "verify_ssl": "no"})
    assert "write it unquoted" in str(excinfo.value)

    # A value the quotes are not the story for keeps the plain phrasing.
    with pytest.raises(ConfigError, match="verify_ssl: must be a boolean$"):
        _validate("proxmox", {**PROXMOX_CONFIG, "verify_ssl": 5})


@pytest.mark.parametrize(
    ("platform", "blob", "expected"),
    [
        pytest.param("proxmox", {**PROXMOX_CONFIG, "token_secret": None}, PROXMOX_DEFAULT_SECRET, id="proxmox"),
        pytest.param(
            "azure-vm",
            {**AZURE_CONFIG, "auth": {**AZURE_SP, "secret": None}},
            AZURE_DEFAULT_SECRET,
            id="azure",
        ),
        pytest.param(
            "aws-ec2",
            {**EC2_CONFIG, "auth": {**EC2_CREDS, "access_key_secret": None}},
            AWS_DEFAULT_SECRET,
            id="aws",
        ),
    ],
)
def test_an_explicit_null_secret_name_now_means_the_default(
    platform: str, blob: dict[str, object], expected: str
) -> None:
    """BREAKING on all THREE credential-bearing platforms, not azure
    alone: each used to RAISE on an explicit ``null`` with a message
    telling the operator to omit the key, and each used to emit no edge.
    The model rule is that absent or ``None`` yields the owner template,
    which is what git-credential's token already did, so the four are
    consistent now. An operator who followed the old error's advice will
    not otherwise connect the two, which is why the upgrade note has to
    name all three."""
    _validate(platform, blob)
    (ref,) = _refs(platform, blob)
    assert ref.name == expected


# -- The tagged mode unions: the ways to get them wrong ----------------------
#
# azure's ``auth``, aws's ``auth``, and lima's ``placement`` are the same
# shape, so they are pinned together: whatever an operator does wrong, all
# three have to say so in the same words, at an address the operator wrote.
# The MESSAGE is asserted, not merely the raise (FR12): a union whose
# failures are unreadable would trade one bad diagnostic for another.

#: ``(platform, base config, the union's field name)`` for the three
#: platforms carrying a tagged mode union.
TAGGED = [
    pytest.param("azure-vm", AZURE_CONFIG, "auth", id="azure-vm"),
    pytest.param("aws-ec2", EC2_CONFIG, "auth", id="aws-ec2"),
    pytest.param("lima", LIMA_LOCAL, "placement", id="lima"),
]


@pytest.mark.parametrize(("platform", "config", "field"), TAGGED)
def test_the_union_defaults_to_the_mode_omission_used_to_select(
    platform: str, config: dict[str, object], field: str
) -> None:
    """Omitting the field selects the declared default, which is the
    same mechanism omission selected before the union existed (ambient
    on the clouds, local on lima). An earlier revision made omission an
    error; the operator ruling that reversed it is recorded at the union
    sites, and this pins both halves: the default is a fact of the MODEL
    (visible to describe-kind and the emitted schema, not conjured by a
    validator), and validation really resolves an omitting document to
    that arm.
    """
    model = capability_config_model("vm-platform", platform)
    assert model is not None
    default = model.model_fields[field].default
    assert getattr(default, "mode", None) == _MODES[platform][0]
    # And it really is a discriminated union rather than a lone block:
    # the default names one of several arms a document may write.
    assert shape_of(model.model_fields[field]).discriminator == "mode"

    without = {k: v for k, v in config.items() if k != field}
    validated = validate_capability_config(kind="vm-platform", config={"name": platform, **without}, owner=OWNER)
    assert validated is not None
    assert getattr(validated, field).mode == _MODES[platform][0]


@pytest.mark.parametrize(("platform", "config", "field"), TAGGED)
def test_an_unknown_mode_names_the_modes_there_are(platform: str, config: dict[str, object], field: str) -> None:
    """A tag no arm answers to is refused, and the message lists the tags
    that exist rather than leaving the operator to find them."""
    with pytest.raises(ConfigError) as exc:
        _validate(platform, {**config, field: {"mode": "no-such-mode"}})
    message = str(exc.value)
    assert message.startswith(f"vm-site/t.{field}: unknown mode 'no-such-mode'; ")
    for tag in _MODES[platform]:
        assert repr(tag) in message


@pytest.mark.parametrize(
    ("platform", "config", "field", "stray"),
    [
        pytest.param("azure-vm", AZURE_CONFIG, "auth", "tenant_id", id="azure-vm"),
        pytest.param("aws-ec2", EC2_CONFIG, "auth", "access_key_id", id="aws-ec2"),
        pytest.param("lima", LIMA_LOCAL, "placement", "host", id="lima"),
    ],
)
def test_a_field_from_the_other_arm_is_refused(
    platform: str, config: dict[str, object], field: str, stray: str
) -> None:
    """The arms are CLOSED, which is the property a nullable block beside
    an enum could not have: a credential field under the no-credential arm
    is an unknown field, caught by the emitted schema in the operator's
    editor as well as by the loader. The address is the field the operator
    wrote, with no arm-tag segment spliced into it."""
    tagless = {k: v for k, v in config.items() if k != field}
    mode = _MODES[platform][0]
    with pytest.raises(ConfigError) as exc:
        _validate(platform, {**tagless, field: {"mode": mode, stray: "x"}})
    assert str(exc.value).startswith(f"vm-site/t.{field}.{stray}: unknown field; expected one of: mode")


@pytest.mark.parametrize(("platform", "config", "field"), TAGGED)
def test_an_extra_field_inside_an_arm_is_refused(platform: str, config: dict[str, object], field: str) -> None:
    """An arm forbids extras like every other model here, so a typo inside
    one is named rather than ignored. This is the case that used to bite
    lima hardest: a misspelled host key read as ABSENT, which read as a
    local site, which reported a missing ``limactl`` the operator did not
    need."""
    mode = _MODES[platform][0]
    tagless = {k: v for k, v in config.items() if k != field}
    with pytest.raises(ConfigError) as exc:
        _validate(platform, {**tagless, field: {"mode": mode, "hst": "typo"}})
    assert str(exc.value).startswith(f"vm-site/t.{field}.hst: unknown field; expected one of: ")


#: Every mode each union answers to, first one first. The first is the
#: no-extra-fields arm on all three, which is what lets the mixed-arm and
#: extra-field cases above share one parametrization.
_MODES = {
    "azure-vm": ("ambient", "service-principal"),
    "aws-ec2": ("ambient", "access-key"),
    "lima": ("local", "ssh"),
}


# -- The resolved-mode read (what doctor's site rows render) -----------------


def _modes(platform: str, blob: dict[str, object]) -> tuple[tuple[str, str], ...]:
    return resolved_capability_modes(kind="vm-platform", config={"name": platform, **blob})


def test_resolved_modes_report_the_written_tag() -> None:
    assert _modes("lima", LIMA_SSH) == (("placement", "ssh"),)
    assert _modes("azure-vm", {**AZURE_CONFIG, "auth": AZURE_SP}) == (("auth", "service-principal"),)
    assert _modes("aws-ec2", {**EC2_CONFIG, "auth": EC2_CREDS}) == (("auth", "access-key"),)


def test_resolved_modes_report_the_declared_default_for_an_omitting_document() -> None:
    """The read that makes an IMPLICIT choice reviewable: a site that
    wrote no union has still resolved to an arm, and the answer is the
    same one validation gives it."""
    assert _modes("lima", {}) == (("placement", "local"),)
    assert _modes("azure-vm", {k: v for k, v in AZURE_CONFIG.items() if k != "auth"}) == (("auth", "ambient"),)
    assert _modes("aws-ec2", {"region": "us-east-1"}) == (("auth", "ambient"),)


def test_resolved_modes_are_total_over_what_validation_would_refuse() -> None:
    """A rendering caller degrades to the bare platform name, so an
    unknown implementation or a malformed union contributes no pair
    rather than an exception; validation is where those become errors."""
    assert resolved_capability_modes(kind="vm-platform", config={"name": "no-such-platform"}) == ()
    assert resolved_capability_modes(kind="vm-platform", config="nope") == ()
    assert _modes("lima", {"placement": "junk"}) == ()
    assert _modes("lima", {"placement": {"mode": 7}}) == ()
    assert _modes("wsl2", {}) == ()


# -- Extraction (total, never raising) ---------------------------------------


def test_config_free_platforms_imply_no_edges() -> None:
    assert _refs("lima", LIMA_SSH) == ()
    assert _refs("lima", LIMA_LOCAL) == ()
    assert _refs("wsl2", {}) == ()


def test_the_ambient_arm_emits_no_secret_reference() -> None:
    """The whole point of the ambient arm: a site that DELIBERATELY
    borrows the host's identity declares so, and still names no secret.
    The edge set is what tells "chose ambient" apart from "named a
    credential", and choosing ambient must not invent one."""
    assert _refs("azure-vm", AZURE_CONFIG) == ()
    assert _refs("aws-ec2", EC2_CONFIG) == ()
    # And it stays empty when the arm is the only thing in the blob, so
    # the emptiness is the ARM's property rather than the rest of the
    # config's.
    assert _refs("azure-vm", {"auth": AMBIENT_AUTH}) == ()
    assert _refs("aws-ec2", {"auth": AMBIENT_AUTH}) == ()
    # An ABSENT union resolves to the same ambient default and emits the
    # same zero edges: extraction reads defaults as if written, and this
    # default names nothing.
    assert _refs("azure-vm", {k: v for k, v in AZURE_CONFIG.items() if k != "auth"}) == ()
    assert _refs("aws-ec2", {k: v for k, v in EC2_CONFIG.items() if k != "auth"}) == ()
    assert _refs("lima", {}) == ()


def test_aws_ec2_returns_the_secret_access_key_reference() -> None:
    (ref,) = _refs("aws-ec2", {**EC2_CONFIG, "auth": EC2_CREDS})
    assert (ref.kind, ref.name) == ("secret", "aws-secret")
    assert ref.usage == "the AWS secret access key"

    # Omitting ``access_key_secret`` falls back to the well-known default name.
    (ref,) = _refs("aws-ec2", {**EC2_CONFIG, "auth": {"mode": "access-key", "access_key_id": "AKIA"}})
    assert ref.name == AWS_DEFAULT_SECRET


def test_aws_ec2_extraction_is_total_on_malformed_config() -> None:
    """Extraction never raises. The edge's identity is the secret NAME,
    so it emits even when the arm's other fields are missing or malformed, and
    is omitted only when the arm itself, or the field naming the edge, is
    unusable."""
    (ref,) = _refs("aws-ec2", {"auth": {"mode": "access-key", "access_key_secret": "aws-secret"}})
    assert ref.name == "aws-secret"
    (ref,) = _refs("aws-ec2", {**EC2_CONFIG, "auth": {"mode": "access-key"}})
    assert ref.name == AWS_DEFAULT_SECRET
    assert _refs("aws-ec2", {**EC2_CONFIG, "auth": {**EC2_CREDS, "access_key_secret": ""}}) == ()
    assert _refs("aws-ec2", {**EC2_CONFIG, "auth": "nope"}) == ()
    # No tag names no arm, so nothing is walked: an omitted or unknown
    # ``mode`` contributes no edge rather than guessing which arm was meant.
    assert _refs("aws-ec2", {**EC2_CONFIG, "auth": {"access_key_secret": "aws-secret"}}) == ()
    assert _refs("aws-ec2", {**EC2_CONFIG, "auth": {"mode": "profile", "access_key_secret": "aws-secret"}}) == ()


def test_azure_returns_the_client_secret_reference() -> None:
    (ref,) = _refs("azure-vm", {**AZURE_CONFIG, "auth": AZURE_SP})
    assert (ref.kind, ref.name) == ("secret", "az-sp")
    assert "service-principal" in ref.usage

    # Omitting ``secret`` falls back to the well-known default name.
    no_name = {k: v for k, v in AZURE_SP.items() if k != "secret"}
    (ref,) = _refs("azure-vm", {**AZURE_CONFIG, "auth": no_name})
    assert ref.name == AZURE_DEFAULT_SECRET


def test_azure_extraction_is_total_on_malformed_config() -> None:
    """Extraction never raises. The edge's identity is the secret NAME,
    so it emits even when the arm's other fields are missing or
    malformed, and is omitted only when the arm itself, or the field
    naming the edge, is unusable."""
    # Nothing but the tag and the secret name: the edge still emits.
    (ref,) = _refs("azure-vm", {"auth": {"mode": "service-principal", "secret": "az-sp"}})
    assert ref.name == "az-sp"
    # The bare tag is still a declared service principal, so the
    # default-named edge emits (validation is what rejects the shape).
    (ref,) = _refs("azure-vm", {**AZURE_CONFIG, "auth": {"mode": "service-principal"}})
    assert ref.name == AZURE_DEFAULT_SECRET
    # An arm whose OTHER fields are malformed still emits (their shape
    # does not change what the edge points at).
    assert _refs("azure-vm", {**AZURE_CONFIG, "auth": {**AZURE_SP, "tenant_id": 3}}) == (
        ConfigReference(kind="secret", name="az-sp", usage="the Azure service-principal client secret"),
    )
    # A malformed name, or a non-table, makes the edge underivable, so it
    # is omitted (never raised).
    assert _refs("azure-vm", {**AZURE_CONFIG, "auth": {**AZURE_SP, "secret": ""}}) == ()
    assert _refs("azure-vm", {**AZURE_CONFIG, "auth": {**AZURE_SP, "secret": 3}}) == ()
    assert _refs("azure-vm", {**AZURE_CONFIG, "auth": "nope"}) == ()
    # No tag names no arm (see the aws twin).
    assert _refs("azure-vm", {**AZURE_CONFIG, "auth": {"secret": "az-sp"}}) == ()
    assert _refs("azure-vm", {**AZURE_CONFIG, "auth": {"mode": "managed-identity", "secret": "az-sp"}}) == ()


def test_proxmox_returns_the_token_secret_reference() -> None:
    (ref,) = _refs("proxmox", PROXMOX_CONFIG)
    assert (ref.kind, ref.name) == ("secret", PROXMOX_DEFAULT_SECRET)
    assert "token" in ref.usage

    (ref,) = _refs("proxmox", {**PROXMOX_CONFIG, "token_secret": "my-token"})
    assert ref.name == "my-token"


def test_proxmox_extraction_is_total_on_malformed_config() -> None:
    """Extraction never raises: it emits the token edge best-effort
    even when OTHER required fields are missing (their absence does not
    change the edge's identity), and omits the edge only when its own
    identity field (``token_secret``) is malformed."""
    # Every other required key missing: the token edge still emits.
    (ref,) = _refs("proxmox", {"token_secret": "my-token"})
    assert ref.name == "my-token"
    # A malformed token_secret makes the edge's identity underivable, so
    # the edge is omitted (never raised).
    assert _refs("proxmox", {**PROXMOX_CONFIG, "token_secret": ""}) == ()
    assert _refs("proxmox", {**PROXMOX_CONFIG, "token_secret": 3}) == ()


def test_proxmox_extraction_matches_valid_config_extraction() -> None:
    """For a valid blob, extraction yields exactly the edge the retired
    hand-rolled method returned (the pre-flip golden): one secret
    reference to the default token secret."""
    assert _refs("proxmox", PROXMOX_CONFIG) == (
        ConfigReference(kind="secret", name=PROXMOX_DEFAULT_SECRET, usage="the Proxmox API token"),
    )


def test_extraction_is_pure() -> None:
    """It runs at construct AND at finalize; two calls must agree."""
    assert _refs("proxmox", PROXMOX_CONFIG) == _refs("proxmox", PROXMOX_CONFIG)


def test_legacy_platform_metadata_hooks() -> None:
    lima_row = {"name": "dev", "wsl_distro_name": None, "proxmox_vmid": None}
    assert LimaPlatform.legacy_platform_metadata(lima_row, {}) == {"instance_name": "dev"}
    wsl_row = {"name": "dev", "wsl_distro_name": "dev", "proxmox_vmid": None}
    assert WSL2Platform.legacy_platform_metadata(wsl_row, {}) == {"distro_name": "dev"}
    wsl_row_null = {"name": "dev", "wsl_distro_name": None}
    assert WSL2Platform.legacy_platform_metadata(wsl_row_null, {}) == {"distro_name": "dev"}
    az_row = {"name": "dev", "azure_resource_id": "/subscriptions/s/x"}
    assert AzureVMPlatform.legacy_platform_metadata(az_row, {}) == {"resource_id": "/subscriptions/s/x"}
    az_row_null = {"name": "dev", "azure_resource_id": None}
    assert AzureVMPlatform.legacy_platform_metadata(az_row_null, {}) == {}
    # ec2 ships after the DB migration, so it has no legacy rows to map: the
    # base no-op return of {} is correct regardless of the row's contents.
    assert EC2Platform.legacy_platform_metadata({"name": "dev", "azure_resource_id": "/x"}, {}) == {}
    px_row = {"name": "dev", "proxmox_vmid": "104"}
    assert ProxmoxPlatform.legacy_platform_metadata(px_row, {}) == {"vmid": "104"}
    assert ProxmoxPlatform.legacy_platform_metadata(px_row, {"proxmox": {"node": "pve1"}}) == {
        "vmid": "104",
        "node": "pve1",
    }
