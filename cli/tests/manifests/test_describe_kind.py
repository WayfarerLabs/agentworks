"""``agw resource describe-kind``: the field reference an operator reads.

What is worth pinning is what the surface PROMISES: that it answers for a
kind and for one capability implementation, that it needs neither a config
nor a registry (so it answers on a broken host, and about a capability
whose plugin is not enabled), and that every fact in it is derived rather
than authored twice.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Annotated, ClassVar, Literal

import pytest
from pydantic import Discriminator, Field

from agentworks.errors import ConfigError
from agentworks.manifests.describe import reference_lines
from agentworks.manifests.loader import load_manifests
from agentworks.manifests.reference import describable_targets, reference_for
from agentworks.plugins import Plugin, seated_plugin
from agentworks.schema import AgwModel
from tests.plugins._fixtures import ConformingVMPlatform

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


class LocalDisk(AgwModel):
    """A disk carved out of the host's own storage."""

    kind: Literal["local"]
    size_gb: int = 40


class NetworkDisk(AgwModel):
    """A disk attached over the network."""

    kind: Literal["network"]
    volume: str


class DisabledConfig(AgwModel):
    """A fixture platform's config."""

    name: Literal["never-enabled"]
    region: str = "westus2"
    """Where this fixture platform creates its VMs."""
    disks: list[Annotated[LocalDisk | NetworkDisk, Discriminator("kind")]] = Field(default_factory=list)
    """The disks every VM gets, each saying which kind of disk it is."""


class DisabledPlatform(ConformingVMPlatform):
    name: ClassVar[str] = "never-enabled"
    description: ClassVar[str] = "a fixture platform no config opts into"
    config_model: ClassVar[type[AgwModel]] = DisabledConfig


@pytest.fixture
def seated() -> Iterator[None]:
    """The fixture platform in the live registry, seated through the
    shipped plugin machinery. Nothing enables its plugin, which is the
    point: registration is what publishes a capability's schema, and
    enablement is a property of the published ROW."""
    with seated_plugin(Plugin(name="describe-kind-fixtures", capabilities={"vm-platform": (DisabledPlatform,)})):
        yield


def _text(target: str) -> str:
    return "\n".join(reference_lines(reference_for(target)))


def _field_entry(text: str, field: str) -> str:
    """One field's rendered block, unwrapped onto a single line.

    Bounded by the next field heading, so an assertion cannot pass on
    prose that belongs to the field below. Unwrapped because the renderer
    fills to a width, which puts the line break wherever it lands.
    """
    headings = list(re.finditer(r"^( *)(\S+) {2}\(", text, re.MULTILINE))
    for index, heading in enumerate(headings):
        if heading.group(2) != field:
            continue
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        return " ".join(text[heading.start() : end].split())
    raise AssertionError(f"{field} is not a field in:\n{text}")


# --- a declarable kind ------------------------------------------------


def test_a_kind_shows_its_prose_then_its_two_blocks() -> None:
    text = _text("vm-site")

    assert text.startswith("VM sites (vm-site, resource kind)")
    # The summary is the kind's own one-line description, not a second
    # authored string (see agentworks/topics.py).
    assert "Configured places to create VMs" in text
    assert "A vm-site is a configured place VMs come from" in text
    assert "\nmetadata:\n" in text
    assert "\nspec:\n" in text


def test_a_field_carries_its_type_requiredness_and_description() -> None:
    text = _text("secret")

    # The example renders as YAML, not as Python `repr`: this reader is
    # about to write it into a document.
    assert "  hint  (string or null, optional, e.g. Generate at https://" in text
    assert "Operator-facing text shown when the secret has to be entered by hand" in text


def test_a_kind_points_at_the_sample_that_renders_the_same_fields() -> None:
    assert "`agw resource sample secret`" in _text("secret")


# --- a capability kind and its implementations ------------------------


def test_a_capability_kind_is_an_index_of_implementations() -> None:
    text = _text("vm-platform")

    assert "implementations:" in text
    assert "  lima" in text
    assert "Lima VMs (local, or on a remote host via SSH)" in text
    assert "`agw resource describe-kind vm-platform/<name>`" in text


def test_an_implementation_shows_the_config_it_declares() -> None:
    text = _text("vm-platform/lima")

    assert text.startswith("Lima (vm-platform/lima, vm-platform implementation)")
    assert "config:" in text
    assert "vm_host  (string or null, optional, min length 1, e.g. me@gpu-box)" in text


def test_a_host_kind_lists_the_arms_and_marks_the_one_shown() -> None:
    text = _text("vm-site")

    assert "- lima (shown below): Lima VMs (local, or on a remote host via SSH)" in text
    assert "- proxmox: Proxmox VE cluster VMs (clone + cloud-init)" in text


def test_a_host_kind_gives_an_address_for_the_arms_it_did_not_expand() -> None:
    """Listing five platforms and expanding one leaves the operator with no
    way to read the other four, which is exactly the question the list
    provokes."""
    assert "`agw resource describe-kind vm-platform/" in _text("vm-site")


def test_a_capability_kind_summary_does_not_enumerate_its_implementations() -> None:
    """The summary renders directly above the live list from the registry,
    so a parenthetical naming implementations is a second enumeration that
    can go stale against the list below it. harness-integration's said
    "(shell, claude-code)" from before codex shipped until 2.8's review."""
    text = _text("harness-integration")
    summary = text.splitlines()[1]

    assert "codex" in text, "the registry list is what names them"
    assert "(" not in summary, summary


# --- what the surface does NOT need -----------------------------------


def test_a_capability_no_config_enables_still_documents_itself(seated: None) -> None:
    """The promise that separates this surface from every other one: a
    plugin's capability is registered whether or not config opts into it,
    so an operator can read about a platform BEFORE enabling it. Nothing
    here loads config or builds a registry, and nothing constructs the
    implementation."""
    text = _text("vm-platform/never-enabled")

    assert "a fixture platform no config opts into" in text
    assert "region  (string, optional, default westus2)" in text
    assert "Where this fixture platform creates its VMs." in text


def test_a_collection_of_tagged_blocks_names_its_arms_and_expands_one(seated: None) -> None:
    """The surface an operator actually reads, over the shape no shipped
    config has.

    A list whose elements each say which kind of element they are was
    rendered as "list of table" and nothing else: the tags were
    undiscoverable from the CLI, from the generated sample, and from the
    guide, though the loader dispatched on them and the dependency graph
    walked them.
    """
    block = _field_entry(_text("vm-platform/never-enabled"), "disks")

    assert "disks (list of table, optional)" in block
    assert "The disks every VM gets, each saying which kind of disk it is." in block
    assert "- (each element) (table, required)" in block
    assert "- local (shown below): A disk carved out of the host's own storage." in block
    assert "- network: A disk attached over the network." in block


def test_an_expanded_element_arm_shows_that_arms_own_fields(seated: None) -> None:
    """One arm's fields, at the element's indent: what an operator writes
    under the ``-``, rather than a table of everything every arm takes."""
    text = _text("vm-platform/never-enabled")

    assert "size_gb  (integer, optional, default 40)" in text
    assert "volume" not in text, "the arm that was not expanded contributes no fields"


def test_a_seated_capability_is_addressable_and_an_unseated_one_is_not(seated: None) -> None:
    assert "vm-platform/never-enabled" in describable_targets()


def test_the_target_list_covers_kinds_and_implementations() -> None:
    targets = describable_targets()

    assert "secret" in targets
    assert "vm-platform" in targets
    assert "vm-platform/lima" in targets
    assert "vm-platform/never-enabled" not in targets, "the fixture is seated only inside its fixture"


# --- the quoted-boolean trap ------------------------------------------

#: Every field whose old TOML loader read it through ``bool(...)``, so a
#: quoted ``no`` meant TRUE. Named as ``describe-kind`` targets, because
#: the field reference is the surface every one of those errors points at.
_INVERTING_BOOLEANS = [
    ("apt-source", "key_dearmor"),
    ("admin-template", "mise_activate"),
    ("admin-template", "mise_allow_unlocked"),
    ("admin-template", "mise_prune_on_reinit"),
    ("admin-template", "git_force_safe_directory"),
    ("agent-template", "mise_activate"),
    ("agent-template", "mise_allow_unlocked"),
    ("agent-template", "mise_prune_on_reinit"),
    ("workspace-template", "tmuxinator"),
    ("vm-platform/proxmox", "verify_ssl"),
]


@pytest.mark.parametrize(("target", "field"), _INVERTING_BOOLEANS)
def test_a_boolean_that_used_to_invert_says_so(target: str, field: str) -> None:
    """``must be a boolean`` is not enough to act on for these.

    Each of these was read through ``bool(...)``, so ``key_dearmor: "no"``
    meant TRUE: the opposite of what it reads as. An operator meeting the
    new type error and writing the obvious `false` silently flips the
    behavior they had, and the error's own remedy is to come here, so the
    warning has to be here for the whole class rather than for the one
    field somebody happened to think of.

    The warning names the QUOTED spelling, and the assertion pins that
    it does. It used to say a bare ``no`` was a string, which described
    the retired TOML loader: manifests are YAML, where a bare ``no`` is a
    boolean and means exactly what it reads as. Warning about the wrong
    spelling is worse than not warning, because the whole hazard on these
    fields is a silent inversion.
    """
    entry = _field_entry(_text(target), field)

    assert "used to mean TRUE, the opposite of what it reads as" in entry, entry
    assert 'QUOTED `"no"`' in entry, entry


def test_the_quoted_boolean_warning_describes_this_loader(tmp_path: Path) -> None:
    """The property the warning asserts, executed against the loader.

    A string match on prose only proves the prose did not change. What
    makes the warning TRUE is the loader: a bare ``no`` is a boolean
    (pyyaml resolves YAML 1.1) and the quoted one is a string the strict
    models refuse. Pinned here, beside the text it justifies, so a change
    to either has to face the other.

    One field stands for the list above. The rule is the parser's and the
    strict models', not any field's: every entry is a plain ``bool``, so
    a second case would exercise the same two lines of pydantic.
    """
    manifest = (
        "apiVersion: agentworks/v1\nkind: apt-source\nmetadata:\n  name: docker\nspec:\n"
        "  key_url: https://example.com/k.gpg\n  key_path: /etc/apt/keyrings/docker.gpg\n"
        "  source: deb https://example.com stable main\n  source_file: docker.list\n"
        "  key_dearmor: {value}\n"
    )
    resources = tmp_path / "resources"
    resources.mkdir()

    (resources / "bare.yaml").write_text(manifest.format(value="no"))
    (entry,) = load_manifests(resources).entries
    assert entry.resource.key_dearmor is False, "a bare `no` means false, exactly as it reads"

    (resources / "bare.yaml").write_text(manifest.format(value='"no"'))
    with pytest.raises(ConfigError, match="key_dearmor"):
        load_manifests(resources)


# --- the CLI ----------------------------------------------------------


def test_describe_kind_is_a_clean_cli_error_for_an_unknown_target(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A typed domain error, one clean line, no traceback: the same
    contract `resource sample` has for the same mistake."""
    from agentworks import cli as cli_mod

    monkeypatch.setattr("sys.argv", ["agentworks", "resource", "describe-kind", "vm-platfrom"])
    monkeypatch.setenv("AGW_DEBUG", "")

    with pytest.raises(SystemExit) as excinfo:
        cli_mod.main()

    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert "unknown kind 'vm-platfrom'" in err
    assert "Traceback" not in err


def test_the_command_completes_its_argument() -> None:
    """Any CLI surface an operator types needs its completion entry; the
    kinds completer is config-free, which matters for a command whose
    reason to exist includes a host whose config does not load."""
    from agentworks.completions.spec import DYNAMIC_COMPLETIONS

    assert DYNAMIC_COMPLETIONS[("resource.describe-kind", "target")] == "resource_kinds"
