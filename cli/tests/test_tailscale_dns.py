"""Tests for the tailscaled cold-boot startup-ordering fix.

See ``agentworks.vms.tailscale_dns`` and GitHub issue #117 for the
root-cause analysis. These tests cover the drop-in's idempotency, the
specific systemd-unit semantics that make the fix correct, and the
invariant that we never restart tailscaled in phase B.
"""

from __future__ import annotations

from unittest.mock import MagicMock


def _make_dns_fix_target(*, dropin_content: str | None = None) -> MagicMock:
    """ExecTarget mock parameterized by current drop-in content.

    - ``dropin_content=None``: the drop-in file is absent (``cat`` exits non-zero).
    - ``dropin_content="..."``: ``cat`` returns this content.
    """
    target = MagicMock()
    write_log: list[tuple[str, str]] = []
    run_log: list[str] = []
    target.write_log = write_log
    target.run_log = run_log

    from agentworks.vms.tailscale_dns import TAILSCALED_DROPIN_PATH

    def run_side_effect(cmd, **kwargs):  # noqa: ANN001 -- mock side_effect signature
        run_log.append(cmd)
        result = MagicMock()
        result.stderr = ""
        if cmd == f"cat {TAILSCALED_DROPIN_PATH}":
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


# -- Drop-in content semantics (the load-bearing piece of the fix) ------------


def test_dropin_content_orders_after_network_online() -> None:
    """The drop-in must order tailscaled after network-online.target.

    This is the actual race fix; if this assertion ever flips, the
    cold-boot DNS race comes back.
    """
    from agentworks.vms.tailscale_dns import TAILSCALED_DROPIN_CONTENT

    assert "After=network-online.target" in TAILSCALED_DROPIN_CONTENT
    assert "Wants=network-online.target" in TAILSCALED_DROPIN_CONTENT


def test_dropin_content_includes_nss_lookup_target() -> None:
    """nss-lookup.target is the passive sync point for NSS-providing
    resolvers (systemd-resolved, NetworkManager, etc.), so ordering
    after it catches D-Bus-readiness even when the per-resolver After=
    on the unit isn't sufficient."""
    from agentworks.vms.tailscale_dns import TAILSCALED_DROPIN_CONTENT

    assert "nss-lookup.target" in TAILSCALED_DROPIN_CONTENT


def test_dropin_content_does_not_use_requires() -> None:
    """Requires= would take tailscaled (and our SSH transport) down if
    network-online.target ever fails to fire. Wants= degrades to the
    pre-fix behavior instead, which is recoverable. This contract is
    documented in the module docstring and is the reason for the
    deliberate choice; lock it in."""
    from agentworks.vms.tailscale_dns import TAILSCALED_DROPIN_CONTENT

    # Check actual systemd directives, not comment text. A comment line
    # like "# Wants= (not Requires=) ..." should not flip this test.
    directive_lines = [
        ln for ln in TAILSCALED_DROPIN_CONTENT.splitlines()
        if ln and not ln.lstrip().startswith("#")
    ]
    assert not any("Requires=" in ln for ln in directive_lines)


# -- Drop-in install / idempotency --------------------------------------------


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

    assert any(content == TAILSCALED_DROPIN_CONTENT for _, content in target.write_log)
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
    """A stale drop-in (e.g. an earlier managed version) is rewritten."""
    from agentworks.vms.tailscale_dns import (
        TAILSCALED_DROPIN_CONTENT,
        TAILSCALED_DROPIN_PATH,
        _ensure_tailscaled_dropin,
    )

    # An earlier version of this fix ordered against systemd-resolved
    # specifically. Confirm that drop-in is recognized as stale and rewritten.
    stale = "[Unit]\nAfter=systemd-resolved.service\nWants=systemd-resolved.service\n"
    target = _make_dns_fix_target(dropin_content=stale)
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

    assert not any("restart tailscaled" in cmd for cmd in target.run_log)


# -- Top-level apply: non-fatal contract --------------------------------------


def test_apply_invokes_dropin_install() -> None:
    """End-to-end smoke: the drop-in install is visible in the run_log."""
    from agentworks.vms.tailscale_dns import (
        TAILSCALED_DROPIN_PATH,
        apply_tailscaled_dns_fix,
    )

    target = _make_dns_fix_target(dropin_content=None)
    logger = MagicMock()

    apply_tailscaled_dns_fix(target, logger)

    assert any("install -m 0644" in cmd and TAILSCALED_DROPIN_PATH in cmd for cmd in target.run_log)
    assert any(cmd == "systemctl daemon-reload" for cmd in target.run_log)


def test_apply_swallows_ssherror_and_warns() -> None:
    """The apply function must not propagate SSHError; failures warn and
    continue, matching the rest of phase B's contract."""
    from agentworks.ssh import SSHError
    from agentworks.vms.tailscale_dns import apply_tailscaled_dns_fix

    target = MagicMock()
    target.run.side_effect = SSHError("simulated failure")
    logger = MagicMock()

    # Must not raise.
    apply_tailscaled_dns_fix(target, logger)

    # The non-fatal contract: a warning was emitted via the logger.
    assert logger.warning.called
