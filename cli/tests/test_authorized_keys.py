"""Tests for SSH authorized_keys reconciliation during VM init."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

from agentworks.config import OperatorConfig
from agentworks.ssh import SSHError
from agentworks.vms.initializer import AUTHORIZED_KEYS_HEADER, _reconcile_authorized_keys
from agentworks.vms.initializer.ssh_keys import AuthorizedKeysUnproven
from tests.ssh_fixtures import TEST_SSH_PUBLIC_KEY, write_test_ssh_keypair

if TYPE_CHECKING:
    from tests.conftest import CapturedOutput


def _make_config(tmp_path: Path, extra_keys: list[str] | None = None) -> MagicMock:
    """Build a mock Config with real key files on disk."""
    private = tmp_path / "id"
    write_test_ssh_keypair(private)
    pub = tmp_path / "id.pub"

    extra_paths: list[Path] = []
    for i, content in enumerate(extra_keys or []):
        p = tmp_path / f"extra{i}.pub"
        p.write_text(content + "\n")
        extra_paths.append(p)

    operator = OperatorConfig(
        ssh_public_key=pub,
        ssh_private_key=private,
        extra_ssh_public_keys=extra_paths,
    )
    config = MagicMock()
    config.operator = operator
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
    assert TEST_SSH_PUBLIC_KEY in content
    assert content.startswith(AUTHORIZED_KEYS_HEADER)


def test_primary_plus_extra_keys(tmp_path: Path) -> None:
    config = _make_config(
        tmp_path,
        extra_keys=[
            "ssh-rsa BBBB-extra1",
            "ssh-ed25519 CCCC-extra2",
        ],
    )
    target = MagicMock()
    logger = MagicMock()
    logger.has_warnings = False

    _reconcile_authorized_keys(target, config, "/home/agentworks", logger)

    content = target.write_file.call_args.args[1]
    assert TEST_SSH_PUBLIC_KEY in content
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


def test_direct_write_failure_does_not_expose_transport_detail(
    tmp_path: Path,
    captured_output: CapturedOutput,
) -> None:
    sentinel = "sensitive-transport-diagnostic-sentinel"
    config = _make_config(tmp_path)
    target = MagicMock()
    target.write_file.side_effect = SSHError(sentinel)
    logger = MagicMock()

    outcome = _reconcile_authorized_keys(target, config, "/home/agentworks", logger)

    assert isinstance(outcome, AuthorizedKeysUnproven)
    diagnostics = (
        repr(outcome),
        repr(getattr(outcome, "args", ())),
        repr(logger.warning.call_args_list),
        repr(captured_output.warnings),
        repr(captured_output.lines),
    )
    assert all(sentinel not in diagnostic for diagnostic in diagnostics)


# -- owner= (stage-and-install) path ---------------------------------------


def _build_owner_target(staging_path: str = "/tmp/agw-ak.ABC123") -> MagicMock:
    """``Transport`` mock whose mktemp returns a deterministic staging path."""
    target = MagicMock()

    def _run_side_effect(cmd: str, *_, **__) -> MagicMock:
        result = MagicMock()
        result.ok = True
        if cmd.startswith("mktemp"):
            result.stdout = staging_path + "\n"
        else:
            result.stdout = ""
        return result

    target.run.side_effect = _run_side_effect
    return target


def test_owner_branch_stages_then_installs(tmp_path: Path) -> None:
    """owner= path: install -d, mktemp, write_file (private mode), install, rm."""
    config = _make_config(tmp_path)
    target = _build_owner_target()
    logger = MagicMock()
    logger.has_warnings = False

    _reconcile_authorized_keys(target, config, "/home/agt-foo", logger, owner="agt-foo")

    # Sequence of target.run commands (run is called 4 times: install -d,
    # mktemp, install -o ..., rm -f).
    run_cmds = [call.args[0] for call in target.run.call_args_list]
    assert any(cmd.startswith("install -d -o agt-foo -g agt-foo -m 0700 /home/agt-foo/.ssh") for cmd in run_cmds), (
        f"missing install -d in {run_cmds}"
    )
    assert any(cmd.startswith("mktemp") for cmd in run_cmds), f"missing mktemp in {run_cmds}"
    assert any(
        "install -o agt-foo -g agt-foo -m 0600" in cmd and "/home/agt-foo/.ssh/authorized_keys" in cmd
        for cmd in run_cmds
    ), f"missing final install in {run_cmds}"
    assert any(cmd.startswith("rm -f") for cmd in run_cmds), f"missing rm -f cleanup in {run_cmds}"

    # write_file must land in the mktemp staging path with 0600 perms so the
    # content is private before the atomic install lands it in agent home.
    target.write_file.assert_called_once()
    staging_path, content = target.write_file.call_args.args
    assert staging_path == "/tmp/agw-ak.ABC123"
    assert target.write_file.call_args.kwargs["mode"] == "0600"
    assert content.startswith(AUTHORIZED_KEYS_HEADER)


def test_owner_branch_cleans_up_on_install_failure(tmp_path: Path) -> None:
    """If install -o ... fails, the staging rm -f still runs (try/finally)."""
    config = _make_config(tmp_path)
    target = MagicMock()
    run_calls: list[str] = []

    def _run_side_effect(cmd: str, *_, **__) -> MagicMock:
        run_calls.append(cmd)
        if cmd.startswith("mktemp"):
            r = MagicMock()
            r.stdout = "/tmp/agw-ak.ABC123\n"
            r.ok = True
            return r
        if "install -o agt-foo" in cmd:
            raise SSHError("simulated install failure")
        r = MagicMock()
        r.ok = True
        r.stdout = ""
        return r

    target.run.side_effect = _run_side_effect
    logger = MagicMock()
    logger.has_warnings = False

    import contextlib

    with contextlib.suppress(SSHError):
        _reconcile_authorized_keys(target, config, "/home/agt-foo", logger, owner="agt-foo")

    # Cleanup must have run despite the install failure.
    assert any(cmd.startswith("rm -f /tmp/agw-ak.ABC123") for cmd in run_calls), (
        f"expected staging cleanup, got: {run_calls}"
    )


def test_owner_branch_raises_on_failure(tmp_path: Path) -> None:
    """owner= path raises (unlike owner=None which warns).

    The caller relies on this so a half-set-up agent triggers rollback
    rather than continuing with downstream agent SSH calls that all fail
    with cryptic exit 255 errors.
    """
    config = _make_config(tmp_path)
    target = MagicMock()

    def _run_side_effect(cmd: str, *_, **__) -> MagicMock:
        if cmd.startswith("install -d"):
            raise SSHError("simulated install -d failure")
        r = MagicMock()
        r.ok = True
        r.stdout = ""
        return r

    target.run.side_effect = _run_side_effect
    logger = MagicMock()
    logger.has_warnings = False

    raised = False
    try:
        _reconcile_authorized_keys(target, config, "/home/agt-foo", logger, owner="agt-foo")
    except SSHError:
        raised = True
    assert raised, "owner= path must raise on failure"
