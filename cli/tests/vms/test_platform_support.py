"""The disabled-resource model for VM platforms and sites: platforms
self-report host support (``unsupported_reason`` gates the capability
row), and every vm-site, bundled and declared alike, registers
UNCONDITIONALLY and self-disables (the generic ``disabled_reason``)
when its platform is missing/host-disabled or the bound instance lacks
a local requirement. Disabled sites list and describe like any
resource; using one is a typed error naming the chain; references
degrade to doctor warnings instead of breaking every command.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentworks.bootstrap import build_registry
from agentworks.capabilities.vm_platform.lima import LimaPlatform
from agentworks.capabilities.vm_platform.wsl2 import WSL2Platform
from agentworks.config import load_config
from agentworks.errors import ConfigError, StateError, ValidationError
from agentworks.vms.sites import resolve_site, select_site, site_disabled_reason


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


_GPU_BOX = (
    "apiVersion: agentworks/v1\n"
    "kind: vm-site\n"
    "metadata:\n"
    "  name: gpu-box\n"
    "spec:\n"
    "  platform: lima\n"
    "  platform_config:\n"
    "    vm_host: me@box\n"
)


def _support(
    monkeypatch: pytest.MonkeyPatch,
    *,
    wsl2: str | None,
    lima_local: str | None,
) -> None:
    """Pin the two host-dependent checks to explicit outcomes.

    ``wsl2`` pins the platform-level gate; ``lima_local`` pins the
    instance-level requirement for LOCAL lima sites only (remote sites
    with a ``vm_host`` stay enabled, mirroring the real check).
    """
    monkeypatch.setattr(
        WSL2Platform, "unsupported_reason", classmethod(lambda cls: wsl2)
    )
    monkeypatch.setattr(WSL2Platform, "disabled_reason", lambda self: None)
    monkeypatch.setattr(
        LimaPlatform,
        "disabled_reason",
        lambda self: None if self.platform_config.get("vm_host") else lima_local,
    )


def test_every_site_registers_regardless_of_host(
    make_config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worst host (no Windows, no limactl) still registers both
    bundled sites; only the platform capability row is host-gated."""
    _support(monkeypatch, wsl2="Windows only", lima_local="limactl not installed")
    registry = build_registry(make_config())
    sites = dict(registry.iter_kind_items("vm-site"))
    assert {"lima-local", "wsl2"} <= set(sites)
    platforms = {e.name for e in registry.iter_kind("vm-platform")}
    assert "wsl2" not in platforms
    assert {"lima", "azure-vm", "proxmox"} <= platforms


def test_disabled_reasons_chain(
    make_config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A site's reason names the failing link: the platform gate for
    wsl2, the instance requirement for lima-local."""
    _support(monkeypatch, wsl2="Windows only", lima_local="limactl not installed")
    registry = build_registry(make_config())
    sites = dict(registry.iter_kind_items("vm-site"))
    assert site_disabled_reason(sites["lima-local"]) == "limactl not installed"
    assert (
        site_disabled_reason(sites["wsl2"])
        == "platform 'wsl2' is disabled: Windows only"
    )


def test_supported_host_has_everything_enabled(
    make_config, monkeypatch: pytest.MonkeyPatch
) -> None:
    _support(monkeypatch, wsl2=None, lima_local=None)
    registry = build_registry(make_config())
    for _, decl in registry.iter_kind_items("vm-site"):
        assert site_disabled_reason(decl) is None
    platforms = {e.name for e in registry.iter_kind("vm-platform")}
    assert platforms == {"lima", "wsl2", "azure-vm", "proxmox"}


def test_remote_lima_site_enabled_without_local_limactl(
    make_config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The load-bearing split: a remote-Lima site runs limactl on the
    vm_host over SSH, so only the LOCAL site disables."""
    _support(monkeypatch, wsl2="Windows only", lima_local="limactl not installed")
    registry = build_registry(make_config(resources=_GPU_BOX))
    sites = dict(registry.iter_kind_items("vm-site"))
    assert site_disabled_reason(sites["gpu-box"]) is None
    assert site_disabled_reason(sites["lima-local"]) is not None


def test_declared_site_on_disabled_platform_registers_disabled(
    make_config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A declared site whose platform is host-disabled no longer breaks
    every command (a resources dir shared across hosts degrades
    gracefully on the wrong host): it registers, disabled with the
    platform's reason, and only USING it errors."""
    _support(monkeypatch, wsl2="Windows only", lima_local=None)
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
    registry = build_registry(config)
    decl = dict(registry.iter_kind_items("vm-site"))["my-wsl"]
    assert site_disabled_reason(decl) == "platform 'wsl2' is disabled: Windows only"
    with pytest.raises(StateError, match="disabled on this host") as exc:
        resolve_site("my-wsl", registry)
    assert "Windows only" in str(exc.value)


def test_unknown_platform_site_registers_disabled(
    make_config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The plugin world for free: a site declared against a platform
    this build doesn't ship (plugin uninstalled, or a typo;
    indistinguishable by design) is a disabled site, not a registry
    error."""
    _support(monkeypatch, wsl2=None, lima_local=None)
    config = make_config(
        resources=(
            "apiVersion: agentworks/v1\n"
            "kind: vm-site\n"
            "metadata:\n"
            "  name: orbital\n"
            "spec:\n"
            "  platform: skynet\n"
        )
    )
    registry = build_registry(config)
    decl = dict(registry.iter_kind_items("vm-site"))["orbital"]
    assert site_disabled_reason(decl) == "platform 'skynet' is not installed"
    with pytest.raises(StateError, match="disabled on this host"):
        resolve_site("orbital", registry)


def test_bundled_site_names_are_reserved_on_every_host(
    make_config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bundled rows publish everywhere, so the registry's reserved
    override fires even on a host where the bundled site is disabled:
    an operator cannot squat lima-local on a limactl-less box."""
    _support(monkeypatch, wsl2="Windows only", lima_local="limactl not installed")
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
    with pytest.raises(ConfigError, match="reserved"):
        build_registry(config)


def test_defaults_site_naming_a_disabled_site_is_valid_config(
    make_config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The site exists, so defaults.site resolves; this host merely
    can't use it yet. Using it errors at resolve time and doctor warns
    on the reference; build_registry must NOT fail every command."""
    _support(monkeypatch, wsl2="Windows only", lima_local="limactl not installed")
    config = make_config('[defaults]\nsite = "lima-local"\n')
    registry = build_registry(config)  # no raise
    with pytest.raises(StateError, match="limactl not installed"):
        resolve_site("lima-local", registry)


def test_select_site_infers_over_enabled_sites_only(
    make_config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Disabled sites are not a choice, but their existence never
    breaks inference: the single enabled site wins."""
    _support(monkeypatch, wsl2="Windows only", lima_local="limactl not installed")
    registry = build_registry(make_config(resources=_GPU_BOX))
    assert select_site(None, None, registry) == "gpu-box"


def test_select_site_errors_with_reasons_when_all_disabled(
    make_config, monkeypatch: pytest.MonkeyPatch
) -> None:
    _support(monkeypatch, wsl2="Windows only", lima_local="limactl not installed")
    registry = build_registry(make_config())
    with pytest.raises(ValidationError, match="no vm-sites are enabled") as exc:
        select_site(None, None, registry)
    assert "limactl not installed" in str(exc.value)
    assert "Windows only" in str(exc.value)


def test_resource_layer_surfaces_disabled_state(
    make_config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`agw resource list` marks disabled rows and describe carries the
    reason: a disabled resource is still a resource."""
    from agentworks.resources.inspect import describe_resource, list_resources

    _support(monkeypatch, wsl2="Windows only", lima_local="limactl not installed")
    registry = build_registry(make_config())
    listing = list_resources(registry, kinds=("vm-site",))
    rows = {r.name: r for r in listing.rows}
    assert rows["lima-local"].disabled_reason == "limactl not installed"
    assert rows["wsl2"].disabled_reason == "platform 'wsl2' is disabled: Windows only"
    desc = describe_resource(registry, "vm-site", "wsl2")
    assert desc.disabled_reason == "platform 'wsl2' is disabled: Windows only"
    # Kinds without a disabled concept stay None (the no-op default).
    tmpl = describe_resource(registry, "vm-template", "default")
    assert tmpl.disabled_reason is None


def test_doctor_lists_platforms_and_disabled_sites(
    make_config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Platform rows carry only the platform-level state; per-site
    availability (lima-local without limactl) reports in the sites
    group where the site lives."""
    from agentworks import doctor

    _support(monkeypatch, wsl2="Windows only", lima_local="limactl not installed")

    group = doctor._check_vm_platforms()
    by_name = {c.name: c for c in group.checks}
    assert by_name["wsl2"].status is doctor.Status.INFO
    assert "Windows only" in (by_name["wsl2"].message or "")
    lima_row = by_name["lima"]
    assert lima_row.status is doctor.Status.OK
    assert lima_row.message is None  # the bundled-site note moved to VM sites
    assert by_name["azure-vm"].status is doctor.Status.OK


# -- The real methods (both branches, deterministically) ----------------------


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


def test_wsl2_site_additionally_needs_wsl_exe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The instance-level requirement on a supported host: wsl.exe is
    an optional Windows feature."""
    site = WSL2Platform("wsl2", {})

    monkeypatch.setattr("shutil.which", lambda name: None)
    reason = site.disabled_reason()
    assert reason is not None
    assert "wsl.exe" in reason

    monkeypatch.setattr("shutil.which", lambda name: "/x/wsl")
    assert site.disabled_reason() is None


def test_lima_disabled_reason_is_local_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """lima the platform is supported everywhere; the limactl
    requirement binds to LOCAL sites only (remote sites run limactl on
    the vm_host over SSH)."""
    assert LimaPlatform.unsupported_reason() is None

    monkeypatch.setattr("shutil.which", lambda name: None)
    local = LimaPlatform("lima-local", {})
    reason = local.disabled_reason()
    assert reason is not None
    assert "limactl" in reason
    remote = LimaPlatform("gpu-box", {"vm_host": "me@box"})
    assert remote.disabled_reason() is None

    monkeypatch.setattr("shutil.which", lambda name: "/x/limactl")
    assert LimaPlatform("lima-local", {}).disabled_reason() is None
