"""``_is_tailscale_reachable``: the Tailscale power-state fast path's
reachability probe. A missing ``tailscale`` binary is a setup problem
that silently buys a cloud round trip on every gated command, so it
degrades to False loudly (one warn per process) rather than in silence;
a genuine ping timeout stays a silent False."""

from __future__ import annotations

import subprocess

import pytest

from agentworks.vms import manager as vm_manager


def _warn_counter(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Capture output.warn calls the manager makes, and reset the
    once-per-process guard so the test sees the first-warn behavior."""
    warns: list[str] = []
    monkeypatch.setattr(vm_manager, "_warned_tailscale_missing", False)
    monkeypatch.setattr("agentworks.output.warn", lambda msg, *a, **k: warns.append(msg))
    return warns


def test_missing_binary_warns_once_and_degrades(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing binary (FileNotFoundError) returns False both calls but
    warns exactly once, naming the cause and the degraded fast path."""
    warns = _warn_counter(monkeypatch)

    def _no_binary(*_a: object, **_k: object) -> object:
        raise FileNotFoundError("tailscale")

    monkeypatch.setattr(subprocess, "run", _no_binary)

    assert vm_manager._is_tailscale_reachable("100.64.0.1") is False
    assert vm_manager._is_tailscale_reachable("100.64.0.1") is False

    assert len(warns) == 1
    assert "tailscale binary not found" in warns[0]


def test_timeout_degrades_silently(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ping timeout is transient, not a setup problem: return False
    without warning."""
    warns = _warn_counter(monkeypatch)

    def _timeout(*_a: object, **_k: object) -> object:
        raise subprocess.TimeoutExpired(cmd="tailscale", timeout=10)

    monkeypatch.setattr(subprocess, "run", _timeout)

    assert vm_manager._is_tailscale_reachable("100.64.0.1") is False
    assert warns == []
