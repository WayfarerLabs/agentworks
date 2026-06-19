"""Tests for the tailscaled cold-boot startup-ordering fix and the VM DNS
health check that gates phase B against DNS-dependent steps.

See ``agentworks.vms.tailscale_dns`` and GitHub issue #117 for the
root-cause analysis. These tests cover:

- the drop-in's idempotency and the specific systemd-unit semantics that
  make the cold-boot ordering fix correct
- the invariant that we never restart tailscaled in phase B
- the DNS health check's per-branch behavior (silent on healthy, warn
  on broken-but-not-#117, warn on #117-shape-without-known-heal, raise
  with heal hint on the full #117 latch)
- the ordering invariant (DNS probe before any diagnostic command) and
  the read-only invariant (no writes from any branch, ever)
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


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


# -- Latched-state detection --------------------------------------------------


def _make_latch_target(
    *,
    resolv_readable: bool = True,
    resolv_is_tailscaled: bool = True,
    dns_probe_ok: bool = False,
    resolved_active: bool = True,
) -> MagicMock:
    """ExecTarget mock parameterized by the four signals the detector reads.

    Defaults describe a latched VM with systemd-resolved as the platform
    resolver, which is the case where detection should raise.
    """
    target = MagicMock()
    run_log: list[str] = []
    target.run_log = run_log

    tailscaled_resolv = (
        "# resolv.conf(5) file generated by tailscale\n"
        "nameserver 100.100.100.100\n"
    )
    foreign_resolv = "nameserver 192.168.1.1\n"

    def run_side_effect(cmd, **kwargs):  # noqa: ANN001 -- mock side_effect signature
        run_log.append(cmd)
        result = MagicMock()
        result.stderr = ""
        if cmd == "cat /etc/resolv.conf":
            if not resolv_readable:
                result.returncode = 1
                result.ok = False
                result.stdout = ""
            elif resolv_is_tailscaled:
                result.returncode = 0
                result.ok = True
                result.stdout = tailscaled_resolv
            else:
                result.returncode = 0
                result.ok = True
                result.stdout = foreign_resolv
        elif cmd.startswith("getent hosts"):
            result.returncode = 0 if dns_probe_ok else 2
            result.ok = dns_probe_ok
            result.stdout = "1.2.3.4 example.com\n" if dns_probe_ok else ""
        elif cmd == "systemctl is-active --quiet systemd-resolved":
            result.returncode = 0 if resolved_active else 3
            result.ok = resolved_active
            result.stdout = ""
        else:
            result.returncode = 0
            result.ok = True
            result.stdout = ""
        return result

    target.run.side_effect = run_side_effect
    return target


def test_check_silent_when_dns_works() -> None:
    """The DNS probe is the gate. Working DNS = healthy VM = return silently
    without digging into resolv.conf or the platform resolver."""
    from agentworks.vms.tailscale_dns import check_vm_dns

    target = _make_latch_target(dns_probe_ok=True)
    logger = MagicMock()
    check_vm_dns(target, logger)  # must not raise

    # Happy path stops at the DNS probe. No further diagnostic commands.
    assert not any(cmd.startswith("cat ") for cmd in target.run_log)
    assert not any(cmd.startswith("systemctl is-active") for cmd in target.run_log)
    # And no warning was logged: silence on the happy path is intentional.
    assert not logger.warning.called


def test_check_warns_when_dns_fails_and_resolv_unreadable() -> None:
    """DNS broken AND /etc/resolv.conf unreadable: we can't diagnose, but
    DNS is still broken so subsequent apt steps will fail. Warn so the
    operator has a visible link to the failure that follows."""
    from agentworks.vms.tailscale_dns import check_vm_dns

    target = _make_latch_target(resolv_readable=False)
    logger = MagicMock()
    check_vm_dns(target, logger)  # must not raise

    assert logger.warning.called
    warning_text = " ".join(call.args[0] for call in logger.warning.call_args_list)
    assert "DNS" in warning_text  # the operator needs to see what we saw
    # We bailed before checking the platform resolver. Nothing further to read.
    assert not any(cmd.startswith("systemctl is-active") for cmd in target.run_log)


def test_check_warns_when_dns_fails_but_resolv_not_tailscaled() -> None:
    """DNS broken AND /etc/resolv.conf isn't tailscaled-managed: not the
    known issue #117 shape. Warn so the operator can investigate before
    apt fails cryptically; the known heal doesn't apply."""
    from agentworks.vms.tailscale_dns import check_vm_dns

    target = _make_latch_target(resolv_is_tailscaled=False)
    logger = MagicMock()
    check_vm_dns(target, logger)  # must not raise

    assert logger.warning.called
    warning_text = " ".join(call.args[0] for call in logger.warning.call_args_list)
    assert "DNS" in warning_text
    # We didn't gate on resolved: the issue #117 shape didn't match.
    assert not any(cmd.startswith("systemctl is-active") for cmd in target.run_log)


def test_check_warns_when_resolved_not_active() -> None:
    """DNS broken AND /etc/resolv.conf is tailscaled-managed BUT resolved
    isn't the active resolver: we recognize the issue #117 shape but the
    heal we'd suggest doesn't apply to this platform. Surface a warning
    that says so rather than raising with a hint we know would be wrong."""
    from agentworks.vms.tailscale_dns import check_vm_dns

    target = _make_latch_target(resolved_active=False)
    logger = MagicMock()
    check_vm_dns(target, logger)  # must not raise

    assert logger.warning.called
    warning_text = " ".join(call.args[0] for call in logger.warning.call_args_list)
    # When we do recognize the shape, the warning should name it AND
    # explicitly call out that no heal is implemented for this resolver.
    assert "latched" in warning_text or "issue #117" in warning_text
    assert "not implemented" in warning_text or "No heal" in warning_text


def test_check_raises_state_error_with_heal_hint() -> None:
    """All issue #117 signals present (DNS broken, resolv.conf
    tailscaled-managed, resolved active): raise with the manual heal block."""
    import pytest

    from agentworks.errors import StateError
    from agentworks.vms.tailscale_dns import check_vm_dns

    target = _make_latch_target()  # defaults describe the latched state

    with pytest.raises(StateError) as exc_info:
        check_vm_dns(target, MagicMock())

    err = exc_info.value
    assert err.entity_kind == "vm"
    hint = err.hint or ""
    # The hint must contain the actual heal commands an operator pastes:
    assert "systemctl stop tailscaled" in hint
    assert "ln -sf /run/systemd/resolve/stub-resolv.conf" in hint
    assert "systemctl start tailscaled" in hint
    # And must direct the operator at the provisioner-shell entry point
    # so they don't kill their Tailscale-SSH session running the first
    # command. The platform-native transport survives stopping
    # tailscaled; a future contributor who drops this reference back to
    # the simple-block form re-introduces the lockout footgun.
    assert "vm shell --provisioner" in hint


def test_check_dns_probe_runs_first() -> None:
    """Ordering invariant: the DNS probe is the gate. On any failing path,
    getent must be the first command issued; everything else is
    diagnosis-of-failure that only matters once we know DNS is broken."""
    from agentworks.vms.tailscale_dns import check_vm_dns

    # Use the all-latched defaults so we walk the full diagnosis chain.
    target = _make_latch_target()
    import contextlib

    from agentworks.errors import StateError

    with contextlib.suppress(StateError):
        check_vm_dns(target, MagicMock())

    assert target.run_log, "expected at least one command"
    assert target.run_log[0].startswith("getent hosts"), (
        f"expected DNS probe first, got: {target.run_log[0]!r}"
    )


@pytest.mark.parametrize(
    ("case", "kwargs"),
    [
        ("resolv_unreadable", {"resolv_readable": False}),
        ("resolv_not_tailscaled", {"resolv_is_tailscaled": False}),
        ("dns_works", {"dns_probe_ok": True}),
        ("resolved_not_active", {"resolved_active": False}),
        ("latched", {}),  # defaults describe the all-signals-match latched state
    ],
)
def test_check_does_not_modify_anything(case: str, kwargs: dict[str, bool]) -> None:
    """The DNS check is read-only across every branch: no writes, no service
    touches, no daemon-reloads, no file restorations. The operator decides
    whether and how to heal; the check only surfaces the diagnosis.

    Parametrized across all five branches (four no-raise paths plus the
    latched/raise path) so any future contributor who adds a side effect
    in any branch trips this regardless of which branch they touched.

    Uses an allow-list shape (every command must start with one of three
    known read-only prefixes) rather than a deny-list. A deny-list would
    only catch the specific mutation verbs we thought to enumerate; a
    contributor could slip ``mv``, ``cp``, ``chmod``, ``sed -i``, ``tee``,
    or a ``sh -c "... > /etc/foo"`` past it. The allow-list catches any
    new command shape regardless of how it would mutate state.
    """
    import contextlib

    from agentworks.errors import StateError
    from agentworks.vms.tailscale_dns import check_vm_dns

    target = _make_latch_target(**kwargs)
    with contextlib.suppress(StateError):
        check_vm_dns(target, MagicMock())

    # No file writes via either entry point. write_file is the documented
    # path; copy_to is the lower-level primitive write_file delegates to.
    # Asserting both pins down the contract regardless of which API a
    # future contributor reaches for.
    target.write_file.assert_not_called()
    target.copy_to.assert_not_called()

    # Allow-list: detection should only ever issue these three read-only
    # command shapes. Any other command shape (even one that looks
    # read-only) is a contract change that warrants explicit review.
    allowed_prefixes = ("cat ", "getent hosts ", "systemctl is-active ")
    for cmd in target.run_log:
        assert cmd.startswith(allowed_prefixes), (
            f"[{case}] detection issued a command outside the read-only "
            f"allow-list: {cmd!r}"
        )
