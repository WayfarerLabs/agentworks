"""Parity oracle for the built-in apt / install-command entries (dissolve-catalog SDD).

This module pins the resolved payloads of all 18 built-in apt /
install-command entries as constants, captured from the pre-migration
built-in definition path. It is the permanent no-drift reference: the
built-in entries now ship as bundled YAML manifests, and the parity test
here asserts the Registry still resolves byte-for-byte identical payloads.

The constants are the oracle. They are hand-transcribed (not derived from
the loader) on purpose: if a payload silently changes in the bundled
manifests, the constant no longer matches and the test fails.

Payload scope: only the kind-specific fields plus ``name`` and
``description`` are compared. Provenance fields on ``DeclaredResource``
(``origin``, ``declared_at``, ``references``) are deliberately excluded:
they differ by definition-path and are checked separately by the origin
assertions in the Phase 1 registry test.
"""

from __future__ import annotations

from textwrap import dedent
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    from agentworks.apt import AptPackageEntry, AptSourceEntry
    from agentworks.install_commands import (
        SystemInstallCommandEntry,
        UserInstallCommandEntry,
    )

# -- The oracle: resolved payloads of the 18 built-in entries ------------------

EXPECTED_APT_SOURCES: dict[str, dict[str, Any]] = {
    "github-cli": {
        "name": "github-cli",
        "description": "GitHub CLI official apt repository",
        "key_url": "https://cli.github.com/packages/githubcli-archive-keyring.gpg",
        "key_path": "/etc/apt/keyrings/githubcli-archive-keyring.gpg",
        "source": (
            "deb [arch={arch} signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] "
            "https://cli.github.com/packages stable main"
        ),
        "source_file": "github-cli.list",
        "key_dearmor": False,
    },
    "hashicorp": {
        "name": "hashicorp",
        "description": "HashiCorp official apt repository",
        "key_url": "https://apt.releases.hashicorp.com/gpg",
        "key_path": "/etc/apt/keyrings/hashicorp-archive-keyring.gpg",
        "source": (
            "deb [arch={arch} signed-by=/etc/apt/keyrings/hashicorp-archive-keyring.gpg] "
            "https://apt.releases.hashicorp.com bookworm main"
        ),
        "source_file": "hashicorp.list",
        "key_dearmor": True,
    },
    "nodesource-v22": {
        "name": "nodesource-v22",
        "description": "NodeSource Node.js 22.x apt repository",
        "key_url": "https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key",
        "key_path": "/etc/apt/keyrings/nodesource.gpg",
        "source": (
            "deb [arch={arch} signed-by=/etc/apt/keyrings/nodesource.gpg] "
            "https://deb.nodesource.com/node_22.x nodistro main"
        ),
        "source_file": "nodesource.list",
        "key_dearmor": True,
    },
    "ngrok-agent": {
        "name": "ngrok-agent",
        "description": "ngrok agent apt repository",
        "key_url": "https://ngrok-agent.s3.amazonaws.com/ngrok.asc",
        "key_path": "/etc/apt/keyrings/ngrok.gpg",
        "source": (
            "deb [arch={arch} signed-by=/etc/apt/keyrings/ngrok.gpg] https://ngrok-agent.s3.amazonaws.com bookworm main"
        ),
        "source_file": "ngrok.list",
        "key_dearmor": True,
    },
    "tofuutils-tenv": {
        "name": "tofuutils-tenv",
        "description": "tofuutils tenv apt repository (Cloudsmith)",
        "key_url": "https://dl.cloudsmith.io/public/tofuutils/tenv/gpg.8ACD4386ADD982F6.key",
        "key_path": "/etc/apt/keyrings/tofuutils-tenv-archive-keyring.gpg",
        "source": (
            "deb [signed-by=/etc/apt/keyrings/tofuutils-tenv-archive-keyring.gpg] "
            "https://dl.cloudsmith.io/public/tofuutils/tenv/deb/debian bookworm main"
        ),
        "source_file": "tofuutils-tenv.list",
        "key_dearmor": True,
    },
}

EXPECTED_APT_PACKAGES: dict[str, dict[str, Any]] = {
    "gh": {
        "name": "gh",
        "description": "GitHub CLI",
        "apt": ["gh"],
        "apt_sources": ["github-cli"],
    },
    "terraform": {
        "name": "terraform",
        "description": "HashiCorp Terraform",
        "apt": ["terraform"],
        "apt_sources": ["hashicorp"],
    },
    "nodejs": {
        "name": "nodejs",
        "description": "Node.js 22.x via NodeSource",
        "apt": ["nodejs"],
        "apt_sources": ["nodesource-v22"],
    },
    "ngrok": {
        "name": "ngrok",
        "description": "ngrok reverse tunnel agent",
        "apt": ["ngrok"],
        "apt_sources": ["ngrok-agent"],
    },
    "tenv": {
        "name": "tenv",
        "description": "tenv (Terraform/OpenTofu/Terragrunt version manager)",
        "apt": ["tenv"],
        "apt_sources": ["tofuutils-tenv"],
    },
}

EXPECTED_SYSTEM_INSTALL_COMMANDS: dict[str, dict[str, Any]] = {
    "az-cli": {
        "name": "az-cli",
        "description": "Azure CLI",
        "command": "curl -sL https://aka.ms/InstallAzureCLIDeb | sudo bash",
        "path": [],
        "test_exec": "az",
        "test_file": None,
        "test_dir": None,
    },
}

EXPECTED_USER_INSTALL_COMMANDS: dict[str, dict[str, Any]] = {
    "oh-my-zsh": {
        "name": "oh-my-zsh",
        "description": "Oh My Zsh",
        "command": (
            'sh -c "$(curl -fsSL '
            'https://raw.githubusercontent.com/ohmyzsh/ohmyzsh/master/tools/install.sh)" '
            "-- --unattended"
        ),
        "path": [],
        "test_exec": None,
        "test_file": None,
        "test_dir": "~/.oh-my-zsh",
    },
    "bun": {
        "name": "bun",
        "description": "Bun JavaScript runtime",
        "command": "curl -fsSL https://bun.sh/install | bash",
        "path": [],
        "test_exec": "bun",
        "test_file": None,
        "test_dir": None,
    },
    "fnm": {
        "name": "fnm",
        "description": "Fast Node Manager",
        "command": "curl -fsSL https://fnm.vercel.app/install | bash",
        "path": [],
        "test_exec": "fnm",
        "test_file": None,
        "test_dir": None,
    },
    "nvm": {
        "name": "nvm",
        "description": "Node Version Manager",
        "command": "curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.4/install.sh | bash",
        "path": [],
        "test_exec": None,
        "test_file": "~/.nvm/nvm.sh",
        "test_dir": None,
    },
    "claude": {
        "name": "claude",
        "description": "Claude Code CLI",
        "command": "curl -fsSL https://claude.ai/install.sh | bash",
        "path": ["~/.local/bin"],
        "test_exec": "claude",
        "test_file": None,
        "test_dir": None,
    },
    "starship": {
        "name": "starship",
        "description": "Starship cross-shell prompt",
        "command": "curl -sS https://starship.rs/install.sh | sh -s -- -y -b ~/.local/bin",
        "path": ["~/.local/bin"],
        "test_exec": "starship",
        "test_file": None,
        "test_dir": None,
    },
    "uv": {
        "name": "uv",
        "description": "uv Python version manager",
        "command": "curl -LsSf https://astral.sh/uv/install.sh | sh",
        "path": [],
        "test_exec": "uv",
        "test_file": None,
        "test_dir": None,
    },
}


# -- Payload extractors: entry dataclass -> comparable payload dict -------------


def apt_source_payload(entry: AptSourceEntry) -> dict[str, Any]:
    return {
        "name": entry.name,
        "description": entry.description,
        "key_url": entry.key_url,
        "key_path": entry.key_path,
        "source": entry.source,
        "source_file": entry.source_file,
        "key_dearmor": entry.key_dearmor,
    }


def apt_package_payload(entry: AptPackageEntry) -> dict[str, Any]:
    return {
        "name": entry.name,
        "description": entry.description,
        "apt": list(entry.apt),
        "apt_sources": list(entry.apt_sources),
    }


def install_command_payload(
    entry: SystemInstallCommandEntry | UserInstallCommandEntry,
) -> dict[str, Any]:
    return {
        "name": entry.name,
        "description": entry.description,
        "command": entry.command,
        "path": list(entry.path),
        "test_exec": entry.test_exec,
        "test_file": entry.test_file,
        "test_dir": entry.test_dir,
    }


# -- Bundled built-in manifests resolve to the oracle payloads -----------------

# Which bundled file each kind's built-in rows ship in. The origin's
# source carries the shipped filename (see manifests/builtin.py).
_BUNDLED_SOURCE = {
    "apt-source": "agentworks.manifests.builtin/apt-sources.yaml",
    "apt-package": "agentworks.manifests.builtin/apt-packages.yaml",
    "system-install-command": "agentworks.manifests.builtin/install-commands.yaml",
    "user-install-command": "agentworks.manifests.builtin/install-commands.yaml",
}


def _write_operator_config(
    tmp_path: Path,
    *,
    toml_body: str = "",
    manifests: dict[str, str] | None = None,
) -> Path:
    """Write a minimal operator config (plus optional TOML apt /
    install-command entries and resources/*.yaml manifests) and return
    the config path.
    """
    pub = tmp_path / "id.pub"
    priv = tmp_path / "id"
    pub.write_text("ssh-ed25519 X")
    priv.write_text("-----BEGIN-----")
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'[operator]\nssh_public_key = "{pub}"\nssh_private_key = "{priv}"\n' + toml_body)
    if manifests:
        resources = tmp_path / "resources"
        resources.mkdir()
        for filename, content in manifests.items():
            (resources / filename).write_text(content)
    return cfg


def test_bundled_builtin_rows_match_oracle(tmp_path: Path) -> None:
    """On a config with no operator apt / install-command entries, the
    Registry's built-in rows come entirely from the bundled manifests and
    resolve to the Phase 0 oracle payloads byte-for-byte, each carrying a
    ``built-in`` origin pointed at its bundled file. This is the no-drift
    proof for the TOML-to-YAML migration.
    """
    from agentworks.bootstrap import build_registry
    from agentworks.config import load_config
    from agentworks.resources.access import kind_dict

    cfg = load_config(_write_operator_config(tmp_path), warn_issues=False)
    registry = build_registry(cfg)

    srcs = kind_dict(registry, "apt-source")
    pkgs = kind_dict(registry, "apt-package")
    sys_cmds = kind_dict(registry, "system-install-command")
    usr_cmds = kind_dict(registry, "user-install-command")

    assert {name: apt_source_payload(entry) for name, entry in srcs.items()} == EXPECTED_APT_SOURCES
    assert {name: apt_package_payload(entry) for name, entry in pkgs.items()} == EXPECTED_APT_PACKAGES
    assert {
        name: install_command_payload(entry) for name, entry in sys_cmds.items()
    } == EXPECTED_SYSTEM_INSTALL_COMMANDS
    assert {name: install_command_payload(entry) for name, entry in usr_cmds.items()} == EXPECTED_USER_INSTALL_COMMANDS

    # Provenance: every built-in row is a built-in origin pointed at the
    # bundled file for its kind (not the former agentworks.catalog source).
    for kind, rows in (
        ("apt-source", srcs),
        ("apt-package", pkgs),
        ("system-install-command", sys_cmds),
        ("user-install-command", usr_cmds),
    ):
        for entry in rows.values():
            assert entry.origin is not None
            assert entry.origin.variant == "built-in"
            assert entry.origin.source == _BUNDLED_SOURCE[kind]


def test_operator_toml_override_wins_over_builtin(tmp_path: Path) -> None:
    """An operator's TOML apt-package with a built-in's name replaces the
    built-in row (publish order + builtin_override='allow'), carrying the
    operator payload and an operator-declared origin.
    """
    from agentworks.bootstrap import build_registry
    from agentworks.config import load_config
    from agentworks.resources.access import kind_dict

    toml_body = dedent(
        """
        [apt_packages.gh]
        description = "Operator gh override"
        apt = ["gh", "gh-extra"]
        apt_sources = ["github-cli"]
        """
    )
    cfg = load_config(_write_operator_config(tmp_path, toml_body=toml_body), warn_issues=False)
    registry = build_registry(cfg)

    gh = kind_dict(registry, "apt-package")["gh"]
    assert gh.apt == ["gh", "gh-extra"]
    assert gh.description == "Operator gh override"
    assert gh.origin is not None
    assert gh.origin.variant == "operator-declared"


def test_operator_manifest_override_wins_over_builtin(tmp_path: Path) -> None:
    """An operator's YAML apt-package manifest with a built-in's name
    replaces the built-in row, the same as the TOML path, carrying the
    operator payload and an operator-declared origin.
    """
    from agentworks.bootstrap import build_registry
    from agentworks.config import load_config
    from agentworks.resources.access import kind_dict

    manifest = dedent(
        """
        apiVersion: agentworks/v1
        kind: apt-package
        metadata:
          name: gh
          description: Operator gh override
        spec:
          apt_sources:
            - github-cli
          apt:
            - gh
            - gh-extra
        """
    )
    cfg = load_config(
        _write_operator_config(tmp_path, manifests={"override.yaml": manifest}),
        warn_issues=False,
    )
    registry = build_registry(cfg)

    gh = kind_dict(registry, "apt-package")["gh"]
    assert gh.apt == ["gh", "gh-extra"]
    assert gh.description == "Operator gh override"
    assert gh.origin is not None
    assert gh.origin.variant == "operator-declared"
