"""Tests for the tailscaled cold-boot DNS race fix.

See ``agentworks.vms.tailscale_dns`` and GitHub issue #117 for the
root-cause analysis. These tests cover each step's idempotency and the
sequencing of file rewrites + daemon-reload.
"""

from __future__ import annotations

from unittest.mock import MagicMock


def _make_dns_fix_target(
    *,
    resolv_link_target: str | None = None,
    dropin_content: str | None = None,
) -> MagicMock:
    """ExecTarget mock parameterized by readlink output and current drop-in content.

    - ``resolv_link_target=None``: ``readlink /etc/resolv.conf`` exits non-zero
      (regular file or absent), mirroring the broken/latched state.
    - ``resolv_link_target="..."``: ``readlink`` returns this target with exit 0.
    - ``dropin_content=None``: the drop-in file is absent (``cat`` exits non-zero).
    - ``dropin_content="..."``: ``cat`` returns this content.
    """
    target = MagicMock()
    write_log: list[tuple[str, str]] = []
    run_log: list[str] = []
    target.write_log = write_log
    target.run_log = run_log

    from agentworks.vms.tailscale_dns import (
        RESOLV_CONF_PATH,
        TAILSCALED_DROPIN_PATH,
    )

    def run_side_effect(cmd, **kwargs):  # noqa: ANN001 -- mock side_effect signature
        run_log.append(cmd)
        result = MagicMock()
        result.stderr = ""
        if cmd == f"readlink {RESOLV_CONF_PATH}":
            if resolv_link_target is None:
                result.returncode = 1
                result.ok = False
                result.stdout = ""
            else:
                result.returncode = 0
                result.ok = True
                result.stdout = resolv_link_target + "\n"
        elif cmd == f"cat {TAILSCALED_DROPIN_PATH}":
            if dropin_content is None:
                result.returncode = 1
                result.ok = False
                result.stdout = ""
            else:
                result.returncode = 0
                result.ok = True
                result.stdout = dropin_content
        elif cmd.startswith("mktemp"):
            result.returncode = 0
            result.ok = True
            result.stdout = "/tmp/agw-tsdns.AAAAAA"
        else:
            result.returncode = 0
            result.ok = True
            result.stdout = ""
        return result

    target.run.side_effect = run_side_effect
    target.write_file.side_effect = lambda path, content, **kw: write_log.append((path, content))
    return target


# -- systemd-resolved enable ---------------------------------------------------


def test_ensure_systemd_resolved_enabled_always_calls_enable_now() -> None:
    """`systemctl enable --now` is idempotent at the systemd layer, so we
    always call it without a pre-check."""
    from agentworks.vms.tailscale_dns import _ensure_systemd_resolved_enabled

    target = _make_dns_fix_target()
    logger = MagicMock()

    _ensure_systemd_resolved_enabled(target, logger)

    assert any(cmd == "systemctl enable --now systemd-resolved" for cmd in target.run_log)


# -- /etc/resolv.conf symlink repair -------------------------------------------


def test_resolv_conf_symlink_noop_when_already_correct() -> None:
    """If readlink shows the resolved stub already, no ln is invoked."""
    from agentworks.vms.tailscale_dns import (
        RESOLVED_STUB_PATH,
        _ensure_resolv_conf_symlink,
    )

    target = _make_dns_fix_target(resolv_link_target=RESOLVED_STUB_PATH)
    logger = MagicMock()

    _ensure_resolv_conf_symlink(target, logger)

    assert not any(cmd.startswith("ln -sf") for cmd in target.run_log)


def test_resolv_conf_symlink_repaired_when_not_symlink() -> None:
    """A non-symlink (regular file, the latched-broken state) is repaired."""
    from agentworks.vms.tailscale_dns import (
        RESOLV_CONF_PATH,
        RESOLVED_STUB_PATH,
        _ensure_resolv_conf_symlink,
    )

    target = _make_dns_fix_target(resolv_link_target=None)
    logger = MagicMock()

    _ensure_resolv_conf_symlink(target, logger)

    assert any(
        cmd == f"ln -sf {RESOLVED_STUB_PATH} {RESOLV_CONF_PATH}"
        for cmd in target.run_log
    )


def test_resolv_conf_symlink_repaired_when_pointing_elsewhere() -> None:
    """A symlink pointing at the wrong target is repaired."""
    from agentworks.vms.tailscale_dns import (
        RESOLV_CONF_PATH,
        RESOLVED_STUB_PATH,
        _ensure_resolv_conf_symlink,
    )

    target = _make_dns_fix_target(resolv_link_target="/run/some/other/path.conf")
    logger = MagicMock()

    _ensure_resolv_conf_symlink(target, logger)

    assert any(
        cmd == f"ln -sf {RESOLVED_STUB_PATH} {RESOLV_CONF_PATH}"
        for cmd in target.run_log
    )


# -- tailscaled drop-in --------------------------------------------------------


def test_dropin_written_when_missing() -> None:
    """First-time install: stage the file, install it, then daemon-reload."""
    from agentworks.vms.tailscale_dns import (
        TAILSCALED_DROPIN_CONTENT,
        TAILSCALED_DROPIN_DIR,
        TAILSCALED_DROPIN_PATH,
        _ensure_tailscaled_dropin,
    )

    target = _make_dns_fix_target(dropin_content=None)
    logger = MagicMock()

    _ensure_tailscaled_dropin(target, logger)

    # write_file called with the canonical content
    assert any(content == TAILSCALED_DROPIN_CONTENT for _, content in target.write_log)
    # drop-in directory ensured, install -m 0644 -> drop-in path, and daemon-reload all happened
    assert any(cmd == f"install -d -m 0755 -o root -g root {TAILSCALED_DROPIN_DIR}" for cmd in target.run_log)
    assert any("install -m 0644" in cmd and TAILSCALED_DROPIN_PATH in cmd for cmd in target.run_log)
    assert any(cmd == "systemctl daemon-reload" for cmd in target.run_log)


def test_dropin_noop_when_content_matches() -> None:
    """If the drop-in already matches, no write, no install, no reload."""
    from agentworks.vms.tailscale_dns import (
        TAILSCALED_DROPIN_CONTENT,
        TAILSCALED_DROPIN_PATH,
        _ensure_tailscaled_dropin,
    )

    target = _make_dns_fix_target(dropin_content=TAILSCALED_DROPIN_CONTENT)
    logger = MagicMock()

    _ensure_tailscaled_dropin(target, logger)

    assert target.write_log == []
    assert not any("install -m 0644" in cmd and TAILSCALED_DROPIN_PATH in cmd for cmd in target.run_log)
    assert not any(cmd == "systemctl daemon-reload" for cmd in target.run_log)


def test_dropin_rewritten_when_content_differs() -> None:
    """A stale drop-in (e.g. older managed version) is rewritten."""
    from agentworks.vms.tailscale_dns import (
        TAILSCALED_DROPIN_CONTENT,
        TAILSCALED_DROPIN_PATH,
        _ensure_tailscaled_dropin,
    )

    target = _make_dns_fix_target(dropin_content="# old content\n[Unit]\nAfter=resolved\n")
    logger = MagicMock()

    _ensure_tailscaled_dropin(target, logger)

    assert any(content == TAILSCALED_DROPIN_CONTENT for _, content in target.write_log)
    assert any("install -m 0644" in cmd and TAILSCALED_DROPIN_PATH in cmd for cmd in target.run_log)
    assert any(cmd == "systemctl daemon-reload" for cmd in target.run_log)


def test_dropin_does_not_restart_tailscaled() -> None:
    """Phase B runs over the tailnet; we must never restart tailscaled here."""
    from agentworks.vms.tailscale_dns import _ensure_tailscaled_dropin

    target = _make_dns_fix_target(dropin_content=None)
    logger = MagicMock()

    _ensure_tailscaled_dropin(target, logger)

    assert not any("restart tailscaled" in cmd or "restart tailscaled.service" in cmd for cmd in target.run_log)


# -- top-level apply ------------------------------------------------------------


def test_apply_invokes_all_three_steps() -> None:
    """End-to-end smoke: enable, symlink, drop-in install all visible in run_log."""
    from agentworks.vms.tailscale_dns import (
        RESOLV_CONF_PATH,
        RESOLVED_STUB_PATH,
        TAILSCALED_DROPIN_PATH,
        apply_tailscaled_dns_fix,
    )

    # Start from a fully-broken state so every step has something to do.
    target = _make_dns_fix_target(resolv_link_target=None, dropin_content=None)
    logger = MagicMock()

    apply_tailscaled_dns_fix(target, logger)

    assert any(cmd == "systemctl enable --now systemd-resolved" for cmd in target.run_log)
    assert any(
        cmd == f"ln -sf {RESOLVED_STUB_PATH} {RESOLV_CONF_PATH}"
        for cmd in target.run_log
    )
    assert any("install -m 0644" in cmd and TAILSCALED_DROPIN_PATH in cmd for cmd in target.run_log)
    assert any(cmd == "systemctl daemon-reload" for cmd in target.run_log)
