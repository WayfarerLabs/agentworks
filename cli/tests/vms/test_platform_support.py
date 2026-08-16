"""The readiness model for VM platforms and sites: platforms self-report
host support (``unsupported_reason`` feeds the platform node's folded
readiness), and every vm-site, bundled and declared alike, registers
UNCONDITIONALLY and folds to not-ready when its platform is host-disabled or
the bound config lacks a local requirement. The verdict is stored on the graph
and read via ``graph.readiness_of``. Not-ready sites remain marked in the
resource list, while doctor owns the diagnostic reason. Using one is a typed
error naming the chain; references degrade to doctor warnings instead of
breaking every command.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from agentworks.bootstrap import build_registry
from agentworks.capabilities.config import validate_capability_config
from agentworks.capabilities.vm_platform.lima import LimaPlatform
from agentworks.capabilities.vm_platform.wsl2 import WSL2Platform
from agentworks.config import load_config
from agentworks.errors import ConfigError, StateError, ValidationError
from agentworks.schema import RefOwner
from agentworks.vms.sites import resolve_site, select_site

if TYPE_CHECKING:
    from agentworks.capabilities.vm_platform import VMPlatform
    from agentworks.resources.graph import Readiness


@pytest.fixture
def make_config(tmp_path: Path):
    key = tmp_path / "id_ed25519"
    key.write_text("private")
    (tmp_path / "id_ed25519.pub").write_text("public")

    def _make(extra: str = "", *, resources: str | None = None):
        path = tmp_path / "config.toml"
        path.write_text(f'[operator]\nssh_public_key = "{key}.pub"\nssh_private_key = "{key}"\n' + extra)
        if resources is not None:
            rdir = tmp_path / "resources"
            rdir.mkdir(exist_ok=True)
            (rdir / "sites.yaml").write_text(resources)
        return load_config(path, warn_issues=False, warn_deprecations=False)

    return _make


def _placement_mode(config: object) -> str | None:
    """The placement tag off an UNVALIDATED lima config, mirroring what
    the real ``LimaPlatform.not_ready`` reads.

    A third statement of that read, and it needs no ``_readiness`` guard
    of its own: every test built on it asserts that ``lima-local`` is
    NOT-ready, and the only way this stub can deliver that is by matching
    the tag in the bundled ``vm-sites.yaml``, which the validate pass
    already holds to the model. A tag renamed underneath this leaves the
    stub answering ready and those assertions fire."""
    from collections.abc import Mapping

    if not isinstance(config, Mapping):
        return None
    placement = config.get("placement")
    return placement.get("mode") if isinstance(placement, Mapping) else None


def _site_doc(name: str, platform: str, **config: str) -> str:
    """One vm-site manifest in the canonical tagged shape."""
    keys = "".join(f"    {key}: {value}\n" for key, value in config.items())
    return (
        "apiVersion: agentworks/v1\n"
        "kind: vm-site\n"
        "metadata:\n"
        f"  name: {name}\n"
        "spec:\n"
        "  platform:\n"
        f"    name: {platform}\n" + keys
    )


_GPU_BOX = _site_doc("gpu-box", "lima", placement="{ mode: ssh, host: me@box }")


def _support(
    monkeypatch: pytest.MonkeyPatch,
    *,
    wsl2: str | None,
    lima_local: str | None,
) -> None:
    """Pin the two host-dependent checks to explicit outcomes.

    ``wsl2`` pins the platform-level host-support gate; ``lima_local``
    pins the config-dependent requirement for LOCAL lima sites only (ssh
    sites stay ready, mirroring the real check, which keys on the
    placement TAG rather than on a host being present).
    """
    from agentworks.resources.graph import Readiness

    monkeypatch.setattr(WSL2Platform, "unsupported_reason", classmethod(lambda cls: wsl2))
    monkeypatch.setattr(WSL2Platform, "not_ready", classmethod(lambda cls, config: Readiness.ready()))
    monkeypatch.setattr(
        LimaPlatform,
        "not_ready",
        classmethod(
            lambda cls, config: (
                Readiness.ready()
                if (_placement_mode(config) != "local" or lima_local is None)
                else Readiness.blocked(lima_local)
            )
        ),
    )


def test_every_site_registers_regardless_of_host(make_config, monkeypatch: pytest.MonkeyPatch) -> None:
    """The worst host (no Windows, no limactl) still registers both
    bundled sites; under R13 every platform row publishes UNCONDITIONALLY
    (host support is readiness, not absence), so all of them are present even
    when host-unsupported."""
    _support(monkeypatch, wsl2="Windows only", lima_local="limactl not installed")
    registry = build_registry(make_config())
    sites = dict(registry.iter_kind_items("vm-site"))
    assert {"lima-local", "wsl2"} <= set(sites)
    platforms = {e.name for e in registry.iter_kind("vm-platform")}
    # R9.5: the host-unsupported wsl2 platform is now a PRESENT (not-ready)
    # row, where before publish_to skipped it.
    assert platforms == {"lima", "wsl2", "azure-vm", "proxmox", "aws-ec2", "gcp-gce"}


def test_not_ready_reasons_chain(make_config, monkeypatch: pytest.MonkeyPatch) -> None:
    """A site's reason names the failing link: the platform gate for
    wsl2, the instance requirement for lima-local. The wsl2 site propagates
    the platform's own readiness verdict verbatim (no re-wrap, R9.1)."""
    _support(monkeypatch, wsl2="Windows only", lima_local="limactl not installed")
    registry = build_registry(make_config())
    graph = registry.graph
    assert graph.readiness_of("vm-site", "lima-local").reason == "limactl not installed"
    assert graph.readiness_of("vm-site", "wsl2").reason == "platform 'wsl2' is unsupported here: Windows only"


def test_r9_5_host_unsupported_platform_is_present_and_not_ready_on_the_graph(
    make_config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R9.5 at the graph level: an installed-but-host-unsupported platform
    (wsl2 off Windows) is a PRESENT ``vm-platform`` node whose stored
    ``readiness_of`` is not-ready, and the bundled wsl2 site that references
    it is NOT-READY, not a hard error. (The RENDERED not-ready row in
    ``resource list`` / doctor is asserted in later phases.)"""
    _support(monkeypatch, wsl2="Windows only", lima_local=None)
    registry = build_registry(make_config())  # no raise: wsl2 site is not-ready, not absent

    # The platform node is present and not-ready (readiness-vocabulary reason).
    assert ("vm-platform", "wsl2") in {(k, n) for k in registry.iter_kinds() for n, _ in registry.iter_kind_items(k)}
    platform_verdict = registry.graph.readiness_of("vm-platform", "wsl2")
    assert platform_verdict.reason == "platform 'wsl2' is unsupported here: Windows only"

    # The bundled wsl2 SITE folds to not-ready, propagating the platform verdict.
    site_verdict = registry.graph.readiness_of("vm-site", "wsl2")
    assert site_verdict.reason == "platform 'wsl2' is unsupported here: Windows only"


def test_r9_5_host_unsupported_platform_renders_not_ready_row(make_config, monkeypatch: pytest.MonkeyPatch) -> None:
    """R9.5 at the RENDERED level: the present, host-unsupported wsl2
    ``vm-platform`` row marks not-ready in ``resource list``, projected
    through the unified ``not_ready_reason_for`` reading ``graph.readiness_of``
    (the Phase-3 ``_VMPlatformKind`` shim is retired). Readiness-vocabulary
    reason (R9.1)."""
    from agentworks.resources.inspect import list_resources

    _support(monkeypatch, wsl2="Windows only", lima_local=None)
    registry = build_registry(make_config())
    rows = {r.name: r for r in list_resources(registry, kinds=("vm-platform",)).rows}
    assert rows["wsl2"].not_ready_reason == "platform 'wsl2' is unsupported here: Windows only"
    assert rows["lima"].not_ready_reason is None


def test_supported_host_has_everything_enabled(make_config, monkeypatch: pytest.MonkeyPatch) -> None:
    _support(monkeypatch, wsl2=None, lima_local=None)
    registry = build_registry(make_config())
    for name, _decl in registry.iter_kind_items("vm-site"):
        assert registry.graph.readiness_of("vm-site", name).reason is None
    platforms = {e.name for e in registry.iter_kind("vm-platform")}
    assert platforms == {"lima", "wsl2", "azure-vm", "proxmox", "aws-ec2", "gcp-gce"}


def test_remote_lima_site_enabled_without_local_limactl(make_config, monkeypatch: pytest.MonkeyPatch) -> None:
    """The load-bearing split: an ssh-placed Lima site runs limactl on
    the placement host over SSH, so only the LOCAL site disables."""
    _support(monkeypatch, wsl2="Windows only", lima_local="limactl not installed")
    registry = build_registry(make_config(resources=_GPU_BOX))
    graph = registry.graph
    assert graph.readiness_of("vm-site", "gpu-box").reason is None
    assert graph.readiness_of("vm-site", "lima-local").reason is not None


def test_declared_site_on_unsupported_platform_registers_not_ready(
    make_config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A declared site whose platform is host-unsupported no longer breaks
    every command (a resources dir shared across hosts degrades
    gracefully on the wrong host): it registers, not-ready with the
    platform's reason, and only USING it errors."""
    _support(monkeypatch, wsl2="Windows only", lima_local=None)
    config = make_config(
        resources=_site_doc("my-wsl", "wsl2"),
    )
    registry = build_registry(config)
    reason = registry.graph.readiness_of("vm-site", "my-wsl").reason
    assert reason == "platform 'wsl2' is unsupported here: Windows only"
    with pytest.raises(StateError, match="not ready on this host") as exc:
        resolve_site("my-wsl", registry)
    assert "Windows only" in str(exc.value)


def test_unknown_platform_site_is_a_hard_error(make_config, monkeypatch: pytest.MonkeyPatch) -> None:
    """R9.2: a site declared against a platform this build doesn't ship
    (a typo, or, later, an uninstalled plugin) is now a HARD finalize
    error, not a silent self-disable. With the vm-site edge-suppression
    removed, the site emits its platform edge unconditionally; the absent
    ``vm-platform`` row is the error miss policy's unknown-reference (the
    same loud failure every other kind's typo gets)."""
    _support(monkeypatch, wsl2=None, lima_local=None)
    config = make_config(
        resources=_site_doc("orbital", "skynet"),
    )
    with pytest.raises(ConfigError, match="unknown vm-platform 'skynet'"):
        build_registry(config)


def test_bundled_site_names_are_reserved_on_every_host(make_config, monkeypatch: pytest.MonkeyPatch) -> None:
    """Bundled rows publish everywhere, so the registry's reserved
    override fires even on a host where the bundled site is disabled:
    an operator cannot squat lima-local on a limactl-less box."""
    _support(monkeypatch, wsl2="Windows only", lima_local="limactl not installed")
    config = make_config(
        resources=_site_doc("lima-local", "lima"),
    )
    with pytest.raises(ConfigError, match="reserved"):
        build_registry(config)


def test_defaults_site_naming_a_disabled_site_is_valid_config(make_config, monkeypatch: pytest.MonkeyPatch) -> None:
    """The site exists, so defaults.site resolves; this host merely
    can't use it yet. Using it errors at resolve time and doctor warns
    on the reference; build_registry must NOT fail every command."""
    _support(monkeypatch, wsl2="Windows only", lima_local="limactl not installed")
    config = make_config('[defaults]\nsite = "lima-local"\n')
    registry = build_registry(config)  # no raise
    with pytest.raises(StateError, match="limactl not installed"):
        resolve_site("lima-local", registry)


def test_select_site_infers_over_enabled_sites_only(make_config, monkeypatch: pytest.MonkeyPatch) -> None:
    """Disabled sites are not a choice, but their existence never
    breaks inference: the single enabled site wins."""
    _support(monkeypatch, wsl2="Windows only", lima_local="limactl not installed")
    registry = build_registry(make_config(resources=_GPU_BOX))
    assert select_site(None, None, registry) == "gpu-box"


def test_select_site_errors_with_reasons_when_none_ready(make_config, monkeypatch: pytest.MonkeyPatch) -> None:
    _support(monkeypatch, wsl2="Windows only", lima_local="limactl not installed")
    registry = build_registry(make_config())
    with pytest.raises(ValidationError, match="no vm-sites are ready") as exc:
        select_site(None, None, registry)
    assert "limactl not installed" in str(exc.value)
    assert "Windows only" in str(exc.value)


def test_resource_layer_surfaces_not_ready_state(make_config, monkeypatch: pytest.MonkeyPatch) -> None:
    """`agw resource list` carries readiness facts for present resources."""
    from agentworks.resources.inspect import list_resources

    _support(monkeypatch, wsl2="Windows only", lima_local="limactl not installed")
    registry = build_registry(make_config())
    listing = list_resources(registry, kinds=("vm-site",))
    rows = {r.name: r for r in listing.rows}
    assert rows["lima-local"].not_ready_reason == "limactl not installed"
    assert rows["wsl2"].not_ready_reason == "platform 'wsl2' is unsupported here: Windows only"
    # Kinds without a readiness concept stay None (the no-op default).
    assert registry.graph.readiness_of("vm-template", "default").reason is None


def test_doctor_lists_platforms_and_not_ready_sites(make_config, monkeypatch: pytest.MonkeyPatch) -> None:
    """Platform rows carry only the platform-level state, read off the graph;
    per-site availability (lima-local without limactl) reports in the sites
    group where the site lives. DISABLED plugin platforms (``azure-vm`` and
    ``proxmox`` with their plugins not enabled) are skipped: the System plugins
    roster is the enablement authority, so this section never renders them as a
    misleading ``[ok]``."""
    from agentworks import doctor

    _support(monkeypatch, wsl2="Windows only", lima_local="limactl not installed")
    registry = build_registry(make_config())

    group = doctor._check_vm_platforms(registry)
    by_name = {c.name: c for c in group.checks}
    assert by_name["wsl2"].status is doctor.Status.INFO
    assert "not ready" in (by_name["wsl2"].message or "")
    assert "Windows only" in (by_name["wsl2"].message or "")
    lima_row = by_name["lima"]
    assert lima_row.status is doctor.Status.OK
    assert lima_row.message is None  # the bundled-site note moved to VM sites
    # azure-vm and proxmox are plugin platforms; with no plugins enabled they
    # are disabled and must NOT appear here (they list in the System plugins
    # roster as disabled instead).
    assert "azure-vm" not in by_name
    assert "proxmox" not in by_name


def test_doctor_shows_enabled_plugin_platform_with_real_readiness(make_config, monkeypatch: pytest.MonkeyPatch) -> None:
    """The other half of the disabled-hides rule: an ENABLED plugin platform
    (``proxmox`` opted in) DOES appear in the VM platforms section, carrying its
    real stored readiness rather than being hidden."""
    from agentworks import doctor

    _support(monkeypatch, wsl2="Windows only", lima_local="limactl not installed")
    registry = build_registry(make_config('[plugins]\nsystem = ["proxmox"]\n'))

    group = doctor._check_vm_platforms(registry)
    by_name = {c.name: c for c in group.checks}
    # Enabled: present, with its real readiness (proxmox is a remote-API platform
    # with no host requirement, so it is ready here).
    assert "proxmox" in by_name
    assert by_name["proxmox"].status is doctor.Status.OK
    # azure-vm stays disabled (its plugin is not enabled), so it stays hidden.
    assert "azure-vm" not in by_name


# -- The real methods (both branches, deterministically) ----------------------

#: Owner frame for the validation in :func:`_readiness`. These blobs back no
#: real site, so this only shapes the message when one is refused.
_PROBE_OWNER = RefOwner(kind="vm-site", name="readiness-probe")


def _readiness(platform: type[VMPlatform], config: dict[str, object]) -> Readiness:
    """``platform.not_ready(config)``, over a blob the platform's own
    declared model accepts.

    The comparator for D8 (see ``agentworks/schema/README.md``).
    ``not_ready`` reads raw config BY HAND, deliberately, so the readiness
    fold stays total over a malformed ``platform_config``; the cost is that
    nothing inside it can notice a tag the model no longer spells.
    Validating here is what ties the two. Rename the literal in
    ``LimaLocalPlacement.mode`` and this refuses the stale spelling, where
    an unvalidated blob would go on agreeing with an equally stale read
    while both disagreed with the model.

    ``not_ready`` still gets the RAW mapping and the validated instance is
    discarded, so what is under test stays the non-constructing read the
    fold calls, called the way the fold calls it. The tag comes from
    ``platform.name``, so this reads identically for every platform.

    Deliberately NOT for malformed blobs: an absent or unreadable
    ``placement`` is a case ``not_ready`` exists to answer and the model
    refuses it by design, so those call sites pass the blob raw and say so.
    """
    validate_capability_config(kind="vm-platform", config={"name": platform.name, **config}, owner=_PROBE_OWNER)
    return platform.not_ready(config)


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


def test_wsl2_not_ready_additionally_needs_wsl_exe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The config-dependent requirement on a supported host: wsl.exe is
    an optional Windows feature. ``not_ready`` is a NON-constructing
    classmethod over the site's config (no instance built).

    Through :func:`_readiness`, so the empty blob this reads nothing out
    of is still one the wsl2 model accepts: taking no configuration at all
    is the assumption its ``not_ready`` is built on."""
    monkeypatch.setattr("shutil.which", lambda name: None)
    verdict = _readiness(WSL2Platform, {})
    assert not verdict.is_ready
    assert verdict.reason is not None
    assert "wsl.exe" in verdict.reason

    monkeypatch.setattr("shutil.which", lambda name: "/x/wsl")
    assert _readiness(WSL2Platform, {}).is_ready


def test_lima_not_ready_is_local_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """lima the platform is supported everywhere; the limactl
    requirement binds to LOCAL sites only (ssh-placed sites run limactl
    on the placement host over SSH). ``not_ready`` reads the config
    directly, non-constructing.

    Keyed on the TAG saying local, never on a guess: a WRITTEN placement
    that does not say local is not treated as local, so a shape error is
    reported against ``placement`` by the validate pass rather than
    surfacing here as a missing ``limactl`` the operator does not need.
    An ABSENT placement resolves to the field's declared local default,
    the same answer validation gives it, so the limactl verdict applies
    to it exactly as to a written ``mode: local``.

    The well-formed blobs go through :func:`_readiness`, which is what
    stops the tags spelled here from drifting away from the model
    alongside the read they are pinning. The malformed one cannot, and
    the comment on it says why."""
    assert LimaPlatform.unsupported_reason() is None

    monkeypatch.setattr("shutil.which", lambda name: None)
    local = _readiness(LimaPlatform, {"placement": {"mode": "local"}})
    assert not local.is_ready
    assert local.reason is not None
    assert "limactl" in local.reason
    assert _readiness(LimaPlatform, {"placement": {"mode": "ssh", "host": "me@box"}}).is_ready
    # Absent: the declared default is local, so the verdict matches the
    # written local's rather than inventing a different site.
    absent = _readiness(LimaPlatform, {})
    assert not absent.is_ready
    assert absent.reason == local.reason
    # Unreadable: NOT local, so no limactl verdict is invented. Raw
    # rather than through _readiness, deliberately: the model refuses
    # this, and answering it anyway is the totality that ``not_ready``
    # exists for.
    assert LimaPlatform.not_ready({"placement": "junk"}).is_ready

    monkeypatch.setattr("shutil.which", lambda name: "/x/limactl")
    assert _readiness(LimaPlatform, {"placement": {"mode": "local"}}).is_ready
    assert _readiness(LimaPlatform, {}).is_ready
