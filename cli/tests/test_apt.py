"""Tests for the apt loaders and apt-source reference validation.

Covers the per-entry loaders in ``agentworks.apt`` (parse-level shape
validation) plus the framework integration: an apt-package referencing an
unknown apt-source is caught at ``build_registry`` time by the
``apt-source`` kind's ``error`` miss policy, not by the loader. Optional
installer-plugin payload parity lives in ``test_builtin_entries_parity.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from agentworks.apt import AptSourceEntry, _load_apt_packages, _load_apt_sources
from agentworks.debian import DebianRelease
from agentworks.errors import ConfigError
from tests.conftest import ManifestDoc, write_manifests

if TYPE_CHECKING:
    from collections.abc import Sequence
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


def test_apt_source_resolves_a_release_map() -> None:
    source = AptSourceEntry(
        name="mapped",
        key_url="https://example.com/key.gpg",
        key_path="/etc/apt/keyrings/example.gpg",
        source=None,
        sources={
            DebianRelease.BOOKWORM: "deb https://example.com bookworm main",
            DebianRelease.TRIXIE: "deb https://example.com trixie main",
        },
        source_file="example.list",
    )

    assert source.source_for(DebianRelease.BOOKWORM).split()[-2] == "bookworm"
    assert source.source_for(DebianRelease.TRIXIE).split()[-2] == "trixie"


@pytest.mark.parametrize(
    ("source", "sources"),
    [
        (None, None),
        ("deb https://example.com stable main", {DebianRelease.TRIXIE: "deb https://example.com trixie main"}),
        ("deb https://example.com bookworm main", None),
    ],
)
def test_apt_source_requires_one_release_safe_shape(
    source: str | None,
    sources: dict[DebianRelease, str] | None,
) -> None:
    with pytest.raises(ValueError):
        AptSourceEntry(
            name="bad",
            key_url="https://example.com/key.gpg",
            key_path="/etc/apt/keyrings/example.gpg",
            source=source,
            sources=sources,
            source_file="example.list",
        )


# -- apt-package loader --------------------------------------------------------


def test_apt_package_defaults_empty_sources() -> None:
    entries = _load_apt_packages({"vim": {"description": "Vim", "apt": ["vim"]}})
    assert entries["vim"].apt_sources == []
    assert entries["vim"].apt == ["vim"]


def test_apt_package_apt_must_be_list() -> None:
    with pytest.raises(ConfigError, match="apt must be a list"):
        _load_apt_packages({"bad": {"description": "Bad", "apt": "vim"}})


# -- Framework integration: unknown apt-source reference -----------------------


def _write_operator_config(tmp_path: Path, *, manifests: Sequence[ManifestDoc | str] = ()) -> Path:
    pub = tmp_path / "id.pub"
    priv = tmp_path / "id"
    pub.write_text("ssh-ed25519 X")
    priv.write_text("-----BEGIN-----")
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'[operator]\nssh_public_key = "{pub}"\nssh_private_key = "{priv}"\n')
    if manifests:
        write_manifests(tmp_path, *manifests)
    return cfg


def test_bad_apt_source_reference_errors_at_build_registry(tmp_path: Path) -> None:
    """An apt-package that names an unknown apt-source parses cleanly at
    load time but fails at ``build_registry`` when the ``apt-source`` kind's
    ``error`` miss policy resolves the reference emitted by
    ``AptPackageEntry.dependencies()``. Single source of truth for
    reference validation lives in the framework.
    """
    from agentworks.bootstrap import build_registry
    from agentworks.config import load_config

    cfg = load_config(
        _write_operator_config(
            tmp_path,
            manifests=[
                ManifestDoc(
                    "apt-package",
                    "bad-pkg",
                    {"apt": ["bad"], "apt_sources": ["nonexistent"]},
                    description="Bad",
                )
            ],
        ),
        warn_issues=False,
    )

    with pytest.raises(ConfigError, match="nonexistent"):
        build_registry(cfg)
