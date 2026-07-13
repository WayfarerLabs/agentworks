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


# -- The real classmethods (both branches, deterministically) ----------------


def test_wsl2_unsupported_reason_is_the_real_os_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the real check, not a stub: off Windows the platform
    gates wholesale; on Windows it is supported."""
    import sys

    monkeypatch.setattr(sys, "platform", "darwin")
    reason = WSL2Platform.unsupported_reason()
    assert reason is not None
    assert "Windows" in reason

    monkeypatch.setattr(sys, "platform", "win32")
    assert WSL2Platform.unsupported_reason() is None


def test_wsl2_bundled_site_additionally_needs_wsl_exe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr("shutil.which", lambda name: None)
    reason = WSL2Platform.bundled_site_unsupported_reason()
    assert reason is not None
    assert "wsl.exe" in reason

    monkeypatch.setattr("shutil.which", lambda name: "/x/wsl")
    assert WSL2Platform.bundled_site_unsupported_reason() is None


def test_lima_support_split_is_real(monkeypatch: pytest.MonkeyPatch) -> None:
    """lima the platform is supported everywhere (remote sites run
    limactl on the vm_host); only the bundled local site needs the
    local tool."""
    assert LimaPlatform.unsupported_reason() is None

    monkeypatch.setattr("shutil.which", lambda name: None)
    reason = LimaPlatform.bundled_site_unsupported_reason()
    assert reason is not None
    assert "limactl" in reason

    monkeypatch.setattr("shutil.which", lambda name: "/x/limactl")
    assert LimaPlatform.bundled_site_unsupported_reason() is None


def test_defaults_site_on_unavailable_bundled_site_names_the_requirement(
    make_config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """validate_sites gets the same bundled-miss treatment as
    lookup_site: `defaults.site = "lima-local"` with limactl missing
    must say "install limactl", never "declare a site named lima-local"
    (a reserved name)."""
    _support(
        monkeypatch, wsl2="requires Windows", lima_bundled="limactl is not installed"
    )
    config = make_config('[defaults]\nsite = "lima-local"\n')
    with pytest.raises(ConfigError, match="unavailable on this host") as exc:
        build_registry(config)
    assert "limactl" in str(exc.value)
    assert "declare a vm-site" not in (exc.value.hint or "")


def test_bundled_site_names_are_reserved_even_when_unavailable(
    make_config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An operator cannot squat lima-local on a limactl-less host: the
    name is reserved unconditionally, or installing the tool later
    would collide with VMs pointing at a name whose meaning changed."""
    _support(
        monkeypatch, wsl2="requires Windows", lima_bundled="limactl is not installed"
    )
    config = make_config(
        resources=(
            "apiVersion: agentworks/v1\n"
            "kind: vm-site\n"
            "metadata:\n"
            "  name: lima-local\n"
            "spec:\n"
            "  platform: lima\n"
        )
    )
    with pytest.raises(ConfigError, match="reserved bundled-site name"):
        build_registry(config)
