"""Config loader tests for the env / secrets surface added in Phase 2 of the
env-and-secrets effort.

These cover:

- env tables on AdminConfig / VMTemplate / WorkspaceTemplate / AgentTemplate /
  SessionTemplate parse into ``dict[str, EnvEntry]`` (plaintext + secret-ref shapes).
- env key validation (regex; rejects invalid names).
- AGENTWORKS_* override emits a load-time warning.
- [secrets.*] parses into SecretDecls including all backend_mappings value forms
  (string, dict, false). ``true`` is rejected.
- [secret_config].backends drives the active backend chain; precedence preserved.
- Unknown backend kinds in [secret_config].backends raise ConfigError.
- Unreachable secrets raise ConfigError at load time.
- Env entries referencing undeclared secrets load cleanly (Phase 1b of the
  Resource Registry SDD removed the strict-error path; auto-decl coverage
  lives in tests/test_env_block_requirements.py, runtime-failure coverage
  in tests/test_secrets_resolve.py).
- Mid-config without any [secrets] / [secret_config] still loads cleanly;
  the default chain applies with nothing to resolve.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from agentworks.bootstrap import build_registry
from agentworks.config import ConfigError, load_config
from agentworks.secrets import active_backends, resolve_secrets


def _write_base(config_path: Path, *, extras: str = "") -> None:
    pub = config_path.parent / "id.pub"
    priv = config_path.parent / "id"
    pub.write_text("ssh-ed25519 AAAA...")
    priv.write_text("-----BEGIN OPENSSH PRIVATE KEY-----")

    config_path.write_text(
        dedent(f"""\
        [operator]
        ssh_public_key = "{pub.as_posix()}"
        ssh_private_key = "{priv.as_posix()}"

        [vm_templates.default]
        apt = ["zsh"]

        [admin.config]
        shell = "zsh"

        [defaults]
        """)
        + dedent(extras),
    )


def test_no_secrets_section_loads_with_default_chain(tmp_path: Path) -> None:
    """When no secrets are configured, the default chain still stands
    up: call sites can run the resolve loop unconditionally. With no
    [secret_config] in the TOML, SecretConfig defaults to the standard
    env-var + prompt chain; with no declared secrets there is nothing
    to resolve (no backend is consulted)."""
    cfg_file = tmp_path / "config.toml"
    _write_base(cfg_file)
    cfg = load_config(cfg_file, warn_issues=False)
    assert cfg.secrets == {}
    # Absence of [secret_config] defaults to the standard chain.
    assert cfg.secret_config_data.backends == ("env-var", "prompt")
    registry = build_registry(cfg)
    backends = active_backends(cfg, registry)
    assert [b.name for b in backends] == ["env-var", "prompt"]
    # No declared secrets => nothing to resolve; the loop is a no-op.
    assert resolve_secrets([], backends) == {}


def test_secret_config_absent_uses_default_chain(tmp_path: Path) -> None:
    """With no [secret_config] table, the loader uses the default chain
    so zero-config secret refs Just Work. Operator who writes
    `KEY = { secret = "x" }` doesn't have to also configure backends."""
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        extras="""
        [admin.env]
        API_KEY = { secret = "api-key" }

        [secrets.api-key]
        description = "API token"
        """,
    )
    cfg = load_config(cfg_file, warn_issues=False)
    assert cfg.secret_config_data.backends == ("env-var", "prompt")


def test_secret_config_table_without_backends_uses_default_chain(tmp_path: Path) -> None:
    """[secret_config] without an explicit backends key still falls back
    to the default chain. This shape lets operators reserve the table
    for future fields without losing the default resolution behavior."""
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        extras="""
        [secret_config]
        """,
    )
    cfg = load_config(cfg_file, warn_issues=False)
    assert cfg.secret_config_data.backends == ("env-var", "prompt")


def test_secret_config_explicit_empty_list_disables_resolution(tmp_path: Path) -> None:
    """An explicit `backends = []` is respected (operator opts out
    entirely). Distinct from absence-of-config, which gets the default."""
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        extras="""
        [secret_config]
        backends = []
        """,
    )
    cfg = load_config(cfg_file, warn_issues=False)
    assert cfg.secret_config_data.backends == ()


def test_admin_env_plaintext_and_secret(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        extras="""
        [admin.env]
        HTTP_PROXY = "http://proxy:3128"
        TOKEN = { secret = "shared-token" }

        [secrets.shared-token]
        description = "Shared token"

        [secret_config]
        backends = ["env-var", "prompt"]
        """,
    )
    cfg = load_config(cfg_file, warn_issues=False)
    assert cfg.admin.env["HTTP_PROXY"].value == "http://proxy:3128"
    assert cfg.admin.env["TOKEN"].secret == "shared-token"


def test_vm_template_env(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        extras="""
        [vm_templates.default.env]
        EDITOR = "nvim"
        """,
    )
    cfg = load_config(cfg_file, warn_issues=False)
    assert cfg.vm_templates["default"].env["EDITOR"].value == "nvim"
    # Resolved VM also carries the env.
    from agentworks.vms.templates import resolve_from_dict as _resolve_vm

    assert _resolve_vm(cfg.vm_templates).env["EDITOR"].value == "nvim"


def test_agent_template_env(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        extras="""
        [agent_templates.claude.env]
        LOG_LEVEL = "info"
        ANTHROPIC_API_KEY = { secret = "anthropic-api-key" }

        [secrets.anthropic-api-key]
        description = "Anthropic API key"

        [secret_config]
        backends = ["env-var", "prompt"]
        """,
    )
    cfg = load_config(cfg_file, warn_issues=False)
    agent = cfg.agent_templates["claude"]
    assert agent.env["LOG_LEVEL"].value == "info"
    assert agent.env["ANTHROPIC_API_KEY"].secret == "anthropic-api-key"


def test_workspace_template_env(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        extras="""
        [workspace_templates.gruntweave]
        repo = "https://example.com/org/repo.git"

        [workspace_templates.gruntweave.env]
        EXTRA = "value"
        """,
    )
    cfg = load_config(cfg_file, warn_issues=False)
    assert cfg.workspace_templates["gruntweave"].env["EXTRA"].value == "value"


def test_session_template_env_plaintext_and_secret(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        extras="""
        [session_templates.shell.env]
        EDITOR = "nvim"
        API_KEY = { secret = "anthropic-api-key" }

        [secrets.anthropic-api-key]
        description = "Anthropic API key"

        [secret_config]
        backends = ["env-var", "prompt"]
        """,
    )
    cfg = load_config(cfg_file, warn_issues=False)
    tmpl = cfg.session_templates["shell"]
    assert tmpl.env is not None
    assert tmpl.env["EDITOR"].value == "nvim"
    assert tmpl.env["API_KEY"].secret == "anthropic-api-key"


def test_invalid_env_key_raises(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        extras="""
        [admin.env]
        "1BAD" = "value"
        """,
    )
    with pytest.raises(ConfigError, match="invalid env var name"):
        load_config(cfg_file, warn_issues=False)


def test_agentworks_prefix_env_emits_warning(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        extras="""
        [admin.env]
        AGENTWORKS_VM = "override-bad"
        """,
    )
    cfg = load_config(cfg_file, warn_issues=False)
    assert any(
        "AGENTWORKS_VM" in issue and "identity variable" in issue
        for issue in cfg.config_issues
    ), cfg.config_issues


def test_env_inline_table_unknown_key_rejected(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        extras="""
        [admin.env]
        BAD = { value = "x" }
        """,
    )
    with pytest.raises(ConfigError, match="unexpected keys"):
        load_config(cfg_file, warn_issues=False)


def test_env_secret_must_be_string(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        extras="""
        [admin.env]
        BAD = { secret = 42 }
        """,
    )
    with pytest.raises(ConfigError, match="secret"):
        load_config(cfg_file, warn_issues=False)


def test_env_referencing_undeclared_secret_does_not_error_at_load(
    tmp_path: Path,
) -> None:
    """Phase 1b of the Resource Registry SDD removed the strict
    config-load error for env-block secret refs that have no
    ``[secrets.<name>]`` block; the Registry's auto-declare miss policy
    handles the missing name at finalize. Verifying the load no longer
    errors; the auto-declare path is covered by
    ``tests/test_env_block_requirements.py``.
    """
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        extras="""
        [admin.env]
        API_KEY = { secret = "missing" }

        [secret_config]
        backends = ["env-var"]
        """,
    )
    # No longer raises -- the secret auto-declares through the framework.
    cfg = load_config(cfg_file, warn_issues=False)
    assert cfg.admin.env["API_KEY"].secret == "missing"


def test_secret_declared_with_all_mapping_forms(tmp_path: Path) -> None:
    """All three backend_mappings value shapes (string, inline table, false) parse
    onto SecretDecl. The chain uses prompt-only so even token-c (which opts out
    of env-var) and token-b (mapping for a future backend) stay reachable through
    the prompt backend."""
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        extras="""
        [secrets.token-a]
        description = "string mapping"
        backend_mappings.env-var = "OVERRIDE_NAME"

        [secrets.token-b]
        description = "structured mapping (for future backend)"
        backend_mappings.onepassword = { vault = "Shared", item = "Tok", field = "key" }

        [secrets.token-c]
        description = "opt-out mapping"
        backend_mappings.env-var = false

        [secret_config]
        backends = ["prompt"]
        """,
    )
    cfg = load_config(cfg_file, warn_issues=False)
    assert cfg.secrets["token-a"].backend_mappings == {"env-var": "OVERRIDE_NAME"}
    assert cfg.secrets["token-b"].backend_mappings == {
        "onepassword": {"vault": "Shared", "item": "Tok", "field": "key"}
    }
    assert cfg.secrets["token-c"].backend_mappings == {"env-var": False}


def test_secret_true_in_backend_mappings_rejected(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        extras="""
        [secrets.token]
        description = "bad"
        backend_mappings.env-var = true

        [secret_config]
        backends = ["env-var"]
        """,
    )
    with pytest.raises(ConfigError, match="true"):
        load_config(cfg_file, warn_issues=False)


def test_secret_config_backends_preserves_precedence(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        extras="""
        [secret_config]
        backends = ["env-var", "prompt"]
        """,
    )
    cfg = load_config(cfg_file, warn_issues=False)
    assert cfg.secret_config_data.backends == ("env-var", "prompt")


def test_active_backends_stand_up_when_configured(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        extras="""
        [secrets.shared]
        description = "Shared token"

        [secret_config]
        backends = ["env-var", "prompt"]
        """,
    )
    cfg = load_config(cfg_file, warn_issues=False)
    registry = build_registry(cfg)
    backends = active_backends(cfg, registry)
    # Smoke-check the chain: the first attempting backend is env-var.
    first = next(
        (b for b in backends if b.would_attempt(cfg.secrets["shared"])), None
    )
    assert first is not None
    assert first.name == "env-var"


def test_unknown_backend_kind_raises(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        extras="""
        [secret_config]
        backends = ["env-var", "totally-fake-backend"]
        """,
    )
    # The chain is reference edges on the published secret-config row
    # (resource-manifests SDD); an unknown name hits the secret-backend
    # kind's error miss policy at build_registry finalize.
    cfg = load_config(cfg_file, warn_issues=False)
    with pytest.raises(ConfigError, match="totally-fake-backend"):
        build_registry(cfg)


def test_unreachable_secret_raises(tmp_path: Path) -> None:
    """A secret with env-var = false and a backend chain with no other
    attempting backend is unreachable; ``validate_chain`` rejects it at
    ``build_registry``."""
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        extras="""
        [secrets.stranded]
        description = "no path to resolution"
        backend_mappings.env-var = false

        [secret_config]
        backends = ["env-var"]
        """,
    )
    cfg = load_config(cfg_file, warn_issues=False)
    with pytest.raises(ConfigError, match="unreachable"):
        build_registry(cfg)


def test_unreachable_secret_error_message_and_hint(tmp_path: Path) -> None:
    """The unreachable-secret error keeps its message short (just the
    affected secret names) and surfaces remediation via the typed hint,
    so the doctor renderer can show it on a separate line and other
    surfaces (raw exception) still see the actionable text."""
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        extras="""
        [secrets.stranded]
        description = "no path to resolution"
        backend_mappings.env-var = false

        [secret_config]
        backends = ["env-var"]
        """,
    )
    cfg = load_config(cfg_file, warn_issues=False)
    with pytest.raises(ConfigError) as exc:
        build_registry(cfg)

    # Message is short: just the affected secrets, no remediation noise.
    assert "stranded" in str(exc.value)
    assert "unreachable secret" in str(exc.value)
    # Remediation lives in the hint, not the message.
    assert exc.value.hint is not None
    assert "active backend chain" in exc.value.hint
    assert "env-var" in exc.value.hint
    # The hint mentions the three remediation paths.
    assert "prompt" in exc.value.hint
    assert "backend_mappings" in exc.value.hint
    assert "remove" in exc.value.hint


def test_unknown_backend_kind_in_secret_backends_errors(
    tmp_path: Path,
) -> None:
    """A typo in [secret_backends.<kind>] (e.g. 'env_var' or 'envvar'
    for 'env-var') errors at config-load time. Phase 2b.2 elevated this
    from a soft warning to a hard error so it matches the framework's
    treatment of [git_credentials.<name>].type typos.
    """
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        extras="""
        [secret_backends.env_var]
        # typo: kind is 'env-var' (kebab), not 'env_var' (snake)
        """,
    )
    with pytest.raises(ConfigError, match="unknown secret backend"):
        load_config(cfg_file, warn_issues=False)


@pytest.mark.parametrize(
    ("scope_extras", "context_label"),
    [
        ("[vm_templates.default.env]\nAGENTWORKS_VM = \"override\"", "vm_templates.default.env"),
        ("[admin.env]\nAGENTWORKS_PLATFORM = \"override\"", "admin.env"),
        ("[agent_templates.claude.env]\nAGENTWORKS_AGENT = \"override\"", "agent_templates.claude.env"),
        ("[workspace_templates.ws.env]\nAGENTWORKS_WORKSPACE = \"override\"", "workspace_templates.ws.env"),
        ("[session_templates.shell.env]\nAGENTWORKS_SESSION = \"override\"", "session_templates.shell.env"),
    ],
)
def test_agentworks_prefix_warning_fires_for_every_scope(
    tmp_path: Path, scope_extras: str, context_label: str,
) -> None:
    """The AGENTWORKS_* override warning fires for every scope's env table,
    not just admin.env. Pin this so a future refactor that moves the check
    into a per-scope code path doesn't silently miss some scopes."""
    cfg_file = tmp_path / "config.toml"
    _write_base(cfg_file, extras="\n" + scope_extras + "\n")
    cfg = load_config(cfg_file, warn_issues=False)
    assert any(
        context_label in issue and "identity variable" in issue
        for issue in cfg.config_issues
    ), cfg.config_issues


def test_plaintext_env_with_newline_warns_at_load(tmp_path: Path) -> None:
    """Per ADR 0014: a newline in a plaintext env value would corrupt
    the SSH SetEnv argument shape. Catch it at load time so the operator
    sees a clear message instead of an opaque SSH-side rejection."""
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        extras='\n[admin.env]\nMULTILINE = "line1\\nline2"\n',
    )
    cfg = load_config(cfg_file, warn_issues=False)
    assert any(
        "MULTILINE" in issue and "newline" in issue
        for issue in cfg.config_issues
    ), cfg.config_issues


def test_session_template_inherits_parent_env(tmp_path: Path) -> None:
    """A child session template with no env of its own inherits the parent's env
    unchanged. Pins None-vs-empty handling in the merge."""
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        extras="""
        [session_templates.parent.env]
        EDITOR = "nvim"

        [session_templates.child]
        inherits = ["parent"]
        """,
    )
    cfg = load_config(cfg_file, warn_issues=False)
    # Resolve the child template through the inheritance chain.
    from agentworks.sessions.templates import resolve_from_dict

    resolved = resolve_from_dict(cfg.session_templates, "child")
    assert resolved.env["EDITOR"].value == "nvim"


def test_session_template_required_commands_parsed(tmp_path: Path) -> None:
    """``required_commands`` parses into a list of strings on the template."""
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        extras="""
        [session_templates.claude]
        command = "claude --name {{session_name}}"
        required_commands = ["claude"]
        """,
    )
    cfg = load_config(cfg_file, warn_issues=False)
    assert cfg.session_templates["claude"].required_commands == ["claude"]


def test_session_template_required_commands_must_be_list(tmp_path: Path) -> None:
    """A non-list ``required_commands`` is rejected at load time."""
    from agentworks.errors import ConfigError

    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        extras="""
        [session_templates.claude]
        command = "claude"
        required_commands = "claude"
        """,
    )
    with pytest.raises(ConfigError, match="required_commands must be a list"):
        load_config(cfg_file, warn_issues=False)


def test_session_template_required_commands_must_be_strings(tmp_path: Path) -> None:
    """Non-string elements (e.g. ints) are rejected at load time -- not
    silently coerced via ``str()``. Pinning the type-strict behavior so a
    future refactor that drops the ``_require_string_list`` helper would
    surface here."""
    from agentworks.errors import ConfigError

    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        extras="""
        [session_templates.claude]
        command = "claude"
        required_commands = [123]
        """,
    )
    with pytest.raises(ConfigError, match="required_commands must be a list of strings"):
        load_config(cfg_file, warn_issues=False)


def test_session_template_required_commands_union_on_inherit(tmp_path: Path) -> None:
    """``required_commands`` is unioned (parents + child, de-duplicated) across
    the inheritance chain, matching the merge semantics of other list fields."""
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        extras="""
        [session_templates.parent]
        required_commands = ["tmux", "claude"]

        [session_templates.child]
        inherits = ["parent"]
        required_commands = ["claude", "jq"]
        """,
    )
    cfg = load_config(cfg_file, warn_issues=False)
    from agentworks.sessions.templates import resolve_from_dict

    resolved = resolve_from_dict(cfg.session_templates, "child")
    assert resolved.required_commands == ["tmux", "claude", "jq"]


def test_undeclared_secret_in_parent_no_longer_errors_at_load(
    tmp_path: Path,
) -> None:
    """Phase 1b: a parent template's env secret-ref to an undeclared name
    no longer errors at config load. The Registry's auto-declare miss
    policy handles it at finalize regardless of whether a child template
    overrides the key with plaintext. The override semantics still apply
    at resolution time -- if the child overrides with a literal, the
    parent's secret-ref doesn't actually need resolution -- but that's a
    runtime concern, not a config-load concern.
    """
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        extras="""
        [agent_templates.parent.env]
        TOKEN = { secret = "missing-secret" }

        [agent_templates.child]
        inherits = ["parent"]

        [agent_templates.child.env]
        TOKEN = "literal-value"

        [secret_config]
        backends = ["env-var", "prompt"]
        """,
    )
    # No longer raises.
    cfg = load_config(cfg_file, warn_issues=False)
    assert cfg.agent_templates["parent"].env["TOKEN"].secret == "missing-secret"
    assert cfg.agent_templates["child"].env["TOKEN"].value == "literal-value"
