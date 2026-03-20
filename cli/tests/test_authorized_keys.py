"""Tests for SSH authorized_keys reconciliation during VM init."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from agentworks.config import UserConfig
from agentworks.vms.initializer import AUTHORIZED_KEYS_HEADER, _reconcile_authorized_keys


def _make_config(tmp_path: Path, extra_keys: list[str] | None = None) -> MagicMock:
    """Build a mock Config with real key files on disk."""
    pub = tmp_path / "id.pub"
    pub.write_text("ssh-ed25519 AAAA-primary\n")

    extra_paths: list[Path] = []
    for i, content in enumerate(extra_keys or []):
        p = tmp_path / f"extra{i}.pub"
        p.write_text(content + "\n")
        extra_paths.append(p)

    user = UserConfig(
        ssh_public_key=pub,
        ssh_private_key=tmp_path / "id",
        extra_ssh_public_keys=extra_paths,
    )
    config = MagicMock()
    config.user = user
    return config


def test_primary_key_only(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    target = MagicMock()
    logger = MagicMock()
    logger.has_warnings = False

    _reconcile_authorized_keys(target, config, "/home/agentworks", logger)

    target.write_file.assert_called_once()
    path, content = target.write_file.call_args.args
    assert path == "/home/agentworks/.ssh/authorized_keys"
    assert "ssh-ed25519 AAAA-primary" in content
    assert content.startswith(AUTHORIZED_KEYS_HEADER)


def test_primary_plus_extra_keys(tmp_path: Path) -> None:
    config = _make_config(tmp_path, extra_keys=[
        "ssh-rsa BBBB-extra1",
        "ssh-ed25519 CCCC-extra2",
    ])
    target = MagicMock()
    logger = MagicMock()
    logger.has_warnings = False

    _reconcile_authorized_keys(target, config, "/home/agentworks", logger)

    content = target.write_file.call_args.args[1]
    assert "ssh-ed25519 AAAA-primary" in content
    assert "ssh-rsa BBBB-extra1" in content
    assert "ssh-ed25519 CCCC-extra2" in content
    assert content.startswith(AUTHORIZED_KEYS_HEADER)


def test_header_present(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    target = MagicMock()
    logger = MagicMock()
    logger.has_warnings = False

    _reconcile_authorized_keys(target, config, "/home/agentworks", logger)

    content = target.write_file.call_args.args[1]
    assert "Managed by agentworks" in content
    assert "manual edits will be overwritten" in content
    assert "extra_ssh_public_keys" in content


def test_file_mode_600(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    target = MagicMock()
    logger = MagicMock()
    logger.has_warnings = False

    _reconcile_authorized_keys(target, config, "/home/agentworks", logger)

    assert target.write_file.call_args.kwargs["mode"] == "600"


def test_full_overwrite_semantics(tmp_path: Path) -> None:
    """Verify the file is a complete replacement, not an append."""
    config = _make_config(tmp_path, extra_keys=["ssh-rsa BBBB-extra1"])
    target = MagicMock()
    logger = MagicMock()
    logger.has_warnings = False

    _reconcile_authorized_keys(target, config, "/home/agentworks", logger)

    content = target.write_file.call_args.args[1]
    # Should have exactly the header + 2 keys (primary + 1 extra), each on its own line
    lines = [ln for ln in content.splitlines() if not ln.startswith("#") and ln.strip()]
    assert len(lines) == 2
