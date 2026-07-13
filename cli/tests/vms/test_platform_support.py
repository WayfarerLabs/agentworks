"""Host-support gating: platforms self-report whether they can run on
this host (``unsupported_reason`` gates registration wholesale;
``bundled_site_unsupported_reason`` gates only the zero-config bundled
site), the knowledge lives on the platform class, and every surface
(registry, bundled sites, errors, doctor) derives from those two calls.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentworks.bootstrap import build_registry
from agentworks.capabilities.vm_platform.lima import LimaPlatform
from agentworks.capabilities.vm_platform.wsl2 import WSL2Platform
from agentworks.config import load_config
from agentworks.errors import ConfigError


@pytest.fixture
def make_config(tmp_path: Path):
    key = tmp_path / "id_ed25519"
    key.write_text("private")
    (tmp_path / "id_ed25519.pub").write_text("public")

    def _make(extra: str = "", *, resources: str | None = None):
        path = tmp_path / "config.toml"
        path.write_text(
            f'[operator]\nssh_public_key = "{key}.pub"\nssh_private_key = "{key}"\n'
            + extra
        )
        if resources is not None:
            rdir = tmp_path / "resources"
            rdir.mkdir(exist_ok=True)
            (rdir / "sites.yaml").write_text(resources)
        return load_config(path, warn_issues=False, warn_deprecations=False)

    return _make


def _support(
    monkeypatch: pytest.MonkeyPatch,
    *,
    wsl2: str | None,
    lima_bundled: str | None,
) -> None:
    """Pin the two host-dependent checks to explicit outcomes."""
    monkeypatch.setattr(
        WSL2Platform, "unsupported_reason", classmethod(lambda cls: wsl2)
    )
    monkeypatch.setattr(
        WSL2Platform,
        "bundled_site_unsupported_reason",
        classmethod(lambda cls: wsl2),
    )
    monkeypatch.setattr(
        LimaPlatform,
        "bundled_site_unsupported_reason",
        classmethod(lambda cls: lima_bundled),
    )


def test_supported_host_gets_both_bundled_sites(
    make_config, monkeypatch: pytest.MonkeyPatch
) -> None:
    _support(monkeypatch, wsl2=None, lima_bundled=None)
    registry = build_registry(make_config())
    sites = {name for name, _ in registry.iter_kind_items("vm-site")}
    assert {"lima-local", "wsl2"} <= sites
    platforms = {e.name for e in registry.iter_kind("vm-platform")}
    assert platforms == {"lima", "wsl2", "azure", "proxmox"}


def test_unsupported_platform_publishes_nothing(
    make_config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """wsl2 off Windows: no capability row, no bundled site -- invisible
    to the resource graph (doctor lists it from the code registry)."""
    _support(monkeypatch, wsl2="requires Windows", lima_bundled=None)
    registry = build_registry(make_config())
    sites = {name for name, _ in registry.iter_kind_items("vm-site")}
    assert "wsl2" not in sites
    assert "lima-local" in sites
    platforms = {e.name for e in registry.iter_kind("vm-platform")}
    assert "wsl2" not in platforms
    # lima the PLATFORM stays registered even when its bundled site is
    # host-gated: remote-Lima sites need nothing locally.
    assert "lima" in platforms


def test_bundled_site_gates_independently_of_the_platform(
    make_config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No local limactl: lima-local is absent but lima stays registered,
    so an operator-declared remote site still resolves."""
    _support(
        monkeypatch, wsl2="requires Windows", lima_bundled="limactl is not installed"
    )
    config = make_config(
        resources=(
            "apiVersion: agentworks/v1\n"
            "kind: vm-site\n"
            "metadata:\n"
            "  name: gpu-box\n"
            "spec:\n"
            "  platform: lima\n"
            "  platform_config:\n"
            "    vm_host: me@box\n"
        )
    )
    registry = build_registry(config)
    sites = {name for name, _ in registry.iter_kind_items("vm-site")}
    assert "lima-local" not in sites
    assert "gpu-box" in sites


def test_declared_site_on_unsupported_platform_fails_helpfully(
    make_config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pre-finalize guard: the error names the platform's stated
    requirement, not the framework's generic reference miss."""
    _support(monkeypatch, wsl2="requires Windows (runs VMs as WSL2 distributions)", lima_bundled=None)
    config = make_config(
        resources=(
            "apiVersion: agentworks/v1\n"
            "kind: vm-site\n"
            "metadata:\n"
            "  name: my-wsl\n"
            "spec:\n"
            "  platform: wsl2\n"
        )
    )
    with pytest.raises(ConfigError, match="disabled on this host") as exc:
        build_registry(config)
    assert "requires Windows" in str(exc.value)
    assert "my-wsl" in str(exc.value)


def test_doctor_lists_installed_platforms_with_reasons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentworks import doctor

    _support(monkeypatch, wsl2="requires Windows", lima_bundled="limactl is not installed")

    group = doctor._check_vm_platforms()
    by_name = {c.name: c for c in group.checks}
    assert by_name["platform: wsl2"].status is doctor.Status.INFO
    assert "requires Windows" in (by_name["platform: wsl2"].message or "")
    lima_row = by_name["platform: lima"]
    assert lima_row.status is doctor.Status.OK
    assert "lima-local" in (lima_row.message or "")
    assert by_name["platform: azure"].status is doctor.Status.OK
