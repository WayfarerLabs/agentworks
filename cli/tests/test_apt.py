"""Tests for the apt loaders and apt-source reference validation.

Covers the per-entry loaders in ``agentworks.apt`` (parse-level shape
validation) plus the framework integration: an apt-package referencing an
unknown apt-source is caught at ``build_registry`` time by the
``apt-source`` kind's ``error`` miss policy, not by the loader. Built-in
payload parity lives in ``test_builtin_catalog_parity.py``.
"""

from __future__ import annotations

from textwrap import dedent
from typing import TYPE_CHECKING

import pytest

from agentworks.apt import _load_apt_packages, _load_apt_sources
from agentworks.errors import ConfigError

if TYPE_CHECKING:
    from pathlib import Path


# -- apt-source loader ---------------------------------------------------------


def test_apt_source_rejects_unsafe_source_file() -> None:
    with pytest.raises(ConfigError, match="simple filename"):
        _load_apt_sources(
            {
                "bad": {
                    "description": "Bad",
                    "key_url": "https://example.com/key.gpg",
                    "key_path": "/etc/apt/keyrings/bad.gpg",
                    "source": "deb https://example.com stable main",
                    "source_file": "../evil.list",
                }
            }
        )


def test_apt_source_requires_key_url() -> None:
    with pytest.raises(ConfigError, match="key_url is required"):
        _load_apt_sources(
            {
                "bad": {
                    "description": "Bad",
                    "key_path": "/etc/apt/keyrings/bad.gpg",
                    "source": "deb https://example.com stable main",
                    "source_file": "bad.list",
                }
            }
        )


def test_apt_source_must_be_table() -> None:
    with pytest.raises(ConfigError, match="must be a table"):
        _load_apt_sources({"bad": "not-a-table"})


# -- apt-package loader --------------------------------------------------------


def test_apt_package_defaults_empty_sources() -> None:
    entries = _load_apt_packages({"vim": {"description": "Vim", "apt": ["vim"]}})
    assert entries["vim"].apt_sources == []
    assert entries["vim"].apt == ["vim"]


def test_apt_package_apt_must_be_list() -> None:
    with pytest.raises(ConfigError, match="apt must be a list"):
        _load_apt_packages({"bad": {"description": "Bad", "apt": "vim"}})


# -- Framework integration: unknown apt-source reference -----------------------


def _write_operator_config(tmp_path: Path, *, toml_body: str = "") -> Path:
    pub = tmp_path / "id.pub"
    priv = tmp_path / "id"
    pub.write_text("ssh-ed25519 X")
    priv.write_text("-----BEGIN-----")
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'[operator]\nssh_public_key = "{pub}"\nssh_private_key = "{priv}"\n'
        + toml_body
    )
    return cfg


def test_bad_apt_source_reference_errors_at_build_registry(tmp_path: Path) -> None:
    """An apt-package that names an unknown apt-source parses cleanly at
    load time but fails at ``build_registry`` when the ``apt-source`` kind's
    ``error`` miss policy resolves the reference emitted by
    ``AptPackageEntry.referenced_resources()``. Single source of truth for
    reference validation lives in the framework.
    """
    from agentworks.bootstrap import build_registry
    from agentworks.config import load_config

    toml_body = dedent(
        """
        [apt_packages.bad-pkg]
        description = "Bad"
        apt = ["bad"]
        apt_sources = ["nonexistent"]
        """
    )
    cfg = load_config(
        _write_operator_config(tmp_path, toml_body=toml_body), warn_issues=False
    )

    with pytest.raises(ConfigError, match="nonexistent"):
        build_registry(cfg)
