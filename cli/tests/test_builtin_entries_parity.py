"""Parity oracle for the optional installer plugin rows.

This module pins the resolved payloads of the apt and user install-command
entries as constants, captured from the pre-plugin built-in definition path.
It is the permanent no-drift reference: the rows now ship from two opt-in
system plugins, and the parity test here asserts the Registry still resolves
byte-for-byte identical payloads.

The constants are the oracle. They are hand-transcribed (not derived from
the loader) on purpose: if a payload silently changes in the bundled
manifests, the constant no longer matches and the test fails.

Payload scope: only the kind-specific fields plus ``name`` and
``description`` are compared. Provenance fields on ``DeclaredResource``
(``origin``, ``declared_at``, ``references``) are checked separately by the
provider assertions below.
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

# -- The oracle: resolved payloads of the moved entries ------------------------

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


# -- Installer plugins resolve to the oracle payloads --------------------------

_PLUGIN_MANIFESTS = {
    "apt": {
        "apt-source": "agentworks.plugins.apt/manifests/apt-sources.yaml",
        "apt-package": "agentworks.plugins.apt/manifests/apt-packages.yaml",
    },
    "install-command": {
        "user-install-command": "agentworks.plugins.install_command/manifests/install-commands.yaml",
    },
}


def _write_operator_config(
    tmp_path: Path,
    *,
    manifests: dict[str, str] | None = None,
) -> Path:
    """Write a minimal operator config (plus optional resources/*.yaml
    manifests) and return the config path.

    config.toml is settings only now (ADR 0022): operator apt /
    install-command entries are declared as ``resources/*.yaml`` manifests.
    """
    pub = tmp_path / "id.pub"
    priv = tmp_path / "id"
    pub.write_text("ssh-ed25519 X")
    priv.write_text("-----BEGIN-----")
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'[operator]\nssh_public_key = "{pub}"\nssh_private_key = "{priv}"\n')
    if manifests:
        resources = tmp_path / "resources"
        resources.mkdir()
        for filename, content in manifests.items():
            (resources / filename).write_text(content)
    return cfg


def test_installer_plugin_rows_match_oracle_and_are_disabled_by_default(tmp_path: Path) -> None:
    """The 16 moved rows retain their payloads, provider, anchor-derived
    provenance, and default-disabled state.
    """
    from agentworks.bootstrap import build_registry
    from agentworks.config import load_config
    from agentworks.plugins import SYSTEM_PLUGINS
    from agentworks.resources.access import kind_dict
    from agentworks.resources.graph import Enablement

    cfg = load_config(_write_operator_config(tmp_path), warn_issues=False)
    registry = build_registry(cfg)

    assert SYSTEM_PLUGINS["apt"].capabilities == {}
    assert SYSTEM_PLUGINS["apt"].manifests == "agentworks.plugins.apt"
    assert SYSTEM_PLUGINS["install-command"].capabilities == {}
    assert SYSTEM_PLUGINS["install-command"].manifests == "agentworks.plugins.install_command"

    srcs = {
        name: entry
        for name, entry in kind_dict(registry, "apt-source").items()
        if entry.origin.plugin == "apt"
    }
    pkgs = {
        name: entry
        for name, entry in kind_dict(registry, "apt-package").items()
        if entry.origin.plugin == "apt"
    }
    usr_cmds = {
        name: entry
        for name, entry in kind_dict(registry, "user-install-command").items()
        if entry.origin.plugin == "install-command"
    }

    assert list(srcs) == list(EXPECTED_APT_SOURCES)
    assert list(pkgs) == list(EXPECTED_APT_PACKAGES)
    assert list(usr_cmds) == list(EXPECTED_USER_INSTALL_COMMANDS)
    assert {name: apt_source_payload(entry) for name, entry in srcs.items()} == EXPECTED_APT_SOURCES
    assert {name: apt_package_payload(entry) for name, entry in pkgs.items()} == EXPECTED_APT_PACKAGES
    assert {name: install_command_payload(entry) for name, entry in usr_cmds.items()} == EXPECTED_USER_INSTALL_COMMANDS

    for plugin, rows_by_kind in (
        ("apt", (("apt-source", srcs), ("apt-package", pkgs))),
        ("install-command", (("user-install-command", usr_cmds),)),
    ):
        for kind, rows in rows_by_kind:
            source = _PLUGIN_MANIFESTS[plugin][kind]
            for entry in rows.values():
                assert entry.origin is not None
                assert entry.origin.variant == "system-plugin"
                assert entry.origin.plugin == plugin
                assert entry.origin.source == source
                assert registry.graph.enablement_of(kind, entry.name) is Enablement.disabled

# The former ``test_operator_toml_override_wins_over_builtin`` was removed here:
# it declared an operator ``[apt_packages.gh]`` override in config.toml, which
# now hard-errors as a resource section (ADR 0022). With the TOML declaration
# surface gone, that test collapses onto its manifest sibling
# ``test_operator_manifest_override_wins_over_installer_plugin`` below (same override
# semantics, same operator-declared origin), which is the sole remaining path.


def test_operator_manifest_override_wins_over_installer_plugin(tmp_path: Path) -> None:
    """An operator's YAML apt-package manifest with an installer plugin's name
    replaces that row, carrying the
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
