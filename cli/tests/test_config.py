"""Tests for config loading and validation."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from agentworks.config import ConfigError, load_config


@pytest.fixture()
def config_dir(tmp_path: Path) -> Path:
    config_file = tmp_path / "config.toml"
    # Create fake SSH keys
    pub = tmp_path / "id_ed25519.pub"
    priv = tmp_path / "id_ed25519"
    pub.write_text("ssh-ed25519 AAAA...")
    priv.write_text("-----BEGIN OPENSSH PRIVATE KEY-----")

    config_file.write_text(
        dedent(f"""\
        [operator]
        ssh_public_key = "{pub.as_posix()}"
        ssh_private_key = "{priv.as_posix()}"

        [vm_templates.default]
        apt = ["zsh", "tmux"]

        [admin.config]
        shell = "zsh"
        git_credentials = ["github"]
        user_install_commands = ["hello"]

        [user_install_commands.hello]
        command = "echo hello"
        path = ["~/.local/bin"]

        [workspace_templates.default]

        [workspace_templates.gruntweave]
        repo = "https://example.com/org/repo.git"

        [workspace_templates.child]
        inherits = ["gruntweave"]
        tmuxinator = false

        [git_credentials.github]
        type = "github"

        [git_credentials.azdo]
        type = "azdo"
        org = "my-org"

        [defaults]
    """)
    )
    return config_file


def test_load_valid_config(config_dir: Path) -> None:
    cfg = load_config(config_dir)
    assert cfg.admin.shell == "zsh"
    assert cfg.vm.apt == ["zsh", "tmux"]
    assert cfg.admin.user_install_commands == ["hello"]
    assert "hello" in cfg.user_install_commands
    assert "default" in cfg.workspace_templates
    assert "gruntweave" in cfg.workspace_templates
    assert cfg.workspace_templates["child"].inherits == ["gruntweave"]
    assert cfg.workspace_templates["child"].tmuxinator is False
    assert cfg.git_credentials["github"].type == "github"
    assert cfg.git_credentials["azdo"].org == "my-org"
    assert cfg.admin.git_credentials == ["github"]


def test_missing_config_file(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        load_config(tmp_path / "nonexistent.toml")


def test_cycle_detection(tmp_path: Path) -> None:
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

        [workspace_templates.a]
        inherits = ["b"]

        [workspace_templates.b]
        inherits = ["a"]
    """)
    )
    with pytest.raises(ConfigError, match="cycle"):
        load_config(config_file)


def test_invalid_git_credential_type(tmp_path: Path) -> None:
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

        [git_credentials.bad]
        type = "gitlab"
    """)
    )
    with pytest.raises(ConfigError, match="git_credentials.bad.type"):
        load_config(config_file)


def test_unexpected_top_level_keys_warns(tmp_path: Path) -> None:
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
    cfg = load_config(config_file)
    assert any("oops" in issue for issue in cfg.config_issues)


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


def test_extra_ssh_public_keys_defaults_empty(config_dir: Path) -> None:
    cfg = load_config(config_dir)
    assert cfg.operator.extra_ssh_public_keys == []


# -- Proxmox config tests (table-driven) --------------------------------------

_PROXMOX_TEST_CASES = [
    {
        "id": "valid_all_fields",
        "toml": """\
            [proxmox]
            api_url = "https://pve.example.com:8006"
            node = "pve"
            token_id = "agentworks@pam!agentworks"
            template_vmid = 9000
            storage = "zfs-pool"
            bridge = "vmbr1"
            verify_ssl = false
        """,
        "expect_error": None,
        "check": lambda cfg: (
            cfg.proxmox.api_url == "https://pve.example.com:8006"
            and cfg.proxmox.node == "pve"
            and cfg.proxmox.token_id == "agentworks@pam!agentworks"
            and cfg.proxmox.template_vmid == 9000
            and cfg.proxmox.storage == "zfs-pool"
            and cfg.proxmox.bridge == "vmbr1"
            and cfg.proxmox.verify_ssl is False
        ),
    },
    {
        "id": "valid_defaults",
        "toml": """\
            [proxmox]
            api_url = "https://pve.local:8006"
            node = "node1"
            token_id = "root@pam!test"
            template_vmid = 100
        """,
        "expect_error": None,
        "check": lambda cfg: (
            cfg.proxmox.storage == "local-lvm"
            and cfg.proxmox.bridge == "vmbr0"
            and cfg.proxmox.verify_ssl is True
        ),
    },
    {
        "id": "missing_api_url",
        "toml": """\
            [proxmox]
            node = "pve"
            token_id = "u@p!t"
            template_vmid = 9000

        """,
        "expect_error": "proxmox.api_url is required",
        "check": None,
    },
    {
        "id": "missing_node",
        "toml": """\
            [proxmox]
            api_url = "https://pve:8006"
            token_id = "u@p!t"
            template_vmid = 9000

        """,
        "expect_error": "proxmox.node is required",
        "check": None,
    },
    {
        "id": "missing_token_id",
        "toml": """\
            [proxmox]
            api_url = "https://pve:8006"
            node = "pve"
            template_vmid = 9000

        """,
        "expect_error": "proxmox.token_id is required",
        "check": None,
    },
    {
        "id": "missing_template_vmid",
        "toml": """\
            [proxmox]
            api_url = "https://pve:8006"
            node = "pve"
            token_id = "u@p!t"

        """,
        "expect_error": "proxmox.template_vmid is required",
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

    config_file = tmp_path / "config.toml"
    config_file.write_text(dedent(f"""\
        [operator]
        ssh_public_key = "{pub.as_posix()}"
        ssh_private_key = "{priv.as_posix()}"

        {dedent(case["toml"])}
    """))

    if case["expect_error"]:
        with pytest.raises(ConfigError, match=case["expect_error"]):
            load_config(config_file)
    else:
        cfg = load_config(config_file)
        assert case["check"](cfg), f"Check failed for {case['id']}"


def test_proxmox_section_absent(config_dir: Path) -> None:
    cfg = load_config(config_dir)
    assert cfg.proxmox is None


def test_user_section_deprecated_alias(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """[user] section is accepted as a deprecated alias for [operator]."""
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

    cfg = load_config(config_file)
    assert cfg.operator.ssh_public_key == pub
    assert cfg.operator.ssh_private_key == priv
    captured = capsys.readouterr()
    assert "deprecated" in captured.err.lower()
    assert "[operator]" in captured.err


# -- Claude plugin config validation ----------------------------------------


def _minimal_config(tmp_path: Path, extra: str = "") -> Path:
    """Write a minimal valid config with optional extra sections."""
    pub = tmp_path / "id.pub"
    priv = tmp_path / "id"
    pub.write_text("key")
    priv.write_text("key")
    config_file = tmp_path / "config.toml"
    config_file.write_text(dedent(f"""\
        [operator]
        ssh_public_key = "{pub.as_posix()}"
        ssh_private_key = "{priv.as_posix()}"

        {dedent(extra)}
    """))
    return config_file


def test_claude_marketplaces_loads_cleanly(tmp_path: Path) -> None:
    config_file = _minimal_config(tmp_path, """
        [admin.config]
        claude_marketplaces = ["https://github.com/example/tools#v1"]
        claude_plugins = ["my-plugin@my-marketplace"]
    """)
    cfg = load_config(config_file, warn_issues=False)
    assert cfg.admin.claude_marketplaces == ["https://github.com/example/tools#v1"]
    assert cfg.admin.claude_plugins == ["my-plugin@my-marketplace"]


def test_claude_marketplaces_agent_template(tmp_path: Path) -> None:
    config_file = _minimal_config(tmp_path, """
        [agent_templates.claude]
        claude_marketplaces = ["https://github.com/example/tools#v1"]
        claude_plugins = ["my-plugin@my-marketplace"]
    """)
    cfg = load_config(config_file, warn_issues=False)
    assert cfg.agent_templates["claude"].claude_marketplaces == ["https://github.com/example/tools#v1"]
    assert cfg.agent_templates["claude"].claude_plugins == ["my-plugin@my-marketplace"]
    assert not cfg.config_issues


def test_claude_marketplaces_rejects_string(tmp_path: Path) -> None:
    config_file = _minimal_config(tmp_path, """
        [admin.config]
        claude_marketplaces = "https://github.com/example/tools"
    """)
    with pytest.raises(ConfigError, match="must be a list of strings"):
        load_config(config_file)


# -- [console] section -----------------------------------------------------


def test_console_default_layout_default_when_section_missing(tmp_path: Path) -> None:
    """No [console] section produces the documented tiled default."""
    config_file = _minimal_config(tmp_path)
    cfg = load_config(config_file)
    assert cfg.console.default_layout == "tiled"


@pytest.mark.parametrize(
    "layout",
    ["tiled", "even-vertical", "even-horizontal", "main-vertical", "main-horizontal"],
)
def test_console_default_layout_accepts_valid_presets(tmp_path: Path, layout: str) -> None:
    """All five tmux preset layout names are accepted verbatim."""
    config_file = _minimal_config(tmp_path, f"""
        [console]
        default_layout = "{layout}"
    """)
    cfg = load_config(config_file)
    assert cfg.console.default_layout == layout


def test_console_default_layout_rejects_unknown(tmp_path: Path) -> None:
    """Unknown layout names fail at load with a list of valid alternatives."""
    config_file = _minimal_config(tmp_path, """
        [console]
        default_layout = "tabbed"
    """)
    with pytest.raises(ConfigError, match="console.default_layout must be one of"):
        load_config(config_file)


def test_console_section_unexpected_keys_warn(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Unknown keys in [console] surface as warnings, not silent ignores."""
    config_file = _minimal_config(tmp_path, """
        [console]
        default_layout = "tiled"
        unknown_key = "x"
    """)
    load_config(config_file)
    captured = capsys.readouterr()
    assert "unknown_key" in captured.err or "unknown_key" in captured.out
