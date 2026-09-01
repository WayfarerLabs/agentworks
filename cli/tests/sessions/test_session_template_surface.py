"""The template surface: the internal
``(harness_integration, harness_integration_config)`` pair,
the manifest flat-field rejection, the pair-inheritance rules (FRD R5,
including the multi-parent divergence), and the harness-integration
reference / graph surfaces.

The flat-TOML hoist and its two conflict errors (FRD R6) were pinned here
against the migrator's frozen TOML reader. Both are gone (operator ruling,
2026-08-07): config.toml declares no session templates, so there is no
flat shape left to hoist. The manifest-side equivalents survive below
(``test_yaml_restart_command_is_rejected``, the flat-field rejection).
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent
from typing import Annotated, ClassVar, Literal

import pytest
from pydantic import Field

from agentworks.bootstrap import build_registry
from agentworks.capabilities.harness_integration import HARNESS_INTEGRATION_REGISTRY, HarnessIntegration, HarnessStart
from agentworks.config import load_config
from agentworks.env.entry import EnvEntry
from agentworks.errors import ConfigError
from agentworks.manifests import load_manifests
from agentworks.resources import GraphDirection, show_graph
from agentworks.resources.access import ResourceIdentity
from agentworks.resources.graph import FinalizeContext
from agentworks.resources.reference import RefRelationship
from agentworks.schema import AgwModel, CapabilityBlock, MergeStrategy
from agentworks.sessions.template import SessionTemplate
from agentworks.sessions.templates import resolve_from_dict, resolve_from_dict_with_provenance

# -- a second registered harness integration, for the cross-integration R5 case --


class _FakeConfig(AgwModel):
    """The fake integration's config: its tag plus one field the
    inheritance tests can put a recognizable value in."""

    name: Literal["fake"]
    marker: str | None = None
    nested: dict[str, str] = Field(default_factory=dict)
    items: Annotated[list[str], MergeStrategy.REPLACE] = Field(default_factory=list)


class _FakeHarnessIntegration(HarnessIntegration):
    """A minimal second harness integration so the 'different integration' inheritance
    case (which needs two registered names) can be exercised without
    ``claude-code`` (unregistered until Phase 2)."""

    name = "fake"
    description = "test double harness"
    contract_version = 1
    config_model = _FakeConfig

    def start(self, ctx, *, force_new=False):  # type: ignore[no-untyped-def]
        return HarnessStart("")

    def _probe_target(self, transport):  # type: ignore[no-untyped-def]
        return None


class _ReplacingSessionTemplate(SessionTemplate):
    merge_strategy: ClassVar[MergeStrategy] = MergeStrategy.REPLACE


@pytest.fixture()
def fake_harness_integration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(HARNESS_INTEGRATION_REGISTRY, "fake", _FakeHarnessIntegration)


def _config(tmp_path: Path, body: str):  # type: ignore[no-untyped-def]
    pub = tmp_path / "id.pub"
    priv = tmp_path / "id"
    pub.write_text("ssh-ed25519 AAAA...")
    priv.write_text("-----BEGIN OPENSSH PRIVATE KEY-----")
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        dedent(f"""\
        [operator]
        ssh_public_key = "{pub.as_posix()}"
        ssh_private_key = "{priv.as_posix()}"

        """)
        + dedent(body)
    )
    return load_config(cfg, warn_issues=False)


@pytest.mark.parametrize(
    "field",
    [
        pytest.param("nope", id="a-plain-typo"),
        # The retired TOML spelling of a real setting, and the case that
        # keeps it retired: re-adding ``restart_command`` to ``ShellConfig``
        # fails this row alone. It earns no migration steer of its own
        # either, which is why the expectation is the same unknown-key line
        # the typo gets.
        pytest.param("restart_command", id="a-retired-key"),
    ],
)
def test_an_unknown_key_in_the_harness_block_errors_at_build(tmp_path: Path, field: str) -> None:
    """The effective blob is shape-validated by the finalize ``validate``
    pass (R3), so a malformed shell block fails at build_registry, not at
    load (this template inherits nothing, so effective and declared are the
    same blob). The error keeps the harness-integration vocabulary and gains
    the source location (re-attached from the resource origin, the manifest
    file now)."""
    root = _manifest(
        tmp_path,
        f"""
        apiVersion: agentworks/v1
        kind: session-template
        metadata:
          name: bad
        spec:
          harness_integration:
            name: shell
            {field}: x
        """,
    )
    config = _config(tmp_path, "")
    with pytest.raises(ConfigError, match=f"{field}: unknown field; expected one of:") as exc:
        build_registry(config, load_manifests(root))
    assert "res.yaml" in str(exc.value)


# -- manifest flat-field rejection + unknown-name miss policy (FRD R2) -------


def _manifest(tmp_path: Path, text: str) -> Path:
    root = tmp_path / "resources"
    root.mkdir(parents=True, exist_ok=True)
    (root / "res.yaml").write_text(dedent(text))
    return root


def test_manifest_flat_field_is_rejected(tmp_path: Path) -> None:
    root = _manifest(
        tmp_path,
        """
        apiVersion: agentworks/v1
        kind: session-template
        metadata:
          name: claude
        spec:
          command: claude
        """,
    )
    with pytest.raises(ConfigError, match=r"command: unknown field; expected one of: "):
        load_manifests(root)


def test_manifest_unknown_harness_name_errors_at_finalize(tmp_path: Path) -> None:
    """A typo'd (or not-yet-registered, e.g. claude-code) harness integration name is
    a valid reference shape at load; the kind's error miss policy reports
    it at finalize, naming the template."""
    cfg = tmp_path / "config.toml"
    pub = tmp_path / "id.pub"
    priv = tmp_path / "id"
    pub.write_text("ssh-ed25519 AAAA...")
    priv.write_text("-----BEGIN OPENSSH PRIVATE KEY-----")
    cfg.write_text(f'[operator]\nssh_public_key = "{pub.as_posix()}"\nssh_private_key = "{priv.as_posix()}"\n')
    _manifest(
        tmp_path,
        """
        apiVersion: agentworks/v1
        kind: session-template
        metadata:
          name: typo
        spec:
              harness_integration:
                name: shel
        """,
    )
    config = load_config(cfg, warn_issues=False)
    with pytest.raises(ConfigError, match="'typo' references unknown harness-integration 'shel'"):
        build_registry(config)


# -- pair inheritance (FRD R5) -----------------------------------------------


def test_child_same_harness_integration_merges_child_wins_and_unions_required() -> None:
    templates = {
        "base": SessionTemplate(
            name="base",
            harness_integration=CapabilityBlock.of("shell", **{"command": "claude", "required_commands": ["claude"]}),
        ),
        "child": SessionTemplate(
            name="child",
            inherits=["base"],
            harness_integration=CapabilityBlock.model_validate(
                {
                    "name": "shell",
                    **{
                        "command": "claude --resume",
                        "required_commands": ["rg"],
                    },
                }
            ),
        ),
    }
    resolved = resolve_from_dict(templates, "child")
    assert resolved.harness_integration == "shell"
    assert resolved.harness_integration_config["command"] == "claude --resume"  # child wins
    assert resolved.harness_integration_config["required_commands"] == ["claude", "rg"]  # union


def test_same_registered_integration_recurses_through_its_model(
    fake_harness_integration: None,
) -> None:
    templates = {
        "base": SessionTemplate(
            name="base",
            harness_integration=CapabilityBlock.of(
                "fake",
                **{"nested": {"left": "base", "shared": "base"}},
            ),
        ),
        "child": SessionTemplate(
            name="child",
            inherits=["base"],
            harness_integration=CapabilityBlock.of(
                "fake",
                **{"nested": {"right": "child", "shared": "child"}},
            ),
        ),
    }

    resolved = resolve_from_dict(templates, "child")
    assert resolved.harness_integration_config["nested"] == {
        "left": "base",
        "right": "child",
        "shared": "child",
    }


def test_same_unknown_integration_replaces_the_complete_raw_config() -> None:
    templates = {
        "base": SessionTemplate(
            name="base",
            harness_integration=CapabilityBlock.of(
                "not-installed",
                **{"left": "base", "nested": {"old": True}},
            ),
        ),
        "child": SessionTemplate(
            name="child",
            inherits=["base"],
            harness_integration=CapabilityBlock.of(
                "not-installed",
                **{"right": "child"},
            ),
        ),
    }

    resolved = resolve_from_dict(templates, "child")
    assert resolved.harness_integration == "not-installed"
    assert resolved.harness_integration_config == {"right": "child"}


def test_harness_list_provenance_tracks_union_and_child_wins(
    fake_harness_integration: None,
) -> None:
    templates = {
        "shell-base": SessionTemplate(
            name="shell-base",
            harness_integration=CapabilityBlock.of("shell", **{"required_commands": ["git"]}),
        ),
        "shell-child": SessionTemplate(
            name="shell-child",
            inherits=["shell-base"],
            harness_integration=CapabilityBlock.of("shell", **{"required_commands": ["git", "rg"]}),
        ),
        "fake-base": SessionTemplate(
            name="fake-base",
            harness_integration=CapabilityBlock.of("fake", **{"items": ["parent"]}),
        ),
        "fake-child": SessionTemplate(
            name="fake-child",
            inherits=["fake-base"],
            harness_integration=CapabilityBlock.of("fake", **{"items": ["child"]}),
        ),
        "same-base": SessionTemplate(
            name="same-base",
            harness_integration=CapabilityBlock.of("fake", **{"items": ["same"]}),
        ),
        "same-child": SessionTemplate(
            name="same-child",
            inherits=["same-base"],
            harness_integration=CapabilityBlock.of("fake", **{"items": ["same"]}),
        ),
        "shape-base": SessionTemplate(
            name="shape-base",
            harness_integration=CapabilityBlock.of("fake", **{"items": "scalar"}),
        ),
        "shape-child": SessionTemplate(
            name="shape-child",
            inherits=["shape-base"],
            harness_integration=CapabilityBlock.of("fake", **{"items": ["child"]}),
        ),
    }

    union = resolve_from_dict_with_provenance(templates, "shell-child")
    replaced = resolve_from_dict_with_provenance(templates, "fake-child")
    same = resolve_from_dict_with_provenance(templates, "same-child")
    shape = resolve_from_dict_with_provenance(templates, "shape-child")

    assert [source.name for source in union.provenance[("harness_integration_config", "required_commands", 0)]] == [
        "shell-base",
        "shell-child",
    ]
    assert union.provenance[("harness_integration_config", "required_commands", 1)][0].name == "shell-child"
    assert ("harness_integration_config", "items", 0) not in replaced.provenance
    assert replaced.provenance[("harness_integration_config", "items")][0].name == "fake-child"
    assert same.provenance[("harness_integration_config", "items")][0].name == "same-child"
    assert shape.provenance[("harness_integration_config", "items")][0].name == "shape-child"


def test_child_silent_inherits_the_pair_unchanged() -> None:
    templates = {
        "base": SessionTemplate(name="base", harness_integration=CapabilityBlock.of("shell", **{"command": "claude"})),
        "child": SessionTemplate(name="child", inherits=["base"]),
    }
    resolved = resolve_from_dict(templates, "child")
    assert resolved.harness_integration == "shell"
    assert resolved.harness_integration_config == {"command": "claude"}


def test_root_replacement_resets_all_omitted_session_fields() -> None:
    templates = {
        "base": SessionTemplate(
            name="base",
            description="Parent session",
            env={"MODE": EnvEntry.model_validate("parent")},
            harness_integration=CapabilityBlock.of("shell", **{"command": "parent-command"}),
        ),
        "child": _ReplacingSessionTemplate(name="child", inherits=["base"]),
    }

    resolution = resolve_from_dict_with_provenance(templates, "child")

    assert resolution.value.description == "Login shell"
    assert resolution.value.env == {}
    assert resolution.value.harness_integration == "shell"
    assert resolution.value.harness_integration_config == {}
    assert all(source.name != "base" for sources in resolution.provenance.values() for source in sources)


# A child naming a DIFFERENT harness integration starting from an empty
# blob is the first move of the test below, which runs the switch across a
# chain that also switches BACK and so pins the flat fold order on top of
# the discard. The two-template version of it made the same assertion off
# the same line and caught nothing the harder one does not.


def test_a_switch_inside_one_parents_chain_discards_an_earlier_parents_blob(
    fake_harness_integration: None,
) -> None:
    """The rule above, applied across a chain that switches away and back:
    ``fake`` discards the ``shell`` blob accumulated so far, and naming
    ``shell`` again afterwards starts fresh rather than recovering it.

    Worth pinning because the merge could plausibly go the other way. The
    resolver folds the chain's declarations FLAT, in one merge order, so
    the switch sits between the two ``shell`` layers and separates them.
    Merging each parent's already-merged pair instead would hide the
    switch inside ``back-to-shell``'s own result and hand the fold a
    plain ``shell`` pair, resurrecting ``from-first`` across a capability
    that had already discarded it.
    """
    templates = {
        "shell-parent": SessionTemplate(
            name="shell-parent",
            harness_integration=CapabilityBlock.of("shell", **{"command": "from-first"}),
        ),
        "detour": SessionTemplate(
            name="detour",
            harness_integration=CapabilityBlock.of("fake", **{"marker": "detour"}),
        ),
        "back-to-shell": SessionTemplate(
            name="back-to-shell",
            inherits=["detour"],
            harness_integration=CapabilityBlock.of("shell", **{"resume_command": "from-second"}),
        ),
        "child": SessionTemplate(name="child", inherits=["shell-parent", "back-to-shell"]),
    }
    layered = resolve_from_dict_with_provenance(templates, "child")
    resolved = layered.value
    assert resolved.harness_integration == "shell"
    assert resolved.harness_integration_config == {"resume_command": "from-second"}
    assert all(
        source.name != "shell-parent"
        for path, sources in layered.provenance.items()
        if path[:1] == ("harness_integration_config",)
        for source in sources
    )


def test_multi_parent_silent_parent_does_not_wipe(tmp_path: Path) -> None:
    """The pinned divergence from today's multi-parent semantics (FRD
    R5): a later harness-integration-silent parent no longer wipes an earlier
    parent's command. Under the old flat-scalar merge, ``env-only``
    would have reset the command to empty."""
    root = _manifest(
        tmp_path,
        """
        apiVersion: agentworks/v1
        kind: session-template
        metadata:
          name: has-command
        spec:
          harness_integration:
            name: shell
            command: run-me
        ---
        apiVersion: agentworks/v1
        kind: session-template
        metadata:
          name: env-only
        spec:
          env:
            FOO: bar
        ---
        apiVersion: agentworks/v1
        kind: session-template
        metadata:
          name: child
        spec:
          inherits: [has-command, env-only]
        """,
    )
    config = _config(tmp_path, "")
    from agentworks.sessions.templates import resolve_template

    resolved = resolve_template(build_registry(config, load_manifests(root)), "child")
    assert resolved.harness_integration == "shell"
    assert resolved.harness_integration_config == {"command": "run-me"}
    assert resolved.env["FOO"].value == "bar"


def test_session_env_values_merge_as_complete_env_entry_models() -> None:
    templates = {
        "base": SessionTemplate(
            name="base",
            env={
                "TOKEN": EnvEntry({"secret": "base-token"}),
                "BASE_ONLY": EnvEntry({"value": "preserved"}),
            },
        ),
        "child": SessionTemplate(
            name="child",
            inherits=["base"],
            env={"TOKEN": EnvEntry({"value": "child-token"})},
        ),
    }

    resolved = resolve_from_dict(templates, "child")

    assert resolved.env == {
        "TOKEN": EnvEntry({"value": "child-token"}),
        "BASE_ONLY": EnvEntry({"value": "preserved"}),
    }


def test_undeclared_default_resolves_to_shell_empty() -> None:
    resolved = resolve_from_dict({}, None)
    assert resolved.name == "default"
    assert resolved.harness_integration == "shell"
    assert resolved.harness_integration_config == {}


# -- Graph / reference surfaces (FRD R2, R8) ---------------------------------


def test_declared_harness_integration_emits_a_reference() -> None:
    tmpl = SessionTemplate(name="claude", harness_integration=CapabilityBlock.of("shell", **{"command": "claude"}))
    refs = tmpl.dependencies(FinalizeContext())
    harness_refs = [r for r in refs if r.kind == "harness-integration"]
    assert len(harness_refs) == 1
    assert harness_refs[0].name == "shell"
    assert harness_refs[0].usage == "the session harness integration"


def test_undeclared_harness_integration_emits_no_reference() -> None:
    tmpl = SessionTemplate(name="plain")
    assert [r for r in tmpl.dependencies(FinalizeContext()) if r.kind == "harness-integration"] == []


def test_harness_integration_row_lists_its_declaring_template(tmp_path: Path) -> None:
    root = _manifest(
        tmp_path,
        """
        apiVersion: agentworks/v1
        kind: session-template
        metadata:
          name: htop
        spec:
          harness_integration:
            name: shell
            command: htop
        """,
    )
    config = _config(tmp_path, "")
    registry = build_registry(config, load_manifests(root))
    result = show_graph(
        registry,
        ResourceIdentity("harness-integration", "shell"),
        GraphDirection.DEPENDENTS,
        1,
    )
    assert any(
        edge.source.kind == "session-template"
        and edge.source.name == "htop"
        and edge.target.kind == "harness-integration"
        and edge.target.name == "shell"
        and edge.relationship is RefRelationship.USES
        for edge in result.edges
    )
