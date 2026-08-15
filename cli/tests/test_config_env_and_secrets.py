"""Registry-boundary tests for the env / secrets surface added in Phase 2 of
the env-and-secrets effort.

config.toml is settings only now (ADR 0022): the env-carrying resources
(admin-template, vm/agent/workspace/session templates) and the
``[secrets.*]`` declarations are YAML manifests under ``resources/``, and
the decode + validation that used to run at ``load_config`` now runs at
``build_registry`` (envelope decode) and ``registry.finalize`` (chain /
capability validation). These tests exercise the same guarantees through
that boundary:

- env tables on admin-template / vm / workspace / agent / session templates
  parse into ``dict[str, EnvEntry]`` (plaintext + secret-ref shapes), read
  back off the finalized registry.
- env key validation (regex; rejects invalid names).
- AGENTWORKS_* override emits a warning (now on the ManifestSet, not
  cfg.config_issues).
- secret manifests parse into SecretDecls including all backend_mappings
  value forms (string, dict, false). ``true`` is rejected.
- [secret_config].sources drives the active source chain; precedence
  preserved (still a config setting).
- Unknown source names in [secret_config].sources raise ConfigError.
- Unreachable secrets raise ConfigError at build_registry.
- Env entries referencing undeclared secrets load cleanly (the Registry's
  auto-declare miss policy; auto-decl coverage lives in
  tests/test_env_block_references.py, runtime-failure coverage in
  tests/test_secrets_resolve.py).
- Settings-only config (no secret manifests, no [secret_config]) still
  builds; the default chain applies with nothing to resolve.
"""

from __future__ import annotations

from textwrap import dedent
from typing import TYPE_CHECKING

import pytest

from agentworks.bootstrap import build_registry
from agentworks.config import ConfigError, load_config
from agentworks.manifests import RESOURCES_DIRNAME, load_manifests
from agentworks.secrets.policy import InteractionPolicy
from agentworks.secrets.resolve import (
    CompletionPolicy,
    ResolutionPolicy,
    active_sources,
    resolve_batch,
)
from tests.conftest import ManifestDoc, write_manifests

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


def _write_base(
    config_path: Path,
    *,
    settings: str = "",
    manifests: Sequence[ManifestDoc | str] = (),
) -> None:
    """Write a settings-only config.toml plus its resources/ manifests.

    ``settings`` carries settings-only TOML ([secret_config], [plugins]);
    every resource under test goes in ``manifests``.
    """
    pub = config_path.parent / "id.pub"
    priv = config_path.parent / "id"
    pub.write_text("ssh-ed25519 AAAA...")
    priv.write_text("-----BEGIN OPENSSH PRIVATE KEY-----")

    config_path.write_text(
        dedent(f"""\
        [operator]
        ssh_public_key = "{pub.as_posix()}"
        ssh_private_key = "{priv.as_posix()}"

        """)
        + dedent(settings),
    )
    if manifests:
        write_manifests(config_path.parent, *manifests)


def _load(cfg_file: Path):  # type: ignore[no-untyped-def]
    """Load the settings config and build the finalized registry: the
    boundary where manifest decode and chain/site validation now surface."""
    return build_registry(load_config(cfg_file, warn_issues=False))


def _manifest_issues(cfg_file: Path) -> tuple[str, ...]:
    """The spec-level warnings the resources/ manifests raise (env hygiene,
    nonconforming secret names, unknown keys). config.toml is settings only
    now, so these ride the ManifestSet, not cfg.config_issues."""
    return load_manifests(cfg_file.parent / RESOURCES_DIRNAME).issues


def test_no_secrets_section_loads_with_default_chain(tmp_path: Path) -> None:
    """When no secrets are configured, the default chain still stands
    up: call sites can run the resolve loop unconditionally. With no
    [secret_config] in the TOML, SecretConfig defaults to the standard
    env-var + prompt chain; with no declared secrets there is nothing
    to resolve (no source is consulted)."""
    cfg_file = tmp_path / "config.toml"
    _write_base(cfg_file)
    cfg = load_config(cfg_file, warn_issues=False)
    # Absence of [secret_config] defaults to the standard chain.
    assert cfg.secret_config_data.sources == ("env-var", "prompt")
    registry = build_registry(cfg)
    # No operator-declared secrets: config carries none, and no manifest
    # declares one (only the ever-present auto-declared tailscale-auth-key
    # remains).
    assert all(decl.origin.variant != "operator-declared" for _name, decl in registry.iter_kind_items("secret"))
    sources = active_sources(cfg, registry)
    assert [source.name for source in sources] == ["env-var", "prompt"]
    # No declared secrets => nothing to resolve; the loop is a no-op.
    batch = resolve_batch(
        [],
        sources,
        policy=ResolutionPolicy(
            interaction=InteractionPolicy.REFUSE,
            completion=CompletionPolicy.COMPLETE,
        ),
        interaction_broker=None,
    )
    assert batch.complete_or_raise() == {}


def test_secret_config_absent_uses_default_chain(tmp_path: Path) -> None:
    """With no [secret_config] table, the loader uses the default chain
    so zero-config secret refs Just Work. Operator who writes
    `KEY = { secret = "x" }` doesn't have to also configure sources."""
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        manifests=[
            ManifestDoc("admin-template", "default", {"env": {"API_KEY": {"secret": "api-key"}}}),
            ManifestDoc("secret", "api-key", description="API token"),
        ],
    )
    cfg = load_config(cfg_file, warn_issues=False)
    assert cfg.secret_config_data.sources == ("env-var", "prompt")


def test_secret_config_table_without_sources_uses_default_chain(tmp_path: Path) -> None:
    """[secret_config] without an explicit sources key still falls back
    to the default chain. This shape lets operators reserve the table
    for future fields without losing the default resolution behavior."""
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        settings="""
        [secret_config]
        """,
    )
    cfg = load_config(cfg_file, warn_issues=False)
    assert cfg.secret_config_data.sources == ("env-var", "prompt")


def test_secret_config_explicit_empty_list_disables_resolution(tmp_path: Path) -> None:
    """An explicit `sources = []` is respected (operator opts out
    entirely). Distinct from absence-of-config, which gets the default."""
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        settings="""
        [secret_config]
        sources = []
        """,
    )
    cfg = load_config(cfg_file, warn_issues=False)
    assert cfg.secret_config_data.sources == ()


@pytest.mark.parametrize(
    "settings",
    [
        '[secret_config]\nbackends = ["env-var"]\n',
        '[secret_config]\nsources = ["env-var"]\nbackends = ["prompt"]\n',
    ],
    ids=["backends-only", "backends-and-sources"],
)
def test_retired_secret_config_backends_key_is_a_hard_error(tmp_path: Path, settings: str) -> None:
    cfg_file = tmp_path / "config.toml"
    _write_base(cfg_file, settings=settings)

    with pytest.raises(ConfigError):
        load_config(cfg_file, warn_issues=False)


def test_unrelated_secret_config_key_remains_a_soft_issue(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        settings="""
        [secret_config]
        sources = ["env-var"]
        future_setting = true
        """,
    )

    cfg = load_config(cfg_file, warn_issues=False)
    assert cfg.secret_config_data.sources == ("env-var",)
    assert len(cfg.config_issues) == 1


def test_admin_env_plaintext_and_secret(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        settings="""
        [secret_config]
        sources = ["env-var", "prompt"]
        """,
        manifests=[
            ManifestDoc(
                "admin-template",
                "default",
                {"env": {"HTTP_PROXY": "http://proxy:3128", "TOKEN": {"secret": "shared-token"}}},
            ),
            ManifestDoc("secret", "shared-token", description="Shared token"),
        ],
    )
    registry = _load(cfg_file)
    admin = registry.lookup("admin-template", "default")
    assert admin.env["HTTP_PROXY"].value == "http://proxy:3128"
    assert admin.env["TOKEN"].secret == "shared-token"


def test_vm_template_env(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        manifests=[ManifestDoc("vm-template", "default", {"env": {"EDITOR": "nvim"}})],
    )
    registry = _load(cfg_file)
    assert registry.lookup("vm-template", "default").env["EDITOR"].value == "nvim"
    # Resolved VM also carries the env.
    from agentworks.vms.templates import resolve_template

    assert resolve_template(registry, "default").env["EDITOR"].value == "nvim"


def test_agent_template_env(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        settings="""
        [secret_config]
        sources = ["env-var", "prompt"]
        """,
        manifests=[
            ManifestDoc(
                "agent-template",
                "claude",
                {"env": {"LOG_LEVEL": "info", "ANTHROPIC_API_KEY": {"secret": "anthropic-api-key"}}},
            ),
            ManifestDoc("secret", "anthropic-api-key", description="Anthropic API key"),
        ],
    )
    registry = _load(cfg_file)
    agent = registry.lookup("agent-template", "claude")
    assert agent.env["LOG_LEVEL"].value == "info"
    assert agent.env["ANTHROPIC_API_KEY"].secret == "anthropic-api-key"


def test_workspace_template_env(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        manifests=[
            ManifestDoc(
                "workspace-template",
                "gruntweave",
                {"repo": "https://example.com/org/repo.git", "env": {"EXTRA": "value"}},
            )
        ],
    )
    registry = _load(cfg_file)
    assert registry.lookup("workspace-template", "gruntweave").env["EXTRA"].value == "value"


def test_session_template_env_plaintext_and_secret(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        settings="""
        [secret_config]
        sources = ["env-var", "prompt"]
        """,
        manifests=[
            ManifestDoc(
                "session-template",
                "shell",
                {"env": {"EDITOR": "nvim", "API_KEY": {"secret": "anthropic-api-key"}}},
            ),
            ManifestDoc("secret", "anthropic-api-key", description="Anthropic API key"),
        ],
    )
    registry = _load(cfg_file)
    tmpl = registry.lookup("session-template", "shell")
    assert tmpl.env is not None
    assert tmpl.env["EDITOR"].value == "nvim"
    assert tmpl.env["API_KEY"].secret == "anthropic-api-key"


def test_invalid_env_key_raises(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        manifests=[ManifestDoc("admin-template", "default", {"env": {"1BAD": "value"}})],
    )
    with pytest.raises(ConfigError, match="invalid env var name"):
        _load(cfg_file)


def test_agentworks_prefix_env_emits_warning(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        manifests=[ManifestDoc("admin-template", "default", {"env": {"AGENTWORKS_VM": "override-bad"}})],
    )
    issues = _manifest_issues(cfg_file)
    assert any("AGENTWORKS_VM" in issue and "identity variable" in issue for issue in issues), issues


def test_env_inline_table_unknown_key_rejected(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        manifests=[ManifestDoc("admin-template", "default", {"env": {"BAD": {"secrit": "x"}}})],
    )
    with pytest.raises(ConfigError, match="env.BAD.secrit: unknown field; expected one of: secret, value"):
        _load(cfg_file)


def test_env_inline_table_may_spell_the_plaintext_value(tmp_path: Path) -> None:
    """A widening the model brings: ``{value: x}`` is the same entry as the
    bare string ``x``, and the entry type genuinely has both fields, so
    refusing the explicit spelling would be an arbitrary rule rather than
    a shape check."""
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        manifests=[ManifestDoc("admin-template", "default", {"env": {"OK": {"value": "x"}}})],
    )

    assert _load(cfg_file).lookup("admin-template", "default").env["OK"].value == "x"


def test_env_secret_must_be_string(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        manifests=[ManifestDoc("admin-template", "default", {"env": {"BAD": {"secret": 42}}})],
    )
    with pytest.raises(ConfigError, match="secret"):
        _load(cfg_file)


def test_env_referencing_undeclared_secret_does_not_error(
    tmp_path: Path,
) -> None:
    """The Registry's auto-declare miss policy handles an env-block secret
    ref that has no ``secret`` manifest: the build no longer errors (the
    strict config-load error is gone). This verifies the build succeeds and
    the entry is preserved; the auto-declare path itself is covered by
    ``tests/test_env_block_references.py``.
    """
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        settings="""
        [secret_config]
        sources = ["env-var"]
        """,
        manifests=[ManifestDoc("admin-template", "default", {"env": {"API_KEY": {"secret": "missing"}}})],
    )
    # No raise: the secret auto-declares through the framework.
    registry = _load(cfg_file)
    assert registry.lookup("admin-template", "default").env["API_KEY"].secret == "missing"


def test_secret_declared_with_all_mapping_forms(tmp_path: Path) -> None:
    """Scalar mappings and framework ``false`` are keyed by source names.

    OnePassword account selection belongs to the declared source while its
    per-secret mapping is the final scalar ``op://`` reference.
    """
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        settings="""
        [plugins]
        system = ["onepassword"]

        [secret_config]
        sources = ["prompt"]
        """,
        manifests=[
            ManifestDoc(
                "secret-source",
                "team-op",
                {"backend": {"name": "onepassword", "account": "acme"}},
            ),
            ManifestDoc(
                "secret", "token-a", {"backend_mappings": {"env-var": "OVERRIDE_NAME"}}, description="string mapping"
            ),
            ManifestDoc(
                "secret",
                "token-b",
                {"backend_mappings": {"team-op": "op://Shared/Tok/key"}},
                description="scalar mapping for a source outside the chain",
            ),
            ManifestDoc("secret", "token-c", {"backend_mappings": {"env-var": False}}, description="opt-out mapping"),
        ],
    )
    registry = _load(cfg_file)
    assert registry.lookup("secret", "token-a").backend_mappings == {"env-var": "OVERRIDE_NAME"}
    assert registry.lookup("secret", "token-b").backend_mappings == {"team-op": "op://Shared/Tok/key"}
    assert registry.lookup("secret", "token-c").backend_mappings == {"env-var": False}


def test_secret_name_over_username_cap_loads_from_manifest(tmp_path: Path) -> None:
    """Issue #275: the secret decoder validates secret names against the
    larger secret cap. The git-token-<credential> default (33 chars) loads
    even though it exceeds the 30-char username cap."""
    cfg_file = tmp_path / "config.toml"
    long_name = "git-token-github-fg-wf-agw-tester"  # 33 chars
    assert len(long_name) > 30
    _write_base(
        cfg_file,
        manifests=[ManifestDoc("secret", long_name, description="PAT for the tester credential")],
    )
    registry = _load(cfg_file)
    assert registry.lookup("secret", long_name).name == long_name


def test_secret_name_over_secret_cap_rejected_from_manifest(tmp_path: Path) -> None:
    """A secret name beyond the secret cap (253) is rejected, and the error
    reports the correct (secret) max, not 30. The name-validation error is a
    spec-level failure, so the decoder surfaces it as ConfigError."""
    from agentworks.naming import MAX_SECRET_NAME_LENGTH

    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        manifests=[ManifestDoc("secret", "s" * (MAX_SECRET_NAME_LENGTH + 1), description="too long")],
    )
    with pytest.raises(ConfigError) as exc:
        _load(cfg_file)
    message = str(exc.value)
    assert "is too long" in message
    assert f"max {MAX_SECRET_NAME_LENGTH}" in message


def test_secret_true_in_backend_mappings_rejected(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        settings="""
        [secret_config]
        sources = ["env-var"]
        """,
        manifests=[ManifestDoc("secret", "token", {"backend_mappings": {"env-var": True}}, description="bad")],
    )
    with pytest.raises(ConfigError, match="true"):
        _load(cfg_file)


def test_secret_config_sources_preserves_precedence(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        settings="""
        [secret_config]
        sources = ["env-var", "prompt"]
        """,
    )
    cfg = load_config(cfg_file, warn_issues=False)
    assert cfg.secret_config_data.sources == ("env-var", "prompt")


def test_active_sources_stand_up_when_configured(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        settings="""
        [secret_config]
        sources = ["env-var", "prompt"]
        """,
        manifests=[ManifestDoc("secret", "shared", description="Shared token")],
    )
    cfg = load_config(cfg_file, warn_issues=False)
    registry = build_registry(cfg)
    sources = active_sources(cfg, registry)
    # Smoke-check the chain: the first attempting source is env-var.
    decl = registry.lookup("secret", "shared")
    first = next((source for source in sources if source.would_attempt(decl)), None)
    assert first is not None
    assert first.name == "env-var"


def test_unknown_source_name_raises(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        settings="""
        [secret_config]
        sources = ["env-var", "totally-fake-source"]
        """,
    )
    # An unknown source name fails settings-reference validation after
    # registry finalization.
    cfg = load_config(cfg_file, warn_issues=False)
    with pytest.raises(ConfigError, match="totally-fake-source"):
        build_registry(cfg)


def test_unreachable_secret_raises(tmp_path: Path) -> None:
    """A secret with env-var = false and a source chain with no other
    attempting source is unreachable; ``validate_chain`` rejects it at
    ``build_registry``."""
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        settings="""
        [secret_config]
        sources = ["env-var"]
        """,
        manifests=[
            ManifestDoc(
                "secret", "stranded", {"backend_mappings": {"env-var": False}}, description="no path to resolution"
            )
        ],
    )
    cfg = load_config(cfg_file, warn_issues=False)
    with pytest.raises(ConfigError, match="unreachable"):
        build_registry(cfg)


def test_reachability_scope_is_operator_declared_only(tmp_path: Path) -> None:
    """Reachability preservation invariant (LLD d): the check covers
    OPERATOR-declared secrets only. With ``sources = []`` every secret is
    unreachable, but the only secrets present are auto-declared (the
    ever-present tailscale-auth-key), so the build SUCCEEDS; an auto-declared
    secret cannot invalidate a deliberate empty-chain opt-out (it surfaces at
    use time instead)."""
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        settings="""
        [secret_config]
        sources = []
        """,
    )
    cfg = load_config(cfg_file, warn_issues=False)
    registry = build_registry(cfg)  # no raise: no operator-declared secret is unreachable
    assert any(name == "tailscale-auth-key" for name, _ in registry.iter_kind_items("secret"))


def test_reachability_keying_is_would_attempt_readiness_blind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reachability preservation invariant (LLD d): the build-time check is
    keyed on WOULD-ATTEMPT (the frozen edges), READINESS-BLIND. A secret whose
    only opted-in source is team-op, whose onepassword backend is forced
    NOT-READY, is still reachable
    (the build succeeds); it would fail only at resolution, exactly as today."""
    from agentworks.plugins.onepassword.backend import OnePasswordBackend
    from agentworks.resources.graph import Readiness

    monkeypatch.setattr(OnePasswordBackend, "not_ready", lambda self: Readiness.blocked("op CLI not installed"))
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        settings="""
        [plugins]
        system = ["onepassword"]

        [secret_config]
        sources = ["team-op"]
        """,
        manifests=[
            ManifestDoc(
                "secret-source",
                "team-op",
                {"backend": {"name": "onepassword"}},
            ),
            ManifestDoc(
                "secret",
                "vaulted",
                {"backend_mappings": {"team-op": "op://Work/item/field"}},
                description="only resolvable via onepassword",
            ),
        ],
    )
    cfg = load_config(cfg_file, warn_issues=False)
    registry = build_registry(cfg)  # no raise: not-ready does not make it unreachable
    assert registry.graph.readiness_of("secret-source", "team-op").reason == "op CLI not installed"
    assert any(name == "vaulted" for name, _ in registry.iter_kind_items("secret"))


def test_unreachable_secret_error_message_and_hint(tmp_path: Path) -> None:
    """The unreachable-secret error keeps its message short (just the
    affected secret names) and surfaces remediation via the typed hint,
    so the doctor renderer can show it on a separate line and other
    surfaces (raw exception) still see the actionable text."""
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        settings="""
        [secret_config]
        sources = ["env-var"]
        """,
        manifests=[
            ManifestDoc(
                "secret", "stranded", {"backend_mappings": {"env-var": False}}, description="no path to resolution"
            )
        ],
    )
    cfg = load_config(cfg_file, warn_issues=False)
    with pytest.raises(ConfigError) as exc:
        build_registry(cfg)

    # Message is short: just the affected secrets, no remediation noise.
    assert "stranded" in str(exc.value)
    assert "unreachable secret" in str(exc.value)
    # Remediation lives in the hint, not the message.
    assert exc.value.hint is not None
    assert "active source chain" in exc.value.hint
    assert "env-var" in exc.value.hint
    # The hint mentions the three remediation paths.
    assert "attemptable secret-source" in exc.value.hint
    assert "remove" in exc.value.hint


#: Every scope that has an env table, with the address its warning frames
#: under. One decode over all five below rather than one test each: the
#: check finds its subject by ANNOTATION (``decode._env_hygiene_issues``
#: matches a mapping of ``EnvEntry``), so there is no per-scope branch to
#: exercise separately; what is worth pinning is that no scope is missed.
_ENV_BEARING_SCOPES = [
    (ManifestDoc("vm-template", "default", {"env": {"AGENTWORKS_VM": "override"}}), "vm-template/default.env"),
    (
        ManifestDoc("admin-template", "default", {"env": {"AGENTWORKS_PLATFORM": "override"}}),
        "admin-template/default.env",
    ),
    (ManifestDoc("agent-template", "claude", {"env": {"AGENTWORKS_AGENT": "override"}}), "agent-template/claude.env"),
    (
        ManifestDoc("workspace-template", "ws", {"env": {"AGENTWORKS_WORKSPACE": "override"}}),
        "workspace-template/ws.env",
    ),
    (
        ManifestDoc("session-template", "shell", {"env": {"AGENTWORKS_SESSION": "override"}}),
        "session-template/shell.env",
    ),
]


def test_agentworks_prefix_warning_fires_for_every_scope(tmp_path: Path) -> None:
    """The AGENTWORKS_* override warning fires for every scope's env table,
    not just admin.env. Pin this so a future refactor that moves the check
    into a per-scope code path doesn't silently miss some scopes.

    Every missed scope is named, rather than the first: a refactor that
    dropped three of them should say so in one run.
    """
    cfg_file = tmp_path / "config.toml"
    _write_base(cfg_file, manifests=[manifest for manifest, _label in _ENV_BEARING_SCOPES])
    issues = _manifest_issues(cfg_file)

    unwarned = [
        label
        for _manifest, label in _ENV_BEARING_SCOPES
        if not any(label in issue and "identity variable" in issue for issue in issues)
    ]
    assert unwarned == [], f"env tables whose AGENTWORKS_* override went unwarned: {unwarned}; issues were {issues}"


def test_plaintext_env_with_newline_warns(tmp_path: Path) -> None:
    """Per ADR 0014: a newline in a plaintext env value would corrupt
    the SSH SetEnv argument shape. Catch it at decode so the operator
    sees a clear message instead of an opaque SSH-side rejection."""
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        manifests=[ManifestDoc("admin-template", "default", {"env": {"MULTILINE": "line1\nline2"}})],
    )
    issues = _manifest_issues(cfg_file)
    assert any("MULTILINE" in issue and "newline" in issue for issue in issues), issues


def test_session_template_inherits_parent_env(tmp_path: Path) -> None:
    """A child session template with no env of its own inherits the parent's env
    unchanged. Pins None-vs-empty handling in the merge."""
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        manifests=[
            ManifestDoc("session-template", "parent", {"env": {"EDITOR": "nvim"}}),
            ManifestDoc("session-template", "child", {"inherits": ["parent"]}),
        ],
    )
    registry = _load(cfg_file)
    # Resolve the child template through the inheritance chain.
    from agentworks.sessions.templates import resolve_template

    resolved = resolve_template(registry, "child")
    assert resolved.env["EDITOR"].value == "nvim"


def test_session_template_required_commands_parsed(tmp_path: Path) -> None:
    """The ``required_commands`` config rides the ``shell`` harness
    integration's config blob (the integration surface owns the command
    vocabulary now)."""
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        manifests=[
            ManifestDoc(
                "session-template",
                "claude",
                {
                    "harness_integration": {
                        "name": "shell",
                        "command": "claude --name {{session_name}}",
                        "required_commands": ["claude"],
                    }
                },
            )
        ],
    )
    registry = _load(cfg_file)
    tmpl = registry.lookup("session-template", "claude")
    assert tmpl.harness_integration.name == "shell"
    assert tmpl.harness_integration.config == {
        "command": "claude --name {{session_name}}",
        "required_commands": ["claude"],
    }


def test_session_template_required_commands_must_be_list(tmp_path: Path) -> None:
    """A non-list ``required_commands`` is rejected. The shell harness
    integration's validate pass fires at finalize (build_registry)."""
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        manifests=[
            ManifestDoc(
                "session-template",
                "claude",
                {"harness_integration": {"name": "shell", "command": "claude", "required_commands": "claude"}},
            )
        ],
    )
    with pytest.raises(ConfigError, match="required_commands: must be a list"):
        _load(cfg_file)


def test_session_template_required_commands_must_be_strings(tmp_path: Path) -> None:
    """Non-string elements (e.g. ints) are rejected, not silently coerced
    via ``str()``. Pinning the type-strict behavior so a future refactor
    that drops the list-of-strings check would surface here."""
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        manifests=[
            ManifestDoc(
                "session-template",
                "claude",
                {"harness_integration": {"name": "shell", "command": "claude", "required_commands": [123]}},
            )
        ],
    )
    with pytest.raises(ConfigError, match=r"required_commands\[0\]: must be a string"):
        _load(cfg_file)


def test_session_template_required_commands_union_on_inherit(tmp_path: Path) -> None:
    """``required_commands`` is unioned (parents + child, de-duplicated) across
    the inheritance chain, matching the merge semantics of other list fields."""
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        manifests=[
            ManifestDoc(
                "session-template",
                "parent",
                {"harness_integration": {"name": "shell", "required_commands": ["tmux", "claude"]}},
            ),
            ManifestDoc(
                "session-template",
                "child",
                {
                    "inherits": ["parent"],
                    "harness_integration": {"name": "shell", "required_commands": ["claude", "jq"]},
                },
            ),
        ],
    )
    registry = _load(cfg_file)
    from agentworks.sessions.templates import resolve_template

    resolved = resolve_template(registry, "child")
    assert resolved.harness_integration == "shell"
    assert resolved.harness_integration_config["required_commands"] == ["tmux", "claude", "jq"]


def test_undeclared_secret_in_parent_no_longer_errors(
    tmp_path: Path,
) -> None:
    """A parent template's env secret-ref to an undeclared name no longer
    errors: the Registry's auto-declare miss policy handles it at finalize
    regardless of whether a child template overrides the key with plaintext.
    The override semantics still apply at resolution time (if the child
    overrides with a literal, the parent's secret-ref doesn't actually need
    resolution), but that's a runtime concern, not a build concern.
    """
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        settings="""
        [secret_config]
        sources = ["env-var", "prompt"]
        """,
        manifests=[
            ManifestDoc("agent-template", "parent", {"env": {"TOKEN": {"secret": "missing-secret"}}}),
            ManifestDoc("agent-template", "child", {"inherits": ["parent"], "env": {"TOKEN": "literal-value"}}),
        ],
    )
    # No longer raises.
    registry = _load(cfg_file)
    assert registry.lookup("agent-template", "parent").env["TOKEN"].secret == "missing-secret"
    assert registry.lookup("agent-template", "child").env["TOKEN"].value == "literal-value"


# --- Issue #279: warn-only validation of operator-supplied secret NAMES ------
#
# Names declared explicitly in a ``secret`` manifest are validated with a hard
# error. Names that enter through a REFERENCE (a VM template's
# tailscale_auth_key, an env entry's `secret = "..."`, a git credential's
# token) historically bypassed that check. They now emit a non-fatal warning
# yet STILL load and resolve unchanged, so no config that loads today newly
# fails.


def test_vm_template_tailscale_auth_key_nonconforming_warns_but_loads(
    tmp_path: Path,
) -> None:
    """A VM template naming a non-conforming (uppercase) tailscale_auth_key
    secret loads successfully, keeps the name usable, and warns naming the
    secret and its config location."""
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        manifests=[ManifestDoc("vm-template", "tester", {"tailscale_auth_key": "GITHUB_TOKEN"})],
    )
    registry = _load(cfg_file)
    # The secret name is preserved exactly, still declared and usable.
    assert registry.lookup("vm-template", "tester").tailscale_auth_key == "GITHUB_TOKEN"
    issues = _manifest_issues(cfg_file)
    assert any("GITHUB_TOKEN" in issue and "vm-template/tester" in issue for issue in issues), issues
    assert any("the Tailscale auth key" in issue for issue in issues), issues


def test_env_secret_ref_nonconforming_warns_but_loads(tmp_path: Path) -> None:
    """An env entry referencing a non-conforming secret name loads with a
    warning; the entry is preserved, not dropped."""
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        manifests=[ManifestDoc("admin-template", "default", {"env": {"FOO": {"secret": "Bad_Name"}}})],
    )
    registry = _load(cfg_file)
    assert registry.lookup("admin-template", "default").env["FOO"].secret == "Bad_Name"
    issues = _manifest_issues(cfg_file)
    assert any("Bad_Name" in issue and "admin-template/default" in issue for issue in issues), issues


def test_git_credential_token_nonconforming_warns_but_loads(tmp_path: Path) -> None:
    """A git credential whose token names a non-conforming secret loads with a
    warning; the token is preserved."""
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        manifests=[ManifestDoc("git-credential", "gh", {"provider": {"name": "github", "token": "GITHUB_TOKEN"}})],
    )
    registry = _load(cfg_file)
    assert registry.lookup("git-credential", "gh").provider.config["token"] == "GITHUB_TOKEN"
    issues = _manifest_issues(cfg_file)
    assert any("GITHUB_TOKEN" in issue and "git-credential/gh" in issue for issue in issues), issues


def test_conforming_secret_ref_names_emit_no_warning(tmp_path: Path) -> None:
    """Conforming secret names across all reference paths load with NO
    secret-naming warning."""
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        manifests=[
            ManifestDoc("vm-template", "tester", {"tailscale_auth_key": "tailscale-auth-key"}),
            ManifestDoc("admin-template", "default", {"env": {"FOO": {"secret": "github-token"}}}),
            ManifestDoc("git-credential", "gh", {"provider": {"name": "github", "token": "git-token-github"}}),
        ],
    )
    issues = _manifest_issues(cfg_file)
    assert not any("secret naming rules" in issue for issue in issues), issues


def test_explicit_secret_declaration_invalid_still_raises(tmp_path: Path) -> None:
    """Status quo: an explicit ``secret`` manifest with a non-conforming
    name still raises at build (warn-only applies to REFERENCES, not explicit
    declarations). The name-validation error is a spec-level failure, so the
    decoder surfaces it as ConfigError."""
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        manifests=[ManifestDoc("secret", "GITHUB_TOKEN", description="non-conforming explicit declaration")],
    )
    with pytest.raises(ConfigError, match="invalid name"):
        _load(cfg_file)


def test_secretdecl_construction_tolerates_nonconforming_operator_name() -> None:
    """The runtime path the prior (rejected) attempt would have broken:
    constructing a SecretDecl for a non-conforming operator name must NOT raise.
    Synthesize / resolve paths stay tolerant so no command newly fails."""
    from agentworks.secrets import SecretDecl

    decl = SecretDecl(name="GITHUB_TOKEN", description="")
    assert decl.name == "GITHUB_TOKEN"
