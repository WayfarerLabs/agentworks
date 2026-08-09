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

    from agentworks.manifests.field_tree import Alternative, FieldEntry


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

    Bounded by the next SIBLING heading, meaning the next one indented no
    further than this one, so an assertion cannot pass on prose that
    belongs to the field below and the block still carries what is nested
    inside it: a union's arms, and the fields under each arm, are part of
    the field they belong to. Unwrapped because the renderer fills to a
    width, which puts the line break wherever it lands.
    """
    headings = list(re.finditer(r"^( *)(\S+) {2}\(", text, re.MULTILINE))
    for index, heading in enumerate(headings):
        if heading.group(2) != field:
            continue
        depth = len(heading.group(1))
        after = (found for found in headings[index + 1 :] if len(found.group(1)) <= depth)
        sibling = next(after, None)
        end = sibling.start() if sibling is not None else len(text)
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
    assert "<key>  (string or one of: false, required, names a secret-source)" in text


def test_secret_source_kind_describes_the_backend_union_and_override_provenance() -> None:
    text = _text("secret-source")

    assert text.startswith("Secret sources (secret-source, resource kind)")
    assert "operator declaration with either name replaces that built-in row" in text
    backend = _field_entry(text, "backend")
    assert "env-var: resolves from AW_SECRET_<NAME> environment variables" in backend
    assert "prompt: prompts interactively at resolution time" in backend
    assert "onepassword: resolves via the 1Password CLI" in backend
    assert "`agw resource sample secret-source`" in text


def test_an_optional_root_union_keeps_its_outer_null_spelling() -> None:
    entry = _field_entry(_text("session-template"), "harness_integration")

    assert "harness_integration (table or null, optional)" in entry


def test_a_field_that_folds_a_scalar_offers_both_spellings() -> None:
    """The shipped defect: an env table accepts ``FOO: a value`` and
    ``FOO: {secret: x}``, the emitted schema said so, and this surface
    documented the table alone, so an operator following the guide's
    "authority on what a spec accepts" rewrote every plaintext value into
    a table for no reason.

    Both halves are asserted, because the fix has to ADD the scalar rather
    than replace the block: the operator who needs the secret form still
    has to be told what is in it.
    """
    text = _text("vm-template")

    assert "  env  (table of string or table, optional)" in text
    assert "    <key>  (string or table, required)" in text
    assert "      - plaintext: An env var whose exported value is written as plaintext." in text
    assert "        value  (string, required)" in text
    assert "      - secret: An env var whose exported value comes from a declared secret." in text
    assert "        secret  (string, required, names a secret)" in text


def test_secret_backend_describes_its_source_config() -> None:
    text = _text("secret-backend/onepassword")

    assert "config:" in text
    assert "name  (one of: onepassword, required)" in text
    assert "account  (string or null, optional, min length 1)" in text
    assert "timeout  (number, optional, default 30.0, gt 0)" in text


def test_tag_only_secret_backend_source_config_is_a_table() -> None:
    text = _text("secret-backend/env-var")

    assert "config:" in text
    assert "name  (one of: env-var, required)" in text


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
    # The union's declared default is a fact of the field, so the
    # parenthetical carries it: what an omitting document resolves to
    # should not take opening the model to learn.
    assert "placement  (table, optional, default {mode: local})" in text


def test_git_token_describe_keeps_the_one_arm_union_visible() -> None:
    """The reference surface agrees with schema and sample: one stored
    arm, its default, its scalar spelling, and the SecretRef field."""
    text = _text("git-credential-provider/github")

    assert "token  (string or table, optional, default {mode: stored})" in text
    assert "- stored: Obtain this credential's token from a stored secret." in text
    assert "mode  (one of: stored, required)" in text
    assert "secret  (string or null, optional, defaults to `git-token-<name>`, names a secret" in text


def test_a_nested_tagged_union_renders_every_arm_with_its_own_fields() -> None:
    """The rendering half of the same first-non-capability-union case
    ``tests/manifests/test_reference.py`` pins structurally.

    Each arm's fields sit under that arm, which is the only reading that
    is true of both: ``mode: local`` takes nothing else and ``mode: ssh``
    requires a ``host``, and a flat list under the field would attribute
    each to the other.
    """
    text = _text("vm-platform/lima")

    assert "    - local: Run limactl on this machine." in text
    assert "    - ssh: Run limactl on another host over SSH." in text
    assert "      mode  (one of: local, required)" in text
    assert "      mode  (one of: ssh, required)" in text
    assert "      host  (string, required, min length 1, e.g. me@gpu-box)" in text


def test_a_nested_union_arm_summary_reaches_the_terminal_as_plain_text() -> None:
    """An arm summary is the arm MODEL's docstring, authored in RST, so it
    has to go through the same markdown normalization every other
    description in this renderer does.

    The alternatives line was the one place that skipped it, which nothing
    noticed while every discriminated union's arms were capabilities
    carrying plain one-line ``description`` strings. Azure's ambient arm is
    the first summary that legitimately wants code spans, so it is the one
    that pins the transform: double backticks in, single backticks out."""
    text = _text("vm-platform/azure-vm")

    assert "- ambient: Authenticate with the ambient chain: `az login`," in text
    assert "``" not in text


def test_a_host_kind_lists_every_arm_it_could_hold() -> None:
    text = _text("vm-site")

    assert "- lima: Lima VMs (local, or on a remote host via SSH)" in text
    assert "- proxmox: Proxmox VE cluster VMs (clone + cloud-init)" in text


def test_every_arm_this_surface_names_is_one_it_also_answers_for(seated: None) -> None:
    """The invariant, over every target the surface documents.

    Naming an arm raises one question, which is what to write if you pick
    it, and there are exactly three honest answers: an address that
    documents it, its fields shown here, or a line saying it is the block
    already open above. Anything else is a word an operator cannot act on,
    and both halves of this have shipped: ``vm-platform/wsl2`` was named
    and unaddressed before the pointer landed, and ``auth: {mode:
    service-principal}`` was named with its three required keys documented
    nowhere any CLI form reached.

    Asserted over the record rather than over the text, and over every
    describable target rather than over a fixture, because the way this
    regresses is a NEW union nobody thought to add a case for.
    """
    for target in describable_targets():
        reference = reference_for(target)
        roots = (*reference.metadata, *reference.spec)
        if reference.root_value is not None:
            roots = (*roots, reference.root_value)
        for path, alternative in _alternatives_in(roots, (target,)):
            answers = (alternative.target, alternative.fields, alternative.recurring)
            assert any(answers), f"{'.'.join(path)}: arm {alternative.name!r} is named and not answered for"


def _alternatives_in(
    entries: tuple[FieldEntry, ...],
    path: tuple[str, ...],
) -> Iterator[tuple[tuple[str, ...], Alternative]]:
    """Every alternative anywhere in ``entries``, with where it was found.

    Arms are walked through too: a union inside a union's arm is exactly
    where a gap hides, and ``vm-site``'s ``platform.placement`` is one.
    """
    for entry in entries:
        here = (*path, entry.name)
        for alternative in entry.alternatives:
            yield here, alternative
            yield from _alternatives_in(alternative.fields, (*here, alternative.name))
        yield from _alternatives_in(entry.children, here)


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
    assert "- local: A disk carved out of the host's own storage." in block
    assert "- network: A disk attached over the network." in block


def test_each_element_arm_shows_that_arms_own_fields(seated: None) -> None:
    """Each arm's fields under that arm, rather than a table of everything
    every arm takes.

    Both arms, because neither is addressable: a disk is not a capability
    and ``agw resource describe-kind vm-platform/network`` is a command
    that fails. ``volume`` was documented nowhere at all while only the
    first arm was expanded, which made a required field of a shape the
    loader accepts invisible to every surface an operator has.
    """
    text = _field_entry(_text("vm-platform/never-enabled"), "disks")

    assert "size_gb (integer, optional, default 40)" in text
    assert "volume (string, required)" in text


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
