"""``agw resource describe-kind``: the field reference an operator reads.

What is worth pinning is what the surface PROMISES: that it answers for a
kind and for one capability implementation, that it needs neither a config
nor a registry (so it answers on a broken host, and about a capability
whose plugin is not enabled), and that every fact in it is derived rather
than authored twice.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, Literal

import pytest

from agentworks.manifests.describe import reference_lines
from agentworks.manifests.reference import describable_targets, reference_for
from agentworks.plugins import Plugin, seated_plugin
from agentworks.schema import AgwModel
from tests.plugins._fixtures import ConformingVMPlatform

if TYPE_CHECKING:
    from collections.abc import Iterator


class DisabledConfig(AgwModel):
    """A fixture platform's config."""

    name: Literal["never-enabled"]
    region: str = "westus2"
    """Where this fixture platform creates its VMs."""


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

    assert "  hint  (string or null, optional, e.g. 'Generate at https://" in text
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
    assert "vm_host  (string or null, optional, min length 1, e.g. 'me@gpu-box')" in text


def test_a_host_kind_lists_the_arms_and_marks_the_one_shown() -> None:
    text = _text("vm-site")

    assert "- lima (shown below): Lima VMs (local, or on a remote host via SSH)" in text
    assert "- proxmox: Proxmox VE cluster VMs (clone + cloud-init)" in text


# --- what the surface does NOT need -----------------------------------


def test_a_capability_no_config_enables_still_documents_itself(seated: None) -> None:
    """The promise that separates this surface from every other one: a
    plugin's capability is registered whether or not config opts into it,
    so an operator can read about a platform BEFORE enabling it. Nothing
    here loads config or builds a registry, and nothing constructs the
    implementation."""
    text = _text("vm-platform/never-enabled")

    assert "a fixture platform no config opts into" in text
    assert "region  (string, optional, default 'westus2')" in text
    assert "Where this fixture platform creates its VMs." in text


def test_a_seated_capability_is_addressable_and_an_unseated_one_is_not(seated: None) -> None:
    assert "vm-platform/never-enabled" in describable_targets()


def test_the_target_list_covers_kinds_and_implementations() -> None:
    targets = describable_targets()

    assert "secret" in targets
    assert "vm-platform" in targets
    assert "vm-platform/lima" in targets
    assert "vm-platform/never-enabled" not in targets, "the fixture is seated only inside its fixture"


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
