"""Config loader tests for ``[plugins]`` (Phase 3 of the system-plugins SDD,
R4).

``[plugins].system`` is the operator's opt-in gate for system plugins:

- Absent [plugins] table, or absent ``system`` key, parses to ``()``.
- A present ``system`` list of strings parses to the equivalent tuple.
- [plugins] not a table, or ``system`` not a list of strings, is a
  ``ConfigError``.
- Unknown keys in [plugins] are a HARD ``ConfigError`` naming the section
  and the offending key(s): a deliberate divergence from the soft
  ``_warn_unexpected_keys`` convention ``[secret_config]`` uses (see the
  in-code rationale on ``_load_plugins``). The pre-rename ``enabled`` key
  is now one such unknown key, so a stale config fails loudly rather than
  silently leaving its plugins un-enabled.
- ``enabled_system_plugins`` reaches ``Config`` through the settings loader,
  mirroring ``secret_config_data``.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from agentworks.config import ConfigError, load_config


def _config(tmp_path: Path, extras: str = "") -> Path:
    pub = tmp_path / "id.pub"
    priv = tmp_path / "id"
    pub.write_text("ssh-ed25519 AAAA...")
    priv.write_text("-----BEGIN OPENSSH PRIVATE KEY-----")
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        dedent(f"""\
        [operator]
        ssh_public_key = "{pub.as_posix()}"
        ssh_private_key = "{priv.as_posix()}"
        """)
        + dedent(extras)
    )
    return cfg


def test_plugins_section_absent_is_empty(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    config = load_config(cfg, warn_issues=False)
    assert config.enabled_system_plugins == ()


def test_plugins_system_key_absent_is_empty(tmp_path: Path) -> None:
    cfg = _config(
        tmp_path,
        """
        [plugins]
        """,
    )
    config = load_config(cfg, warn_issues=False)
    assert config.enabled_system_plugins == ()


def test_plugins_system_list_parses(tmp_path: Path) -> None:
    cfg = _config(
        tmp_path,
        """
        [plugins]
        system = ["a", "b"]
        """,
    )
    config = load_config(cfg, warn_issues=False)
    assert config.enabled_system_plugins == ("a", "b")


def test_plugins_section_not_a_table_raises(tmp_path: Path) -> None:
    # ``plugins = "nope"`` must precede ``[operator]``: TOML has no
    # top-level scope once a table header is open, so appending it after
    # [operator] (this module's _config helper always writes [operator]
    # first) would parse as `operator.plugins`, not top-level `plugins`.
    pub = tmp_path / "id.pub"
    priv = tmp_path / "id"
    pub.write_text("ssh-ed25519 AAAA...")
    priv.write_text("-----BEGIN OPENSSH PRIVATE KEY-----")
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        dedent(f"""\
        plugins = "nope"

        [operator]
        ssh_public_key = "{pub.as_posix()}"
        ssh_private_key = "{priv.as_posix()}"
        """)
    )
    with pytest.raises(ConfigError):
        load_config(cfg, warn_issues=False)


def test_plugins_system_not_a_list_of_strings_raises(tmp_path: Path) -> None:
    cfg = _config(
        tmp_path,
        """
        [plugins]
        system = "a"
        """,
    )
    with pytest.raises(ConfigError):
        load_config(cfg, warn_issues=False)


def test_plugins_system_list_with_non_string_element_raises(tmp_path: Path) -> None:
    cfg = _config(
        tmp_path,
        """
        [plugins]
        system = ["a", 1]
        """,
    )
    with pytest.raises(ConfigError):
        load_config(cfg, warn_issues=False)


def test_plugins_unknown_key_is_a_hard_config_error(tmp_path: Path) -> None:
    """A typo'd key (e.g. ``sytsem``) must fail loudly at load time, not
    accumulate as a soft ``config_issues`` warning the operator could miss:
    [plugins] is an opt-in gate, so a silently-ignored typo would leave a
    plugin un-enabled with no visible signal. This is the behavior that
    diverges from ``[secret_config]``'s soft-warn convention."""
    cfg = _config(
        tmp_path,
        """
        [plugins]
        sytsem = ["a"]
        """,
    )
    with pytest.raises(ConfigError):
        # A hard error, not a collected issue: the raise prevents
        # load_config from ever returning a Config whose config_issues
        # could hide this behind a warning the operator may not read.
        load_config(cfg, warn_issues=False)


def test_plugins_old_enabled_key_is_a_hard_config_error(tmp_path: Path) -> None:
    """The pre-rename ``enabled`` key is now an unknown key, so a stale
    config surfaces a hard ``ConfigError`` naming it rather than silently
    ignoring the list and leaving those plugins un-enabled."""
    cfg = _config(
        tmp_path,
        """
        [plugins]
        enabled = ["a"]
        """,
    )
    with pytest.raises(ConfigError):
        load_config(cfg, warn_issues=False)


def test_plugins_unknown_key_alongside_valid_system_still_raises(tmp_path: Path) -> None:
    """A typo doesn't get masked just because the real ``system`` key is
    also present and valid: the unknown-key check fires regardless."""
    cfg = _config(
        tmp_path,
        """
        [plugins]
        system = ["a"]
        sytsem = ["a"]
        """,
    )
    with pytest.raises(ConfigError):
        load_config(cfg, warn_issues=False)


def test_plugins_system_reaches_config(tmp_path: Path) -> None:
    cfg = _config(
        tmp_path,
        """
        [plugins]
        system = ["a", "b"]
        """,
    )
    config = load_config(cfg, warn_issues=False)
    assert config.enabled_system_plugins == ("a", "b")
