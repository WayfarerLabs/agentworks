"""Site resolution: the only constructor of bound platform instances, plus
the stranded-site ConfigError.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agentworks.capabilities.vm_platform.lima import LimaPlatform
from agentworks.errors import ConfigError, StateError
from agentworks.manifests import builtin as builtin_manifests
from agentworks.plugins.proxmox.platform import ProxmoxPlatform
from agentworks.resources import Origin, Registry
from agentworks.schema import CapabilityBlock
from agentworks.vms.sites import (
    VMSiteDecl,
    resolve_site,
    site_manifest_hint,
)


@pytest.fixture(autouse=True)
def _enabled_everywhere(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dispatch tests exercise resolution shape, not this host's OS and
    tooling: pin every platform supported and every site enabled.
    Tests OF the disabled model re-patch the individual methods."""
    from tests.conftest import stub_platform_support

    stub_platform_support(monkeypatch)


def _registry(*sites: VMSiteDecl) -> Registry:
    from tests.conftest import publish_all_platforms

    registry = Registry.empty()
    builtin_manifests.publish_to(registry)
    # Publish all four capability rows regardless of the test host.
    publish_all_platforms(registry)
    for site in sites:
        registry.add("vm-site", site.name, site, Origin.built_in(source="test"))
    registry.finalize()
    return registry


def test_resolve_site_binds_the_platform_config() -> None:
    registry = _registry(
        VMSiteDecl(
            name="gpu-box",
            platform=CapabilityBlock.of("lima", **{"vm_host": "me@box"}),
        )
    )
    platform = resolve_site("gpu-box", registry)
    assert isinstance(platform, LimaPlatform)
    assert platform.site_name == "gpu-box"
    assert platform.is_remote


def test_resolve_site_bundled_lima_is_local() -> None:
    registry = _registry()
    platform = resolve_site("lima-local", registry)
    assert isinstance(platform, LimaPlatform)
    assert not platform.is_remote


def test_resolve_site_unknown_raises_the_stranded_error() -> None:
    registry = _registry()
    with pytest.raises(ConfigError, match="site 'gpu-box' is not declared") as exc:
        resolve_site("gpu-box", registry)
    assert "kind: vm-site" in (exc.value.hint or "")
    assert "name: gpu-box" in (exc.value.hint or "")


def test_vm_row_site_field_resolves_through_the_vm_site() -> None:
    """A VM row's site field resolves to its bound platform through
    ``resolve_site`` (the shape the node factories use; the old
    ``platform_for`` delegate retired with ``bind_platform``)."""
    registry = _registry()
    vm = SimpleNamespace(site="wsl2")
    platform = resolve_site(vm.site, registry)
    assert platform.name == "wsl2"


def _px_registry() -> Registry:
    return _registry(
        VMSiteDecl(
            name="px",
            platform=CapabilityBlock.model_validate(
                {
                    "name": "proxmox",
                    **{
                        "api_url": "https://pve:8006",
                        "node": "pve1",
                        "token_id": "t",
                        "template_vmid": 9000,
                    },
                }
            ),
        )
    )


def test_ops_read_the_token_through_the_context() -> None:
    """The instance-reads-context pin: the op client reads its token
    only via ``ctx.secret`` (scoped delivery). A context scoped to the
    site's declared names serves it; a context with NO resolved
    secrets fails with the accessor's typed ``ConfigError``; an
    UNDECLARED name is scoped delivery's typed refusal. The instance
    holds no resolver-shaped value source."""
    from agentworks.capabilities.base import RunContext
    from agentworks.orchestration.secrets import ScopedSecrets

    registry = _px_registry()
    platform = resolve_site("px", registry)
    assert isinstance(platform, ProxmoxPlatform)

    served = RunContext(secrets=ScopedSecrets({"proxmox-token": "s3cret"}, ("proxmox-token",)))
    assert platform._api(served) is not None

    bare = resolve_site("px", registry)
    assert isinstance(bare, ProxmoxPlatform)
    with pytest.raises(ConfigError, match=r"no\s+resolved secrets"):
        bare._api(RunContext())
    with pytest.raises(StateError, match="not declared"):
        bare._api(RunContext(secrets=ScopedSecrets({}, ("other-name",))))


# The two ``validate_sites`` cases that lived here moved to
# tests/test_config_setting_references.py when that check became the generic
# settings-reference pass. They are not lost: the new module parametrizes the
# same two assertions (a declared or absent name builds, an unknown one is a
# ConfigError) over every settings value that names a resource, and it drives
# them through a real load_config + build_registry rather than the
# SimpleNamespace stub config they used here.


def test_site_manifest_hint_carries_the_vm_host() -> None:
    hint = site_manifest_hint("gpu-box", vm_host="me@box")
    assert "name: gpu-box" in hint
    assert "vm_host: me@box" in hint


# -- select_site: the house selection model ---------------------------------


def test_select_site_flag_then_default_win() -> None:
    from agentworks.vms.sites import select_site

    registry = _registry()
    assert select_site("flagged", "defaulted", registry) == "flagged"
    assert select_site(None, "defaulted", registry) == "defaulted"


def test_select_site_infers_the_single_declared_site(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exactly one declared site: use it silently (the zero-config
    case: a default install has only its host's bundled site)."""
    from agentworks.resources import Registry
    from agentworks.vms.sites import select_site
    from tests.conftest import publish_all_platforms

    registry = Registry.empty()
    publish_all_platforms(registry)
    registry.add(
        "vm-site",
        "only-one",
        VMSiteDecl(name="only-one", platform=CapabilityBlock(name="lima")),
        Origin.built_in(source="test"),
    )
    registry.finalize()
    assert select_site(None, None, registry) == "only-one"


def test_select_site_prompts_between_several_when_interactive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentworks import output
    from agentworks.vms.sites import select_site

    registry = _registry()  # bundled lima-local + wsl2
    monkeypatch.setattr(output, "is_interactive", lambda: True)
    captured: dict[str, object] = {}

    def _choose(msg: str, options: list[str]) -> int:
        captured["options"] = options
        return 1

    monkeypatch.setattr(output, "choose", _choose)
    assert select_site(None, None, registry) == "wsl2"
    assert captured["options"] == ["lima-local", "wsl2"]


def test_select_site_errors_between_several_when_non_interactive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentworks import output
    from agentworks.errors import ValidationError
    from agentworks.vms.sites import select_site

    registry = _registry()
    monkeypatch.setattr(output, "is_interactive", lambda: False)
    with pytest.raises(ValidationError, match="multiple sites") as exc:
        select_site(None, None, registry)
    assert "--site" in (exc.value.hint or "")


def test_select_site_errors_when_none_declared() -> None:
    from agentworks.errors import ValidationError
    from agentworks.resources import Registry
    from agentworks.vms.sites import select_site
    from tests.conftest import publish_all_platforms

    registry = Registry.empty()
    publish_all_platforms(registry)
    registry.finalize()
    with pytest.raises(ValidationError, match="no vm-sites are ready"):
        select_site(None, None, registry)


# -- Not-ready sites at the resolve chokepoint -------------------------------


def test_resolving_a_not_ready_site_names_the_requirement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A VM on a bundled site whose requirement went away (limactl
    uninstalled after VMs existed) gets the site's not-ready reason at
    resolve time: the site still EXISTS (lookup succeeds; the
    stranded paste-a-manifest error is only for undeclared names)."""
    from agentworks.resources.graph import Readiness
    from agentworks.vms.sites import lookup_site

    monkeypatch.setattr(
        LimaPlatform, "not_ready", classmethod(lambda cls, config: Readiness.blocked("limactl not installed"))
    )
    registry = _registry()

    assert lookup_site("lima-local", registry).platform.name == "lima"
    with pytest.raises(StateError, match="not ready on this host") as exc:
        resolve_site("lima-local", registry)
    assert "limactl" in str(exc.value)
    assert "kind: vm-site" not in (exc.value.hint or "")
