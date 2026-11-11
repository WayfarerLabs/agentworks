"""Tests for eager-prompting orchestration (Phase 6).

Pins:
- compute_needed_secrets unions across targets and dedupes by name,
  preserving first-encounter order both within and across targets
- secret-reference union is invariant under value substitution
- resolve_for_command returns the {secret: value} map AND populates
  the resolver cache so subsequent renders use cached values
- resolve_for_command on an empty union does not call the resolver
- SecretTarget.label is excluded from equality; hashing is not supported
- Admin and agent scopes are mutually exclusive in a single target
"""

from __future__ import annotations

from textwrap import dedent
from typing import TYPE_CHECKING

import pytest

from agentworks.bootstrap import build_registry
from agentworks.config import load_config
from agentworks.env import EnvEntry
from agentworks.errors import StateError, ValidationError
from agentworks.secrets import (
    SecretTarget,
    compute_needed_secrets,
    resolve_for_command,
)
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.secrets.resolve import ResolutionBatch
from tests.conftest import ManifestDoc, write_manifests

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

# ---------------------------------------------------------------------------
# Config fixture
# ---------------------------------------------------------------------------


def _write_config(
    tmp_path: Path,
    *,
    settings: str = "",
    manifests: Sequence[ManifestDoc | str] = (),
) -> Path:
    """Write a settings-only config.toml plus its resources/ manifests and
    return the config path.

    config.toml is settings only now (ADR 0022): the ``[secrets.*]`` these
    tests used to declare are ``secret`` manifests under ``resources/``, and
    ``[secret_config]`` stays a settings-side table carried in ``settings``.
    """
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
        + dedent(settings)
    )
    if manifests:
        write_manifests(tmp_path, *manifests)
    return cfg


# ---------------------------------------------------------------------------
# compute_needed_secrets
# ---------------------------------------------------------------------------


def test_returns_empty_for_no_targets(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    config = load_config(cfg, warn_issues=False)
    assert compute_needed_secrets([], build_registry(config)) == []


def test_unions_single_target_env_chain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AW_SECRET_API_KEY", "x")  # silence prompt fallback
    cfg = _write_config(
        tmp_path,
        settings="""
        [secret_config]
        sources = ["env-var"]
        """,
        manifests=[ManifestDoc("secret", "api-key", description="shared API key")],
    )
    config = load_config(cfg, warn_issues=False)
    vm_env = {"API_KEY": EnvEntry({"secret": "api-key"})}

    decls = compute_needed_secrets([SecretTarget(vm=vm_env)], build_registry(config))
    assert [d.name for d in decls] == ["api-key"]


def test_unknown_reference_raises_instead_of_dropping(tmp_path: Path) -> None:
    """A referenced name with no registry declaration is a
    registry-construction bug (referenced secrets auto-declare at
    finalize, so a legitimate reference always has a row); silently
    dropping it used to surface as a mysterious downstream "secret
    didn't resolve" far from the cause. StateError, not ConfigError:
    it is never an operator's config mistake."""
    from agentworks.errors import StateError

    cfg = _write_config(tmp_path)
    config = load_config(cfg, warn_issues=False)
    vm_env = {"API_KEY": EnvEntry({"secret": "ghost-secret"})}

    with pytest.raises(StateError, match="ghost-secret"):
        compute_needed_secrets(
            [SecretTarget(vm=vm_env, label="test-target")],
            build_registry(config),
        )


def test_runtime_target_can_transiently_auto_declare_overlay_only_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AW_SECRET_OVERLAY_ONLY", "resolved")
    cfg = _write_config(tmp_path, settings='[secret_config]\nsources = ["env-var"]')
    config = load_config(cfg, warn_issues=False)
    registry = build_registry(config)
    target = SecretTarget(vm={"TOKEN": EnvEntry({"secret": "overlay-only"})})

    values = resolve_for_command(
        [target],
        config,
        registry,
        allow_transient_auto_declare=True,
        interaction=TtyInteractionPolicy.REFUSE,
    )

    assert values == {"overlay-only": "resolved"}
    with pytest.raises(KeyError):
        registry.lookup("secret", "overlay-only")


def test_unions_across_multiple_targets_dedup_by_name(tmp_path: Path) -> None:
    cfg = _write_config(
        tmp_path,
        settings="""
        [secret_config]
        sources = ["env-var", "prompt"]
        """,
        manifests=[
            ManifestDoc("secret", "shared", description="shared"),
            ManifestDoc("secret", "unique-a", description="unique to target a"),
        ],
    )
    config = load_config(cfg, warn_issues=False)

    t_a = SecretTarget(
        vm={
            "KEY1": EnvEntry({"secret": "shared"}),
            "KEY2": EnvEntry({"secret": "unique-a"}),
        }
    )
    t_b = SecretTarget(
        vm={
            "KEY3": EnvEntry({"secret": "shared"}),
        }
    )

    decls = compute_needed_secrets([t_a, t_b], build_registry(config))
    names = [d.name for d in decls]
    # 'shared' should appear once. 'unique-a' from t_a is included.
    assert sorted(names) == ["shared", "unique-a"]
    # First-encounter ordering: 'shared' came before 'unique-a' in t_a's env
    # (KEY1 < KEY2 lexicographically; effective_env preserves dict order from
    # input which is insertion order in Python 3.7+).
    assert names[0] == "shared"


def test_walks_all_scopes_in_target(tmp_path: Path) -> None:
    """All five scope dicts in a SecretTarget feed into the union."""
    cfg = _write_config(
        tmp_path,
        settings="""
        [secret_config]
        sources = ["env-var", "prompt"]
        """,
        manifests=[
            ManifestDoc("secret", "vm-secret", description="vm"),
            ManifestDoc("secret", "ws-secret", description="ws"),
            ManifestDoc("secret", "admin-secret", description="admin"),
            ManifestDoc("secret", "session-secret", description="session"),
        ],
    )
    config = load_config(cfg, warn_issues=False)

    target = SecretTarget(
        vm={"V": EnvEntry({"secret": "vm-secret"})},
        workspace={"W": EnvEntry({"secret": "ws-secret"})},
        admin={"A": EnvEntry({"secret": "admin-secret"})},
        session={"S": EnvEntry({"secret": "session-secret"})},
    )
    decls = compute_needed_secrets([target], build_registry(config))
    assert sorted(d.name for d in decls) == [
        "admin-secret",
        "session-secret",
        "vm-secret",
        "ws-secret",
    ]


def test_secret_references_invariant_under_value_substitution(
    tmp_path: Path,
) -> None:
    """Callers may hand pre- or post-substitution env dicts to
    SecretTarget. The computed SecretDecl union is invariant because
    Plaintext interpolation only reads EnvEntry.value
    (plaintext), never EnvEntry.secret (the reference name). This is
    load-bearing for Phase 6.2 wiring, which builds targets from
    un-substituted template env dicts."""
    cfg = _write_config(
        tmp_path,
        settings="""
        [secret_config]
        sources = ["env-var", "prompt"]
        """,
        manifests=[ManifestDoc("secret", "api", description="api")],
    )
    config = load_config(cfg, warn_issues=False)

    pre_subst = {
        "API": EnvEntry({"secret": "api"}),
        "GREETING": EnvEntry({"value": "hello ${agw:session_name}"}),
    }
    post_subst = {
        "API": EnvEntry({"secret": "api"}),
        "GREETING": EnvEntry({"value": "hello mysession"}),
    }
    registry = build_registry(config)
    pre = compute_needed_secrets([SecretTarget(vm=pre_subst)], registry)
    post = compute_needed_secrets([SecretTarget(vm=post_subst)], registry)
    assert [d.name for d in pre] == [d.name for d in post] == ["api"]


def test_admin_and_agent_in_same_target_raises(tmp_path: Path) -> None:
    """Admin and agent scopes are mutually exclusive at the merge
    layer. Building a SecretTarget with both set is a programmer
    error; the orchestrator should surface it eagerly from
    compute_needed_secrets rather than letting it slip through to the
    later compose_env call."""
    cfg = _write_config(tmp_path)
    config = load_config(cfg, warn_issues=False)

    target = SecretTarget(
        vm={},
        admin={"A": EnvEntry({"value": "x"})},
        agent={"B": EnvEntry({"value": "y"})},
    )
    with pytest.raises(ValueError, match="admin.*agent|agent.*admin"):
        compute_needed_secrets([target], build_registry(config))


def test_cross_target_first_encounter_ordering(tmp_path: Path) -> None:
    """First-encounter ordering holds across targets, not just within
    a target. For non-interactive errors the operator wants the
    missing-secrets list in prompt-order."""
    cfg = _write_config(
        tmp_path,
        settings="""
        [secret_config]
        sources = ["env-var", "prompt"]
        """,
        manifests=[
            ManifestDoc("secret", "b-secret", description="b"),
            ManifestDoc("secret", "a-secret", description="a"),
        ],
    )
    config = load_config(cfg, warn_issues=False)

    target_b = SecretTarget(vm={"B": EnvEntry({"secret": "b-secret"})})
    target_a = SecretTarget(vm={"A": EnvEntry({"secret": "a-secret"})})

    decls = compute_needed_secrets([target_b, target_a], build_registry(config))
    # b-secret encountered first (target_b is first in the list), so
    # it leads the order even though a-secret sorts alphabetically before.
    assert [d.name for d in decls] == ["b-secret", "a-secret"]


def test_resolve_for_command_returns_resolved_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The returned values dict IS the channel: the command threads it
    down to its compose_env sites (there is no cache)."""
    monkeypatch.setenv("AW_SECRET_API_KEY", "from-env")
    cfg = _write_config(
        tmp_path,
        settings="""
        [secret_config]
        sources = ["env-var"]
        """,
        manifests=[ManifestDoc("secret", "api-key", description="api")],
    )
    config = load_config(cfg, warn_issues=False)
    target = SecretTarget(vm={"K": EnvEntry({"secret": "api-key"})})
    resolved = resolve_for_command([target], config, build_registry(config), interaction=TtyInteractionPolicy.REFUSE)
    assert resolved == {"api-key": "from-env"}


def test_resolve_for_command_rejects_non_enum_policy_before_any_source_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller-supplied ``"refuse"`` is equal to the enum but not identical to it.

    Every consumer branches by identity, so without the boundary check this call would
    take the not-refuse path and permit the TTY prompt it meant to refuse. This
    is the one name of the resolve family that ``agentworks.secrets`` exports, so it is
    the one most reachable from a caller our type checker never sees.
    """
    monkeypatch.setenv("AW_SECRET_API_KEY", "from-env")
    cfg = _write_config(
        tmp_path,
        settings="""
        [secret_config]
        sources = ["env-var"]
        """,
        manifests=[ManifestDoc("secret", "api-key", description="api")],
    )
    config = load_config(cfg, warn_issues=False)
    registry = build_registry(config)

    def _boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("a rejected policy must not reach the sources")

    monkeypatch.setattr("agentworks.secrets.resolve.active_sources", _boom)
    monkeypatch.setattr("agentworks.secrets.resolve.resolve_batch", _boom)

    target = SecretTarget(vm={"K": EnvEntry({"secret": "api-key"})})
    with pytest.raises(StateError):
        resolve_for_command([target], config, registry, interaction="refuse")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("standalone-sentinel\nsecond\n", id="lf-with-terminal-newline"),
        pytest.param("standalone-sentinel\r\nsecond\r\n", id="crlf-with-terminal-newline"),
    ],
)
def test_resolve_for_command_rejects_multiline_environment_target_after_delivery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv("AW_SECRET_API_KEY", value)
    cfg = _write_config(
        tmp_path,
        settings="""
        [secret_config]
        sources = ["env-var"]
        """,
        manifests=[ManifestDoc("secret", "api-key", description="api")],
    )
    config = load_config(cfg, warn_issues=False)
    target = SecretTarget(vm={"API_KEY": EnvEntry({"secret": "api-key"})})

    with pytest.raises(ValidationError) as caught:
        resolve_for_command(
            [target],
            config,
            build_registry(config),
            interaction=TtyInteractionPolicy.REFUSE,
        )

    assert "cannot be used for environment injection" in str(caught.value)
    assert "standalone-sentinel" not in repr((caught.value.args, vars(caught.value)))
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_resolved_values_are_plain_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one resolve call captures values at that moment; later
    environment mutation cannot change what the command threads down.
    (The prompt-once guarantee is structural: one resolve per command.)"""
    monkeypatch.setenv("AW_SECRET_API_KEY", "first")
    cfg = _write_config(
        tmp_path,
        settings="""
        [secret_config]
        sources = ["env-var"]
        """,
        manifests=[ManifestDoc("secret", "api-key", description="api")],
    )
    config = load_config(cfg, warn_issues=False)
    registry = build_registry(config)
    target = SecretTarget(vm={"K": EnvEntry({"secret": "api-key"})})
    values = resolve_for_command([target], config, registry, interaction=TtyInteractionPolicy.REFUSE)
    assert values == {"api-key": "first"}

    monkeypatch.setenv("AW_SECRET_API_KEY", "second")
    from agentworks.env import compose_env
    from agentworks.env.identity import ResourceContext

    env = compose_env(
        values=values,
        ctx=ResourceContext(vm_name="v", platform="lima", site="lima", user="u"),
        vm={"K": EnvEntry({"secret": "api-key"})},
    )
    assert env["K"] == "first"


def test_resolve_for_command_skips_loop_when_no_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty target list (or targets whose envs reference no secrets)
    must not run the resolve loop -- avoids spinning up prompt machinery
    for commands that need nothing."""
    cfg = _write_config(tmp_path)
    config = load_config(cfg, warn_issues=False)
    registry = build_registry(config)

    called = {"count": 0}

    def _spy(*args: object, **kwargs: object) -> ResolutionBatch:
        called["count"] += 1
        raise AssertionError("empty resolution must not call the core")

    monkeypatch.setattr("agentworks.secrets.resolve.resolve_batch", _spy)

    resolve_for_command([], config, registry, interaction=TtyInteractionPolicy.REFUSE)
    assert called["count"] == 0

    plaintext_target = SecretTarget(vm={"K": EnvEntry({"value": "plain"})})
    resolve_for_command([plaintext_target], config, registry, interaction=TtyInteractionPolicy.REFUSE)
    assert called["count"] == 0


def test_label_excluded_from_equality() -> None:
    """label is diagnostic-only; targets with the same envs but
    different labels are equal. Hashing is not supported (env fields
    are mutable dicts), so set-based dedup is not part of the contract."""
    env = {"K": EnvEntry({"value": "v"})}
    a = SecretTarget(vm=env, label="provisioning")
    b = SecretTarget(vm=env, label="session-create")
    assert a == b


def test_secret_target_is_not_hashable() -> None:
    """The dataclass is frozen but its env fields are mutable dicts.
    Hash attempts must fail loudly rather than half-work. Pinned so a
    future hashing change is a deliberate decision, not silent drift."""
    target = SecretTarget(vm={"K": EnvEntry({"value": "v"})})
    with pytest.raises(TypeError):
        hash(target)


def test_label_round_trips() -> None:
    target = SecretTarget(
        vm={"K": EnvEntry({"value": "v"})},
        label="agent-bootstrap",
    )
    assert target.label == "agent-bootstrap"


@pytest.mark.parametrize("entry", [EnvEntry("${agw:agent_name}"), EnvEntry({"secret": "ignored"})])
def test_identity_collisions_do_not_validate_collect_or_resolve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: EnvEntry
) -> None:
    from agentworks.interpolation import context_values
    from agentworks.secrets.orchestration import validate_target_values

    config = load_config(_write_config(tmp_path), warn_issues=False)
    registry = build_registry(config)
    target = SecretTarget(
        vm={
            "AGENTWORKS_WORKSPACE": entry,
            "AGENTWORKS_WORKSPACE_DIR": entry,
            "AGENTWORKS_SESSION": entry,
            "AGENTWORKS_SESSION_KIND": entry,
        },
        workspace={},
        session={},
    )
    target.validate_context(context_values(vm_name="vm", user="admin", workspace_name="ws", session_name="s"))
    assert compute_needed_secrets([target], registry) == []
    validate_target_values([target], {})

    def unexpected_resolve(*args: object, **kwargs: object) -> None:
        pytest.fail("An ignored secret must not reach resolution")

    monkeypatch.setattr("agentworks.secrets.resolve.resolve_batch", unexpected_resolve)
    assert resolve_for_command([target], config, registry, interaction=TtyInteractionPolicy.REFUSE) == {}


@pytest.mark.parametrize(
    "key", ["AGENTWORKS_WORKSPACE", "AGENTWORKS_SESSION_KIND", "AGENTWORKS_VM", "AGENTWORKS_CUSTOM"]
)
def test_absent_identity_context_does_not_discard_authored_entries(tmp_path: Path, key: str) -> None:
    from agentworks.interpolation import context_values

    config = load_config(_write_config(tmp_path), warn_issues=False)
    target = SecretTarget(vm={key: EnvEntry("${agw:agent_name}")})
    with pytest.raises(ValidationError):
        target.validate_context(context_values(vm_name="vm", user="admin"))
    target = SecretTarget(vm={key: EnvEntry({"secret": "still-needed"})})
    needed = compute_needed_secrets([target], build_registry(config), allow_transient_auto_declare=True)
    assert [decl.name for decl in needed] == ["still-needed"]


def test_session_legacy_context_validation_needs_no_secret_values_and_preserves_field_identity() -> None:
    from agentworks.interpolation import context_values

    target = SecretTarget(
        vm={"VM_LITERAL": EnvEntry("{{workspace_name}}")},
        session={
            "SESSION_LABEL": EnvEntry("{{workspace_name}}"),
            "SECRET": EnvEntry({"secret": "unavailable"}),
        },
        label="session=work",
    )

    with pytest.raises(ValidationError) as caught:
        target.validate_context(context_values(vm_name="box", user="operator", session_name="work"))

    assert caught.value.entity_kind == "template"
    assert caught.value.entity_name == "session=work.env.SESSION_LABEL"


def test_session_validation_execution_and_shell_share_one_legacy_warning(warnings: list[str]) -> None:
    from agentworks.capabilities.base import RunContext
    from agentworks.capabilities.harness_integration import HarnessLaunchIntent, HarnessStart, ShellIntegration
    from agentworks.env import ResourceContext, compose_env
    from agentworks.interpolation import runtime_values

    ctx = ResourceContext(
        vm_name="box", platform="lima", site="local", user="operator", workspace_name="project", session_name="work"
    )
    target = SecretTarget(
        vm={"VM_LITERAL": EnvEntry("{{session_name}}")},
        session={
            "LABEL": EnvEntry("{{session_name}}/{{workspace_name}}"),
            "AGENTWORKS_SESSION": EnvEntry("{{workspace_name}}"),
            "SECRET": EnvEntry({"secret": "credential"}),
        },
    )

    target.validate_context(runtime_values(ctx))
    assert len(warnings) == 1
    out = compose_env(values={"credential": "{{workspace_name}}"}, ctx=ctx, vm=target.vm, session=target.session)

    assert out["LABEL"] == "work/project"
    assert out["VM_LITERAL"] == "{{session_name}}"
    assert out["SECRET"] == "{{workspace_name}}"
    integration = ShellIntegration(
        "fixture",
        {"command": "{{session_name}}/{{workspace_name}}"},
        session_name="work",
        vm_name="box",
        workspace_name="project",
        workspace_path="/srv/project",
        target=None,
        admin=True,
        state={},
    )
    integration.prepare_context(runtime_values(ctx), intent=HarnessLaunchIntent.CREATE)
    launch = integration.start(RunContext(), intent=HarnessLaunchIntent.CREATE)
    assert isinstance(launch, HarnessStart)
    assert launch.command == "work/project"
    assert len(warnings) == 1


def test_empty_session_scope_does_not_enable_legacy_lower_scope_validation(warnings: list[str]) -> None:
    from agentworks.interpolation import context_values

    target = SecretTarget(vm={"VM_LITERAL": EnvEntry("{{workspace_name}}")}, session={})

    target.validate_context(context_values(vm_name="box", user="operator", session_name="work"))

    assert warnings == []
