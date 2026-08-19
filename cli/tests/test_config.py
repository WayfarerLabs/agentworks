"""Tests for config loading and validation."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent
from typing import Any

import pytest

from agentworks.bootstrap import build_registry
from agentworks.config import ConfigError, load_config, load_database_config
from agentworks.manifests import RESOURCES_DIRNAME, load_manifests
from tests.conftest import ManifestDoc, write_manifests


def _manifest_issues(config_file: Path) -> tuple[str, ...]:
    """The spec-level warnings the resources/ manifests raise (nonconforming
    secret names, unknown keys). config.toml is settings only now (ADR 0022),
    so these ride the ManifestSet, not cfg.config_issues."""
    return load_manifests(config_file.parent / RESOURCES_DIRNAME).issues


@pytest.fixture()
def config_dir(tmp_path: Path) -> Path:
    config_file = tmp_path / "config.toml"
    # Create fake SSH keys
    pub = tmp_path / "id_ed25519.pub"
    priv = tmp_path / "id_ed25519"
    pub.write_text("ssh-ed25519 AAAA...")
    priv.write_text("-----BEGIN OPENSSH PRIVATE KEY-----")

    # config.toml is settings only now (ADR 0022): the templates, credentials,
    # and install-command that used to live here are resources/ manifests.
    config_file.write_text(
        dedent(f"""\
        [operator]
        ssh_public_key = "{pub.as_posix()}"
        ssh_private_key = "{priv.as_posix()}"

        [defaults]
    """)
    )
    write_manifests(
        tmp_path,
        ManifestDoc("vm-template", "default", {"apt": ["zsh", "tmux"]}),
        ManifestDoc(
            "admin-template",
            "default",
            {"shell": "zsh", "git_credentials": ["github"], "user_install_commands": ["hello"]},
        ),
        ManifestDoc("user-install-command", "hello", {"command": "echo hello", "path": ["~/.local/bin"]}),
        ManifestDoc("workspace-template", "default"),
        ManifestDoc("workspace-template", "gruntweave", {"repo": "https://example.com/org/repo.git"}),
        ManifestDoc("workspace-template", "child", {"inherits": ["gruntweave"], "tmuxinator": False}),
        ManifestDoc("git-credential", "github", {"provider": {"name": "github"}}),
        ManifestDoc("git-credential", "azdo", {"provider": {"name": "azdo", "org": "my-org"}}),
    )
    return config_file


def test_load_valid_config(config_dir: Path) -> None:
    registry = build_registry(load_config(config_dir))
    admin = registry.lookup("admin-template", "default")
    assert admin.shell == "zsh"
    assert registry.lookup("vm-template", "default").apt == ["zsh", "tmux"]
    assert admin.user_install_commands == ["hello"]
    assert registry.lookup("user-install-command", "hello").command == "echo hello"
    assert registry.lookup("workspace-template", "default").name == "default"
    assert registry.lookup("workspace-template", "gruntweave").repo == "https://example.com/org/repo.git"
    assert registry.lookup("workspace-template", "child").inherits == ["gruntweave"]
    assert registry.lookup("workspace-template", "child").tmuxinator is False
    assert registry.lookup("git-credential", "github").provider.name == "github"
    assert registry.lookup("git-credential", "azdo").provider.config == {"org": "my-org"}
    assert admin.git_credentials == ["github"]
    assert load_config(config_dir).database.auto_backup_before_migration is True


def test_database_config_is_strict_and_focused(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[database]\nauto_backup_before_migration = false\n[operator]\nunrelated_invalid_shape = 17\n"
    )

    assert load_database_config(config_path).auto_backup_before_migration is False


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("database = false\n", "must be a table"),
        ("[database]\nauto_backup_before_migration = 1\n", "must be a boolean"),
        ("[database]\nunknown = true\n", "unexpected"),
        ("[database\n", "invalid config"),
    ],
)
def test_focused_database_config_rejects_unsafe_input(tmp_path: Path, text: str, match: str) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(text)

    with pytest.raises(ConfigError, match=match):
        load_database_config(config_path)


def test_focused_database_config_absent_file_uses_safe_default(tmp_path: Path) -> None:
    assert load_database_config(tmp_path / "absent.toml").auto_backup_before_migration is True


def test_missing_config_file(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        load_config(tmp_path / "nonexistent.toml")


def test_cycle_detection(tmp_path: Path) -> None:
    """Workspace template inheritance cycles are caught by the
    framework's cycle detector at build_registry time (Phase 2a.2).
    The bespoke load-time pass is gone; load_config no longer does
    inherits validation for any template kind.
    """
    pub = tmp_path / "id.pub"
    priv = tmp_path / "id"
    pub.write_text("key")
    priv.write_text("key")

    config_file = tmp_path / "config.toml"
    config_file.write_text(
        dedent(f"""\
        [operator]
        ssh_public_key = "{pub.as_posix()}"
        ssh_private_key = "{priv.as_posix()}"
    """)
    )
    write_manifests(
        tmp_path,
        ManifestDoc("workspace-template", "a", {"inherits": ["b"]}),
        ManifestDoc("workspace-template", "b", {"inherits": ["a"]}),
    )
    cfg = load_config(config_file)
    with pytest.raises(ConfigError, match="cycle"):
        build_registry(cfg)


def test_invalid_git_credential_type(tmp_path: Path) -> None:
    """Phase 2b.1: ``type`` validation moved to the framework. An
    unknown provider type errors at ``build_registry`` time via
    GitCredentialProviderKind's error miss policy.
    """
    pub = tmp_path / "id.pub"
    priv = tmp_path / "id"
    pub.write_text("key")
    priv.write_text("key")

    config_file = tmp_path / "config.toml"
    config_file.write_text(
        dedent(f"""\
        [operator]
        ssh_public_key = "{pub.as_posix()}"
        ssh_private_key = "{priv.as_posix()}"
    """)
    )
    # An UNKNOWN provider (not a legacy ``type`` key, which manifests reject
    # outright): the provider-kind miss policy still fires at build_registry.
    write_manifests(tmp_path, ManifestDoc("git-credential", "bad", {"provider": {"name": "gitlab"}}))
    cfg = load_config(config_file)
    with pytest.raises(ConfigError, match="git-credential-provider 'gitlab'"):
        build_registry(cfg)


# The former ``test_git_credential_type_still_accepted`` and
# ``test_git_credential_provider_wins_over_type`` were removed here: both pinned
# the flat-TOML loader's handling of the legacy ``type`` key (accepted as an
# alias; ``provider`` wins on disagreement). git-credential manifests reject a
# ``type`` key outright ('git-credential manifests use "provider", not "type"'),
# and config.toml no longer declares credentials at all (ADR 0022), so both
# behaviors are structurally gone.


def _git_credential_config(tmp_path: Path, *docs: ManifestDoc | str) -> Path:
    """Write a settings-only config.toml plus the given git-credential
    manifests and return the config path."""
    pub = tmp_path / "id.pub"
    priv = tmp_path / "id"
    pub.write_text("key")
    priv.write_text("key")

    config_file = tmp_path / "config.toml"
    config_file.write_text(
        dedent(f"""\
        [operator]
        ssh_public_key = "{pub.as_posix()}"
        ssh_private_key = "{priv.as_posix()}"
    """)
    )
    if docs:
        write_manifests(tmp_path, *docs)
    return config_file


def test_git_credential_provider_key(tmp_path: Path) -> None:
    """``provider`` is the going-forward vocabulary for the credential
    provider, matching secret-backend manifests."""
    config_file = _git_credential_config(
        tmp_path,
        ManifestDoc("git-credential", "gh", {"provider": {"name": "github"}}),
    )
    registry = build_registry(load_config(config_file))
    assert registry.lookup("git-credential", "gh").provider.name == "github"


def test_git_credential_nonconforming_name_warns_and_loads(tmp_path: Path) -> None:
    """A non-conforming credential name (uppercase) with no explicit token
    still loads and stays usable, but warns: its default token secret
    ``git-token-<name>`` inherits the non-conformance (issue #308). The
    derived-secret warning now rides the manifest issues channel."""
    config_file = _git_credential_config(
        tmp_path,
        ManifestDoc("git-credential", "GITHUB", {"provider": {"name": "github"}}),
    )
    # Warn-only, non-breaking: the credential is present and unchanged.
    registry = build_registry(load_config(config_file, warn_issues=False))
    assert registry.lookup("git-credential", "GITHUB").provider.name == "github"
    issues = _manifest_issues(config_file)
    assert any("git-credential/GITHUB" in issue and "git-token-GITHUB" in issue for issue in issues), issues


def test_git_credential_conforming_name_no_warning(tmp_path: Path) -> None:
    """A conforming credential name emits no derived-secret warning."""
    config_file = _git_credential_config(
        tmp_path,
        ManifestDoc("git-credential", "github", {"provider": {"name": "github"}}),
    )
    assert not any("does not follow the naming rules" in issue for issue in _manifest_issues(config_file))


def test_git_credential_nonconforming_name_with_explicit_token_no_derived_warning(
    tmp_path: Path,
) -> None:
    """When an explicit ``token`` is set, the credential name feeds no derived
    secret, so #308's derived-default warning does not fire (the explicit
    token value's own conformance is issue #279's concern, not this one)."""
    config_file = _git_credential_config(
        tmp_path,
        ManifestDoc("git-credential", "GITHUB", {"provider": {"name": "github", "token": "git-token-github"}}),
    )
    registry = build_registry(load_config(config_file, warn_issues=False))
    cred = registry.lookup("git-credential", "GITHUB")
    assert cred.provider.name == "github"
    assert cred.provider.config["token"] == "git-token-github"
    assert not any("does not follow the naming rules" in issue for issue in _manifest_issues(config_file))


def test_git_credential_name_deriving_secret_at_length_cap_no_warning(tmp_path: Path) -> None:
    """A credential name whose derived ``git-token-<name>`` lands exactly at
    MAX_SECRET_NAME_LENGTH emits no warning: the cap is inclusive (issue #308)."""
    from agentworks.naming import MAX_SECRET_NAME_LENGTH

    # 'git-token-' is 10 chars, so a name of (cap - 10) makes the derived
    # secret name exactly the cap.
    name = "a" * (MAX_SECRET_NAME_LENGTH - len("git-token-"))
    config_file = _git_credential_config(
        tmp_path,
        ManifestDoc("git-credential", name, {"provider": {"name": "github"}}),
    )
    assert not any("does not follow the naming rules" in issue for issue in _manifest_issues(config_file))


def test_git_credential_name_conforming_but_derived_over_cap_warns(tmp_path: Path) -> None:
    """The length-ceiling case: a credential name that passes the naming rules
    on its own, but whose derived ``git-token-<name>`` overflows
    MAX_SECRET_NAME_LENGTH once the prefix is added, still warns. Guards the
    subtle property that the fix validates the DERIVED string, not the bare
    name (issue #308): a future edit validating the bare name would pass this
    name and silently regress."""
    from agentworks.naming import MAX_SECRET_NAME_LENGTH, validate_name

    # One char past the cap once the 10-char 'git-token-' prefix is added.
    name = "a" * (MAX_SECRET_NAME_LENGTH - len("git-token-") + 1)
    # Precondition: the bare name is itself valid; only the derived name overflows.
    validate_name(name, max_length=MAX_SECRET_NAME_LENGTH)
    config_file = _git_credential_config(
        tmp_path,
        ManifestDoc("git-credential", name, {"provider": {"name": "github"}}),
    )
    issues = _manifest_issues(config_file)
    assert any(
        f"git-credential/{name}" in issue and "does not follow the secret naming rules" in issue for issue in issues
    ), issues


def test_unexpected_top_level_keys_fail(tmp_path: Path) -> None:
    """Bare keys before any section header land at top level."""
    pub = tmp_path / "id.pub"
    priv = tmp_path / "id"
    pub.write_text("key")
    priv.write_text("key")

    config_file = tmp_path / "config.toml"
    # 'oops' appears before any [section] header
    config_file.write_text(
        dedent(f"""\
        oops = true

        [operator]
        ssh_public_key = "{pub.as_posix()}"
        ssh_private_key = "{priv.as_posix()}"
    """)
    )
    with pytest.raises(ConfigError, match="unexpected top-level keys in config: oops"):
        load_config(config_file)


def test_dotfiles_keeps_its_removal_guidance(tmp_path: Path) -> None:
    pub = tmp_path / "id.pub"
    priv = tmp_path / "id"
    pub.write_text("key")
    priv.write_text("key")
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        dedent(f"""\
        [operator]
        ssh_public_key = "{pub.as_posix()}"
        ssh_private_key = "{priv.as_posix()}"

        [dotfiles]
        source = "example"
    """)
    )

    with pytest.raises(ConfigError, match=r"\[dotfiles\] section has been removed.*\[admin.config\]"):
        load_config(config_file)


def test_orphaned_key_under_commented_section(tmp_path: Path) -> None:
    """Keys under commented-out section headers are recorded as config issues."""
    pub = tmp_path / "id.pub"
    priv = tmp_path / "id"
    pub.write_text("key")
    priv.write_text("key")

    config_file = tmp_path / "config.toml"
    config_file.write_text(
        dedent(f"""\
        [operator]
        ssh_public_key = "{pub.as_posix()}"
        ssh_private_key = "{priv.as_posix()}"

        # [defaults]          <-- commented out!
        platform = "lima"     # orphaned in [operator], not [defaults]
    """)
    )
    cfg = load_config(config_file)
    assert any("platform" in issue for issue in cfg.config_issues)
    assert any("operator" in issue for issue in cfg.config_issues)


def test_extra_ssh_public_keys(tmp_path: Path) -> None:
    pub = tmp_path / "id.pub"
    priv = tmp_path / "id"
    extra1 = tmp_path / "extra1.pub"
    extra2 = tmp_path / "extra2.pub"
    pub.write_text("ssh-ed25519 AAAA-primary")
    priv.write_text("key")
    extra1.write_text("ssh-ed25519 AAAA-extra1")
    extra2.write_text("ssh-rsa BBBB-extra2")

    config_file = tmp_path / "config.toml"
    config_file.write_text(
        dedent(f"""\
        [operator]
        ssh_public_key = "{pub.as_posix()}"
        ssh_private_key = "{priv.as_posix()}"
        extra_ssh_public_keys = ["{extra1.as_posix()}", "{extra2.as_posix()}"]
    """)
    )
    cfg = load_config(config_file)
    assert len(cfg.operator.extra_ssh_public_keys) == 2
    assert cfg.operator.extra_ssh_public_keys[0] == extra1
    assert cfg.operator.extra_ssh_public_keys[1] == extra2


def test_extra_ssh_public_keys_missing_file(tmp_path: Path) -> None:
    pub = tmp_path / "id.pub"
    priv = tmp_path / "id"
    pub.write_text("key")
    priv.write_text("key")

    config_file = tmp_path / "config.toml"
    config_file.write_text(
        dedent(f"""\
        [operator]
        ssh_public_key = "{pub.as_posix()}"
        ssh_private_key = "{priv.as_posix()}"
        extra_ssh_public_keys = ["/nonexistent/key.pub"]
    """)
    )
    with pytest.raises(ConfigError, match="extra_ssh_public_keys.*does not exist"):
        load_config(config_file)


def test_workload_gated_issues_fatal_false_softens_missing_keys_to_issues(tmp_path: Path) -> None:
    """``workload_gated_issues_fatal=False`` (the flag read-only inspection
    commands pass) turns a missing primary or extra key file into a
    ``config_issues`` entry instead of aborting the load, while every
    other operator field still loads normally."""
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        dedent(f"""\
        [operator]
        ssh_public_key = "{(tmp_path / "id.pub").as_posix()}"
        ssh_private_key = "{(tmp_path / "id").as_posix()}"
        extra_ssh_public_keys = ["{(tmp_path / "extra.pub").as_posix()}"]
    """)
    )
    cfg = load_config(config_file, warn_issues=False, workload_gated_issues_fatal=False)
    assert any("ssh_public_key does not exist" in issue for issue in cfg.config_issues)
    assert any("ssh_private_key does not exist" in issue for issue in cfg.config_issues)
    assert any("extra_ssh_public_keys" in issue and "does not exist" in issue for issue in cfg.config_issues)
    assert cfg.operator.ssh_public_key == (tmp_path / "id.pub")
    assert cfg.operator.ssh_private_key == (tmp_path / "id")


def test_workload_gated_issues_fatal_true_still_raises_by_default(tmp_path: Path) -> None:
    """The default stays strict: a caller that does not pass
    ``workload_gated_issues_fatal=False`` gets today's hard failure,
    exactly as every mutation/provisioning command relies on."""
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        dedent(f"""\
        [operator]
        ssh_public_key = "{(tmp_path / "id.pub").as_posix()}"
        ssh_private_key = "{(tmp_path / "id").as_posix()}"
    """)
    )
    with pytest.raises(ConfigError, match="ssh_public_key does not exist"):
        load_config(config_file)


def test_extra_ssh_public_keys_defaults_empty(config_dir: Path) -> None:
    cfg = load_config(config_dir)
    assert cfg.operator.extra_ssh_public_keys == []


def test_ssh_allow_cidrs_normalized(tmp_path: Path) -> None:
    """Valid entries load normalized: a bare IP becomes its /32, a CIDR
    with host bits set collapses to its network."""
    pub = tmp_path / "id.pub"
    priv = tmp_path / "id"
    pub.write_text("key")
    priv.write_text("key")

    config_file = tmp_path / "config.toml"
    config_file.write_text(
        dedent(f"""\
        [operator]
        ssh_public_key = "{pub.as_posix()}"
        ssh_private_key = "{priv.as_posix()}"
        ssh_allow_cidrs = ["203.0.113.7", "198.51.100.0/24", "10.0.0.1/16"]
    """)
    )
    cfg = load_config(config_file)
    assert cfg.operator.ssh_allow_cidrs == ["203.0.113.7/32", "198.51.100.0/24", "10.0.0.0/16"]


def test_ssh_allow_cidrs_invalid_entry_rejected(tmp_path: Path) -> None:
    """A garbage entry fails at config load with a typed error naming the
    setting and the offending value, not at the first vm op."""
    pub = tmp_path / "id.pub"
    priv = tmp_path / "id"
    pub.write_text("key")
    priv.write_text("key")

    config_file = tmp_path / "config.toml"
    config_file.write_text(
        dedent(f"""\
        [operator]
        ssh_public_key = "{pub.as_posix()}"
        ssh_private_key = "{priv.as_posix()}"
        ssh_allow_cidrs = ["not-an-ip"]
    """)
    )
    with pytest.raises(ConfigError, match="ssh_allow_cidrs.*'not-an-ip'"):
        load_config(config_file)


def test_ssh_allow_cidrs_scalar_rejected(tmp_path: Path) -> None:
    """A scalar value (a bare string would otherwise iterate per
    character) is a typed error naming the setting, not a TypeError or a
    per-character parse failure."""
    pub = tmp_path / "id.pub"
    priv = tmp_path / "id"
    pub.write_text("key")
    priv.write_text("key")

    config_file = tmp_path / "config.toml"
    config_file.write_text(
        dedent(f"""\
        [operator]
        ssh_public_key = "{pub.as_posix()}"
        ssh_private_key = "{priv.as_posix()}"
        ssh_allow_cidrs = "203.0.113.7"
    """)
    )
    with pytest.raises(ConfigError, match="ssh_allow_cidrs must be a list"):
        load_config(config_file)


def test_ssh_allow_cidrs_defaults_empty(config_dir: Path) -> None:
    cfg = load_config(config_dir)
    assert cfg.operator.ssh_allow_cidrs == []


# -- proxmox vm-site tests (table-driven) -------------------------------------
# The proxmox site is a vm-site/proxmox manifest whose platform_config carries
# the connection fields; the proxmox platform capability validates the assembled
# blob at build_registry (the finalize ``validate`` pass, R3).

_PROXMOX_TEST_CASES: list[dict[str, Any]] = [
    {
        "id": "valid_all_fields",
        "platform_config": {
            "api_url": "https://pve.example.com:8006",
            "node": "pve",
            "token_id": "agentworks@pam!agentworks",
            "template_vmid": 9000,
            "storage": "zfs-pool",
            "bridge": "vmbr1",
            "verify_ssl": False,
        },
        "expect_error": None,
        "check": lambda registry: (
            registry.lookup("vm-site", "proxmox").platform.name == "proxmox"
            and registry.lookup("vm-site", "proxmox").platform.config
            == {
                "api_url": "https://pve.example.com:8006",
                "node": "pve",
                "token_id": "agentworks@pam!agentworks",
                "template_vmid": 9000,
                "storage": "zfs-pool",
                "bridge": "vmbr1",
                "verify_ssl": False,
            }
        ),
    },
    {
        "id": "valid_defaults",
        "platform_config": {
            "api_url": "https://pve.local:8006",
            "node": "node1",
            "token_id": "root@pam!test",
            "template_vmid": 100,
        },
        "expect_error": None,
        # Optional keys are absent from the blob when omitted; the
        # platform applies its own defaults (storage local-lvm, bridge
        # vmbr0, verify_ssl True) at use.
        "check": lambda registry: (
            "storage" not in registry.lookup("vm-site", "proxmox").platform.config
            and "bridge" not in registry.lookup("vm-site", "proxmox").platform.config
            and "verify_ssl" not in registry.lookup("vm-site", "proxmox").platform.config
        ),
    },
    {
        "id": "missing_api_url",
        "platform_config": {"node": "pve", "token_id": "u@p!t", "template_vmid": 9000},
        "expect_error": r"vm-site/proxmox\.api_url: is required",
        "check": None,
    },
    {
        "id": "missing_node",
        "platform_config": {"api_url": "https://pve:8006", "token_id": "u@p!t", "template_vmid": 9000},
        "expect_error": r"vm-site/proxmox\.node: is required",
        "check": None,
    },
    {
        "id": "missing_token_id",
        "platform_config": {"api_url": "https://pve:8006", "node": "pve", "template_vmid": 9000},
        "expect_error": r"vm-site/proxmox\.token_id: is required",
        "check": None,
    },
    {
        "id": "missing_template_vmid",
        "platform_config": {"api_url": "https://pve:8006", "node": "pve", "token_id": "u@p!t"},
        "expect_error": r"vm-site/proxmox\.template_vmid: is required",
        "check": None,
    },
]


@pytest.mark.parametrize(
    "case",
    _PROXMOX_TEST_CASES,
    ids=[c["id"] for c in _PROXMOX_TEST_CASES],
)
def test_proxmox_config(tmp_path: Path, case: dict) -> None:
    pub = tmp_path / "id.pub"
    priv = tmp_path / "id"
    pub.write_text("key")
    priv.write_text("key")

    # Proxmox ships in the opt-in ``proxmox`` system plugin (Phase 10, R11);
    # enable it so the proxmox site's platform_config validation runs (a
    # disabled platform's site is not-ready and skips field validation, so
    # the missing-field ConfigError would never fire).
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        dedent(f"""\
        [operator]
        ssh_public_key = "{pub.as_posix()}"
        ssh_private_key = "{priv.as_posix()}"

        [plugins]
        system = ["proxmox"]
    """)
    )
    write_manifests(
        tmp_path,
        ManifestDoc("vm-site", "proxmox", {"platform": {"name": "proxmox", **case["platform_config"]}}),
    )
    config = load_config(config_file)

    if case["expect_error"]:
        # The platform_config blob's shape check runs in the finalize
        # ``validate`` pass (R3): a malformed manifest decodes fine and
        # fails at build_registry, framed by the vm-site name with the
        # source location re-attached.
        with pytest.raises(ConfigError, match=case["expect_error"]):
            build_registry(config)
    else:
        registry = build_registry(config)
        assert case["check"](registry), f"Check failed for {case['id']}"


def test_proxmox_section_absent(config_dir: Path) -> None:
    registry = build_registry(load_config(config_dir))
    with pytest.raises(KeyError):
        registry.lookup("vm-site", "proxmox")


def test_user_section_is_rejected(tmp_path: Path) -> None:
    pub = tmp_path / "id.pub"
    priv = tmp_path / "id"
    pub.write_text("key")
    priv.write_text("key")

    config_file = tmp_path / "config.toml"
    config_file.write_text(
        dedent(f"""\
        [user]
        ssh_public_key = "{pub.as_posix()}"
        ssh_private_key = "{priv.as_posix()}"
    """)
    )

    with pytest.raises(ConfigError, match="unexpected top-level keys in config: user"):
        load_config(config_file)


def test_code_workspaces_fails_before_default_vscode_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A retired path key cannot fall through to the default output directory."""
    from typer.testing import CliRunner

    from agentworks.cli import app

    home = tmp_path / "home"
    home.mkdir()
    pub = tmp_path / "id.pub"
    priv = tmp_path / "id"
    pub.write_text("key")
    priv.write_text("key")
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        dedent(f"""\
        [operator]
        ssh_public_key = "{pub.as_posix()}"
        ssh_private_key = "{priv.as_posix()}"

        [paths]
        code_workspaces = "{tmp_path / "retired"}"
    """)
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", config_file)
    monkeypatch.setattr(
        "agentworks.cli.commands.config.get_db",
        lambda: pytest.fail("database access must happen after config validation"),
    )

    result = CliRunner().invoke(app, ["config", "sync-vscode-workspaces"])

    assert result.exit_code != 0
    assert isinstance(result.exception, ConfigError)
    assert "unexpected keys in [paths]: code_workspaces" in str(result.exception)
    assert not (home / "aw-vscode-workspaces").exists()


# -- Claude plugin config validation ----------------------------------------


def _minimal_config(tmp_path: Path) -> Path:
    """Write a minimal, settings-only config.toml (resources go in manifests)."""
    pub = tmp_path / "id.pub"
    priv = tmp_path / "id"
    pub.write_text("key")
    priv.write_text("key")
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        dedent(f"""\
        [operator]
        ssh_public_key = "{pub.as_posix()}"
        ssh_private_key = "{priv.as_posix()}"
    """)
    )
    return config_file


def test_claude_marketplaces_loads_cleanly(tmp_path: Path) -> None:
    config_file = _minimal_config(tmp_path)
    write_manifests(
        tmp_path,
        ManifestDoc(
            "admin-template",
            "default",
            {
                "claude_marketplaces": ["https://github.com/example/tools#v1"],
                "claude_plugins": ["my-plugin@my-marketplace"],
            },
        ),
    )
    registry = build_registry(load_config(config_file, warn_issues=False))
    admin = registry.lookup("admin-template", "default")
    assert admin.claude_marketplaces == ["https://github.com/example/tools#v1"]
    assert admin.claude_plugins == ["my-plugin@my-marketplace"]


def test_claude_marketplaces_agent_template(tmp_path: Path) -> None:
    config_file = _minimal_config(tmp_path)
    write_manifests(
        tmp_path,
        ManifestDoc(
            "agent-template",
            "claude",
            {
                "claude_marketplaces": ["https://github.com/example/tools#v1"],
                "claude_plugins": ["my-plugin@my-marketplace"],
            },
        ),
    )
    registry = build_registry(load_config(config_file, warn_issues=False))
    agent = registry.lookup("agent-template", "claude")
    assert agent.claude_marketplaces == ["https://github.com/example/tools#v1"]
    assert agent.claude_plugins == ["my-plugin@my-marketplace"]
    assert not _manifest_issues(config_file)


def test_claude_marketplaces_rejects_string(tmp_path: Path) -> None:
    config_file = _minimal_config(tmp_path)
    write_manifests(
        tmp_path,
        ManifestDoc("admin-template", "default", {"claude_marketplaces": "https://github.com/example/tools"}),
    )
    with pytest.raises(ConfigError, match=r"claude_marketplaces: must be a list"):
        build_registry(load_config(config_file, warn_issues=False))


def test_description_stored_for_template_kinds(tmp_path: Path) -> None:
    """description is framework-uniform: every declarable kind's manifest
    stores it onto the loaded dataclass (from metadata.description), with no
    warnings."""
    config_file = _minimal_config(tmp_path)
    write_manifests(
        tmp_path,
        ManifestDoc("vm-template", "dev", description="the dev box"),
        ManifestDoc("agent-template", "dev", description="the dev agent"),
        ManifestDoc("workspace-template", "proj", description="the proj workspace"),
        ManifestDoc("admin-template", "default", description="the admin user"),
        ManifestDoc("named-console-template", "default", description="the default console"),
    )
    registry = build_registry(load_config(config_file, warn_issues=False))
    assert not _manifest_issues(config_file)
    assert registry.lookup("vm-template", "dev").description == "the dev box"
    assert registry.lookup("agent-template", "dev").description == "the dev agent"
    assert registry.lookup("workspace-template", "proj").description == "the proj workspace"
    assert registry.lookup("admin-template", "default").description == "the admin user"
    assert registry.lookup("named-console-template", "default").description == "the default console"


# -- [named_console] section ------------------------------------------------


def test_named_console_tmux_layout_default_when_section_missing(tmp_path: Path) -> None:
    """No [named_console] section: the loader publishes nothing and the
    framework auto-declares the default, whose tmux_layout is the
    aw-session-vertical default the Named Console feature was designed
    around (one privileged session pane on top, helper shells under)."""
    config_file = _minimal_config(tmp_path)
    # No named-console-template manifest: the framework auto-declares the
    # default at finalize.
    nc = build_registry(load_config(config_file)).lookup("named-console-template", "default")
    assert nc.tmux_layout == "aw-session-vertical"


@pytest.mark.parametrize(
    "layout",
    [
        "tiled",
        "even-vertical",
        "even-horizontal",
        "main-vertical",
        "main-horizontal",
        "aw-session-vertical",
    ],
)
def test_named_console_tmux_layout_accepts_valid_presets(tmp_path: Path, layout: str) -> None:
    """All five tmux preset layout names plus the agentworks-specific
    `aw-session-vertical` are accepted verbatim."""
    config_file = _minimal_config(tmp_path)
    write_manifests(tmp_path, ManifestDoc("named-console-template", "default", {"tmux_layout": layout}))
    registry = build_registry(load_config(config_file))
    assert registry.lookup("named-console-template", "default").tmux_layout == layout


def test_named_console_tmux_layout_rejects_unknown(tmp_path: Path) -> None:
    """Unknown layout names fail at build with a list of valid alternatives
    (the decoder's spec-level error surfaces as ConfigError at build_registry)."""
    config_file = _minimal_config(tmp_path)
    write_manifests(tmp_path, ManifestDoc("named-console-template", "default", {"tmux_layout": "tabbed"}))
    with pytest.raises(ConfigError, match="tmux_layout: must be one of"):
        build_registry(load_config(config_file))


def test_named_console_section_unexpected_keys_are_refused(tmp_path: Path) -> None:
    """FR12's warn-to-error flip: an unknown key on the
    named-console-template surface is a hard error naming the fields that
    ARE valid, not a warning beside a config that loaded anyway."""
    config_file = _minimal_config(tmp_path)
    write_manifests(
        tmp_path,
        ManifestDoc("named-console-template", "default", {"tmux_layout": "tiled", "unknown_key": "x"}),
    )
    with pytest.raises(ConfigError, match="unknown_key: unknown field; expected one of: tmux_layout"):
        _manifest_issues(config_file)
