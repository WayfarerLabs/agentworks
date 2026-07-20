"""Parity oracle for the built-in catalog (dissolve-catalog SDD).

This module pins the resolved payloads of all 18 built-in catalog
entries as constants, captured from ``load_builtin_catalog()`` before the
built-in definition path moves off ``catalog.toml``. It is the no-drift
reference for the migration: later phases move these entries to bundled
YAML manifests, and the parity tests here assert the Registry still
resolves byte-for-byte identical payloads.

The constants are the oracle. They are hand-transcribed (not derived from
the loader) on purpose: if a payload silently changes on either side
(``catalog.toml`` or the bundled manifests), the constant no longer
matches and the test fails.

Payload scope: only the kind-specific fields plus ``name`` and
``description`` are compared. Provenance fields on ``DeclaredResource``
(``origin``, ``declared_at``, ``references``) are deliberately excluded:
they differ by definition-path and are checked separately by the origin
assertions in the Phase 1 registry test.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agentworks.catalog import (
        AptPackageEntry,
        AptSourceEntry,
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
            "deb [arch={arch} signed-by=/etc/apt/keyrings/ngrok.gpg] "
            "https://ngrok-agent.s3.amazonaws.com bookworm main"
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


# -- Phase 0: the oracle matches the current TOML definition path --------------


def test_oracle_matches_builtin_catalog() -> None:
    """The oracle constants reproduce the payloads that
    ``load_builtin_catalog()`` resolves from ``catalog.toml`` today. This
    anchors the oracle to the pre-migration behavior; the Phase 1 test
    then holds the bundled-manifest Registry rows against the same oracle.
    """
    from agentworks.catalog import load_builtin_catalog

    catalog = load_builtin_catalog()

    assert {
        name: apt_source_payload(entry)
        for name, entry in catalog.apt_sources.items()
    } == EXPECTED_APT_SOURCES
    assert {
        name: apt_package_payload(entry)
        for name, entry in catalog.apt_packages.items()
    } == EXPECTED_APT_PACKAGES
    assert {
        name: install_command_payload(entry)
        for name, entry in catalog.system_install_commands.items()
    } == EXPECTED_SYSTEM_INSTALL_COMMANDS
    assert {
        name: install_command_payload(entry)
        for name, entry in catalog.user_install_commands.items()
    } == EXPECTED_USER_INSTALL_COMMANDS
