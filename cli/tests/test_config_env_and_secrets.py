"""Config loader tests for the env / secrets surface added in Phase 2 of the
env-and-secrets effort.

These cover:

- env tables on AdminConfig / VMTemplate / WorkspaceTemplate / AgentTemplate /
  SessionTemplate parse into ``dict[str, EnvEntry]`` (plaintext + secret-ref shapes).
- env key validation (regex; rejects invalid names).
- AGENTWORKS_* override emits a load-time warning.
- [secrets.*] parses into SecretDecls including all backend_mappings value forms
  (string, dict, false). ``true`` is rejected.
- [secret_config].backends drives resolver assembly; precedence preserved.
- Unknown backend kinds in [secret_config].backends raise ConfigError.
- Unreachable secrets raise ConfigError at load time.
- Env entries referencing undeclared secrets raise ConfigError.
- Mid-config without any [secrets] / [secret_config] still loads cleanly with
  ``secret_resolver is None``.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from agentworks.config import ConfigError, load_config


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


def test_no_secrets_section_loads_with_empty_resolver(tmp_path: Path) -> None:
    """When no secrets / backends are configured, the resolver is an empty
    SecretResolver rather than None: call sites can render env unconditionally."""
    cfg_file = tmp_path / "config.toml"
    _write_base(cfg_file)
    cfg = load_config(cfg_file, warn_issues=False)
    assert cfg.secrets == {}
    assert cfg.secret_backends == {}
    assert cfg.secret_config_data.backends == ()
    assert cfg.secret_resolver is not None
    # An empty resolver renders an env with no entries to {} without raising.
    assert cfg.secret_resolver.render({}) == {}


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
        backends = ["env_var", "prompt"]
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
    assert cfg.vm.env["EDITOR"].value == "nvim"


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
        backends = ["env_var", "prompt"]
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
        backends = ["env_var", "prompt"]
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


def test_env_referencing_undeclared_secret_raises(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        extras="""
        [admin.env]
        API_KEY = { secret = "missing" }

        [secret_config]
        backends = ["env_var"]
        """,
    )
    with pytest.raises(ConfigError, match="undeclared secret"):
        load_config(cfg_file, warn_issues=False)


def test_secret_declared_with_all_mapping_forms(tmp_path: Path) -> None:
    """All three backend_mappings value shapes (string, inline table, false) parse
    onto SecretDecl. The chain uses prompt-only so even token-c (which opts out
    of env_var) and token-b (mapping for a future backend) stay reachable through
    PromptSource."""
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        extras="""
        [secrets.token-a]
        description = "string mapping"
        backend_mappings.env_var = "OVERRIDE_NAME"

        [secrets.token-b]
        description = "structured mapping (for future backend)"
        backend_mappings.onepassword = { vault = "Shared", item = "Tok", field = "key" }

        [secrets.token-c]
        description = "opt-out mapping"
        backend_mappings.env_var = false

        [secret_config]
        backends = ["prompt"]
        """,
    )
    cfg = load_config(cfg_file, warn_issues=False)
    assert cfg.secrets["token-a"].backend_mappings == {"env_var": "OVERRIDE_NAME"}
    assert cfg.secrets["token-b"].backend_mappings == {
        "onepassword": {"vault": "Shared", "item": "Tok", "field": "key"}
    }
    assert cfg.secrets["token-c"].backend_mappings == {"env_var": False}


def test_secret_true_in_backend_mappings_rejected(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        extras="""
        [secrets.token]
        description = "bad"
        backend_mappings.env_var = true

        [secret_config]
        backends = ["env_var"]
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
        backends = ["env_var", "prompt"]
        """,
    )
    cfg = load_config(cfg_file, warn_issues=False)
    assert cfg.secret_config_data.backends == ("env_var", "prompt")


def test_secret_resolver_assembled_when_backends_configured(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        extras="""
        [secrets.shared]
        description = "Shared token"

        [secret_config]
        backends = ["env_var", "prompt"]
        """,
    )
    cfg = load_config(cfg_file, warn_issues=False)
    assert cfg.secret_resolver is not None
    # Smoke-check the chain by asking for the first attempting source.
    first = cfg.secret_resolver.first_attempting_source(cfg.secrets["shared"])
    assert first is not None
    assert first.kind == "env_var"


def test_unknown_backend_kind_raises(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        extras="""
        [secret_config]
        backends = ["env_var", "totally-fake-backend"]
        """,
    )
    with pytest.raises(ConfigError, match="totally-fake-backend"):
        load_config(cfg_file, warn_issues=False)


def test_unreachable_secret_raises(tmp_path: Path) -> None:
    """A secret with env_var = false and a backend chain with no other attempting
    source is unreachable; the loader rejects this at load time."""
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        extras="""
        [secrets.stranded]
        description = "no path to resolution"
        backend_mappings.env_var = false

        [secret_config]
        backends = ["env_var"]
        """,
    )
    with pytest.raises(ConfigError, match="unreachable"):
        load_config(cfg_file, warn_issues=False)


def test_unknown_backend_kind_in_secret_backends_emits_warning(
    tmp_path: Path,
) -> None:
    """A typo in [secret_backends.<kind>] (e.g. 'env-var' for 'env_var') surfaces
    at load time as a warning, not at reach-for time in [secret_config].backends."""
    cfg_file = tmp_path / "config.toml"
    _write_base(
        cfg_file,
        extras="""
        [secret_backends.env-var]
        # typo: should be env_var
        """,
    )
    cfg = load_config(cfg_file, warn_issues=False)
    assert any(
        "env-var" in issue and "unknown backend kind" in issue
        for issue in cfg.config_issues
    ), cfg.config_issues


@pytest.mark.parametrize(
    ("scope_extras", "context_label"),
    [
        ("[vm_templates.default.env]\nAGENTWORKS_VM = \"override\"", "vm_templates.default.env"),
        ("[admin.env]\nAGENTWORKS_USER = \"override\"", "admin.env"),
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


def test_undeclared_secret_in_parent_caught_even_if_child_overrides(
    tmp_path: Path,
) -> None:
    """A parent template with a secret-ref pointing at an undeclared secret is
    rejected at load time even when a child template overrides that key with
    plaintext. _validate_env_secret_refs walks every template independently."""
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
        backends = ["env_var", "prompt"]
        """,
    )
    with pytest.raises(ConfigError, match="missing-secret"):
        load_config(cfg_file, warn_issues=False)
