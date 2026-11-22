"""Tests for the tailscaled cold-boot startup-ordering fix and the VM DNS
health check that gates phase B against DNS-dependent steps.

See ``agentworks.vms.tailscale_dns`` and GitHub issue #117 for the
root-cause analysis. These tests cover:

- the drop-in's idempotency and the specific systemd-unit semantics that
  make the cold-boot ordering fix correct
- the invariant that we never restart tailscaled in phase B
- the DNS health check's per-branch behavior (silent on healthy, warn
  on broken-but-not-#117, warn on #117-shape-without-known-heal, raise
  on the full #117 latch, warn when a probe cannot run)
- the ordering invariant (DNS probe before any diagnostic command) and
  the read-only invariant (no writes from any branch, ever)

The drop-in runs over a recording admin access whose file operations
publish to the test's own paths (``tests/phase_b_support.py``).
"""

from __future__ import annotations

import contextlib
import stat
from pathlib import Path

import pytest

from agentworks.errors import StateError
from agentworks.execution.errors import CommandFailed, ExecutionIncomplete
from agentworks.execution.requests import Command
from agentworks.execution.results import Completed, Result, Unknown, UnknownReason
from agentworks.vms import tailscale_dns
from tests.phase_b_support import StepGuest, requires_file_program

RELOAD = Command(("systemctl", "daemon-reload"), elevated=True, deadline=60)
DNS_PROBE = Command(("getent", "hosts", "deb.debian.org"), deadline=60)
READ_RESOLV = Command(("cat", "/etc/resolv.conf"), deadline=60)
RESOLVED_ACTIVE = Command(("systemctl", "is-active", "--quiet", "systemd-resolved"), deadline=60)


@pytest.fixture
def guest(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> StepGuest:
    directory = tmp_path / "tailscaled.service.d"
    monkeypatch.setattr(tailscale_dns, "TAILSCALED_DROPIN_DIR", str(directory))
    monkeypatch.setattr(tailscale_dns, "TAILSCALED_DROPIN_PATH", str(directory / "10-after-network-online.conf"))
    return StepGuest()


def _dropin() -> Path:
    return Path(tailscale_dns.TAILSCALED_DROPIN_PATH)


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
    directive_lines = [ln for ln in TAILSCALED_DROPIN_CONTENT.splitlines() if ln and not ln.lstrip().startswith("#")]
    assert not any("Requires=" in ln for ln in directive_lines)


# -- Drop-in install / idempotency --------------------------------------------


@requires_file_program
def test_dropin_written_with_its_directory_and_reloaded_when_missing(guest: StepGuest) -> None:
    tailscale_dns.apply_tailscaled_dns_fix(guest.files, guest.log)

    assert _dropin().read_text() == tailscale_dns.TAILSCALED_DROPIN_CONTENT
    assert stat.S_IMODE(_dropin().stat().st_mode) == 0o644
    assert stat.S_IMODE(_dropin().parent.stat().st_mode) == 0o755
    assert guest.commands() == [RELOAD]
    assert guest.log.warned == []


@requires_file_program
def test_dropin_that_matches_is_neither_written_nor_reloaded(guest: StepGuest) -> None:
    _dropin().parent.mkdir()
    _dropin().write_text(tailscale_dns.TAILSCALED_DROPIN_CONTENT)

    tailscale_dns.apply_tailscaled_dns_fix(guest.files, guest.log)

    assert guest.writes() == []
    assert guest.commands() == []


@requires_file_program
def test_dropin_that_differs_is_rewritten_and_reloaded(guest: StepGuest) -> None:
    _dropin().parent.mkdir()
    _dropin().write_text("[Unit]\nAfter=network.target\n")

    tailscale_dns.apply_tailscaled_dns_fix(guest.files, guest.log)

    assert _dropin().read_text() == tailscale_dns.TAILSCALED_DROPIN_CONTENT
    assert guest.commands() == [RELOAD]


@requires_file_program
def test_dropin_never_restarts_tailscaled(guest: StepGuest) -> None:
    """Phase B runs over the tailnet: restarting tailscaled would cut the connection mid-init."""
    tailscale_dns.apply_tailscaled_dns_fix(guest.files, guest.log)

    assert all("tailscaled" not in command.argv for command in guest.commands())  # type: ignore[union-attr]


# -- Top-level apply: non-fatal contract --------------------------------------


@requires_file_program
def test_a_failed_reload_restores_the_prior_dropin_and_the_next_run_retries(guest: StepGuest) -> None:
    _dropin().parent.mkdir()
    _dropin().write_text("[Unit]\nAfter=network.target\n")
    guest.respond(RELOAD, CommandFailed("box", 1, Result(Completed(1)), streams_logged=True))

    tailscale_dns.apply_tailscaled_dns_fix(guest.files, guest.log)

    assert _dropin().read_text() == "[Unit]\nAfter=network.target\n"
    guest.access.responses.clear()
    guest.access.calls.clear()

    tailscale_dns.apply_tailscaled_dns_fix(guest.files, guest.log)

    assert _dropin().read_text() == tailscale_dns.TAILSCALED_DROPIN_CONTENT
    assert guest.commands() == [RELOAD]


@requires_file_program
def test_a_failed_reload_warns_without_raising(guest: StepGuest) -> None:
    guest.respond(RELOAD, CommandFailed("box", 1, Result(Completed(1)), streams_logged=True))

    tailscale_dns.apply_tailscaled_dns_fix(guest.files, guest.log)

    assert len(guest.log.warned) == 1


# -- Latched-state detection --------------------------------------------------


def _latch_guest(
    *,
    resolv_readable: bool = True,
    resolv_is_tailscaled: bool = True,
    dns_probe_ok: bool = False,
    resolved_active: bool = True,
) -> StepGuest:
    """A guest answering the four signals the detector reads.

    Defaults describe a latched VM with systemd-resolved as the platform
    resolver, which is the case where detection should raise.
    """
    guest = StepGuest(local_files=False)
    resolv = (
        b"# resolv.conf(5) file generated by tailscale\nnameserver 100.100.100.100\n"
        if resolv_is_tailscaled
        else b"nameserver 192.168.1.1\n"
    )
    guest.respond(DNS_PROBE, Result(Completed(0 if dns_probe_ok else 2)))
    guest.respond(READ_RESOLV, Result(Completed(0), resolv) if resolv_readable else Result(Completed(1)))
    guest.respond(RESOLVED_ACTIVE, Result(Completed(0 if resolved_active else 3)))
    return guest


def test_check_silent_when_dns_works() -> None:
    """Working DNS = healthy VM: return silently without reading resolv.conf or the resolver."""
    guest = _latch_guest(dns_probe_ok=True)

    tailscale_dns.check_vm_dns(guest.files, guest.log)

    assert guest.commands() == [DNS_PROBE]
    assert guest.log.warned == []


@pytest.mark.parametrize(
    ("case", "signals", "probes"),
    [
        ("resolv_unreadable", {"resolv_readable": False}, [DNS_PROBE, READ_RESOLV]),
        ("resolv_not_tailscaled", {"resolv_is_tailscaled": False}, [DNS_PROBE, READ_RESOLV]),
        ("resolved_not_active", {"resolved_active": False}, [DNS_PROBE, READ_RESOLV, RESOLVED_ACTIVE]),
    ],
)
def test_check_warns_without_raising_when_dns_is_broken_but_the_heal_does_not_fit(
    case: str, signals: dict[str, bool], probes: list[Command]
) -> None:
    guest = _latch_guest(**signals)

    tailscale_dns.check_vm_dns(guest.files, guest.log)

    assert len(guest.log.warned) == 1, case
    assert guest.commands() == probes


def test_check_raises_state_error_on_the_full_latch() -> None:
    """All issue #117 signals present (DNS broken, resolv.conf tailscaled-managed, resolved active)."""
    guest = _latch_guest()

    with pytest.raises(StateError) as exc_info:
        tailscale_dns.check_vm_dns(guest.files, guest.log)

    assert exc_info.value.entity_kind == "vm"
    assert exc_info.value.hint
    assert guest.commands() == [DNS_PROBE, READ_RESOLV, RESOLVED_ACTIVE]


@pytest.mark.parametrize(
    "failure",
    [
        ExecutionIncomplete("box", Result(Unknown(UnknownReason.DEADLINE))),
        ExecutionIncomplete("box", Result(Unknown(UnknownReason.CONNECTION))),
    ],
    ids=["deadline", "connection"],
)
def test_a_probe_that_cannot_run_warns_that_dns_was_not_checked(failure: Exception) -> None:
    guest = _latch_guest()
    guest.respond(READ_RESOLV, failure)

    tailscale_dns.check_vm_dns(guest.files, guest.log)

    assert len(guest.log.warned) == 1


@pytest.mark.parametrize(
    ("case", "signals"),
    [
        ("resolv_unreadable", {"resolv_readable": False}),
        ("resolv_not_tailscaled", {"resolv_is_tailscaled": False}),
        ("dns_works", {"dns_probe_ok": True}),
        ("resolved_not_active", {"resolved_active": False}),
        ("latched", {}),  # defaults describe the all-signals-match latched state
    ],
)
def test_check_is_read_only_and_probes_dns_first(case: str, signals: dict[str, bool]) -> None:
    """The DNS check issues only its three read-only probes, DNS first, unelevated, in every branch.

    An allow-list (the exact requests) rather than a deny-list: any new
    request shape, however read-only it looks, is a contract change that
    warrants explicit review. No file operation runs.
    """
    guest = _latch_guest(**signals)

    with contextlib.suppress(StateError):
        tailscale_dns.check_vm_dns(guest.files, guest.log)

    requests = guest.access.requests()
    assert requests[0] == DNS_PROBE, case
    assert all(request in (DNS_PROBE, READ_RESOLV, RESOLVED_ACTIVE) for request in requests), case
