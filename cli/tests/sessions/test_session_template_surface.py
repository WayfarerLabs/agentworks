"""The template surface: the internal
``(harness_integration, harness_integration_config)`` pair,
the manifest flat-field rejection, the pair-inheritance rules (FRD R5,
including the multi-parent divergence), and the harness-integration
reference / describe surfaces.

The flat-TOML hoist and its two conflict errors (FRD R6) were pinned here
against the migrator's frozen TOML reader. Both are gone (operator ruling,
2026-08-07): config.toml declares no session templates, so there is no
flat shape left to hoist. The manifest-side equivalents survive below
(``test_yaml_restart_command_is_rejected``, the flat-field rejection).
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent
from typing import Literal

import pytest

from agentworks.bootstrap import build_registry
from agentworks.capabilities.harness_integration import HARNESS_INTEGRATION_REGISTRY, HarnessIntegration
from agentworks.config import load_config
from agentworks.errors import ConfigError
from agentworks.manifests import load_manifests
from agentworks.resources.graph import FinalizeContext
from agentworks.resources.inspect import describe_resource
from agentworks.schema import AgwModel, CapabilityBlock
from agentworks.sessions.template import SessionTemplate
from agentworks.sessions.templates import resolve_from_dict

# -- a second registered harness integration, for the cross-integration R5 case --


class _FakeConfig(AgwModel):
    """The fake integration's config: its tag plus one field the
    inheritance tests can put a recognizable value in."""

    name: Literal["fake"]
    marker: str | None = None


class _FakeHarnessIntegration(HarnessIntegration):
    """A minimal second harness integration so the 'different integration' inheritance
    case (which needs two registered names) can be exercised without
    ``claude-code`` (unregistered until Phase 2)."""

    name = "fake"
    description = "test double harness"
    contract_version = 1
    config_model = _FakeConfig

    def start(self, ctx):  # type: ignore[no-untyped-def]
        return ""

    def resume(self, ctx):  # type: ignore[no-untyped-def]
        return ""

    def _probe_target(self, transport):  # type: ignore[no-untyped-def]
        return None


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


def test_unknown_shell_field_errors_at_build(tmp_path: Path) -> None:
    """The effective blob is shape-validated by the finalize ``validate``
    pass (R3), so a malformed shell block fails at build_registry, not at
    load (this template inherits nothing, so effective and declared are the
    same blob). The error keeps the harness-integration vocabulary and gains
    the source location (re-attached from the resource origin, the manifest
    file now)."""
    root = _manifest(
        tmp_path,
        """
        apiVersion: agentworks/v1
        kind: session-template
        metadata:
          name: bad
        spec:
          harness_integration:
            name: shell
            nope: x
        """,
    )
    config = _config(tmp_path, "")
    with pytest.raises(ConfigError, match="nope: unknown field; expected one of:") as exc:
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


def test_child_silent_inherits_the_pair_unchanged() -> None:
    templates = {
        "base": SessionTemplate(name="base", harness_integration=CapabilityBlock.of("shell", **{"command": "claude"})),
        "child": SessionTemplate(name="child", inherits=["base"]),
    }
    resolved = resolve_from_dict(templates, "child")
    assert resolved.harness_integration == "shell"
    assert resolved.harness_integration_config == {"command": "claude"}


def test_yaml_restart_command_is_rejected(tmp_path: Path) -> None:
    root = _manifest(
        tmp_path,
        """
        apiVersion: agentworks/v1
        kind: session-template
        metadata:
          name: old-shell
        spec:
          harness_integration:
            name: shell
            restart_command: old-resume
        """,
    )
    with pytest.raises(ConfigError, match="restart_command: unknown field"):
        build_registry(_config(tmp_path, ""), load_manifests(root))


def test_child_different_harness_integration_starts_fresh(fake_harness_integration: None) -> None:
    """A child naming a DIFFERENT harness integration starts from an empty blob; the
    parent's blob was addressed to the wrong capability and never leaks."""
    templates = {
        "base": SessionTemplate(name="base", harness_integration=CapabilityBlock.of("shell", **{"command": "sh-cmd"})),
        "child": SessionTemplate(
            name="child",
            inherits=["base"],
            harness_integration=CapabilityBlock.of("fake", **{"marker": "child"}),
        ),
    }
    resolved = resolve_from_dict(templates, "child")
    assert resolved.harness_integration == "fake"
    assert resolved.harness_integration_config == {"marker": "child"}  # no leak of the shell blob


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
    resolved = resolve_from_dict(templates, "child")
    assert resolved.harness_integration == "shell"
    assert resolved.harness_integration_config == {"resume_command": "from-second"}


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


def test_undeclared_default_resolves_to_shell_empty() -> None:
    resolved = resolve_from_dict({}, None)
    assert resolved.name == "default"
    assert resolved.harness_integration == "shell"
    assert resolved.harness_integration_config == {}


# -- describe / reference surfaces (FRD R2, R8) ------------------------------


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
    desc = describe_resource(registry, "harness-integration", "shell")
    sources = {entry.source for entry in desc.references}
    assert ("session-template", "htop") in sources
