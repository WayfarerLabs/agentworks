"""Shared fixtures for the orchestrated-command proof suites.

The migrated commands' tests all drive the real config, registry,
resolver, and backend loop (env-var backend) with the platform's
backend ops as the fakes, so they share three pieces: the standard
proxmox site section, the operator-config builder, and the
backend-loop recorder (the prompt-session oracle). Registered as a
pytest plugin from ``tests/conftest.py``, so the fixtures are
available everywhere without imports; suites with extra needs (more
env vars, extra baked-in sections, un-stubbing an autouse fixture)
keep a local ``make_config`` built on :func:`write_operator_config`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from agentworks.config import Config
    from tests.conftest import ManifestDoc

# The orchestrated suites use proxmox as their platform fixture: it is the
# only VM platform that carries a config secret (``proxmox-token``), so it is
# the natural stand-in for exercising the secret-resolution boundary (the
# ``secret_union`` / backend-loop assertions these suites are built around).
# Since Phase 10 (R11) proxmox ships in the opt-in ``proxmox`` system plugin,
# so the shared config opts in exactly as a real proxmox operator would; the
# dependency on the plugin is explicit in the fixture, never silent. (Tests of
# the disabled-by-default behavior live in ``tests/plugins/test_proxmox.py``.)
PLUGINS_ENABLED = """
[plugins]
system = ["proxmox"]
"""


def proxmox_site() -> ManifestDoc:
    """The proxmox ``vm-site`` as a resources/ manifest: the declarative
    replacement for the retired legacy ``[proxmox]`` TOML section (now an
    ordinary unexpected top-level key under ADR 0022).

    A function (not a module constant) so each caller gets a fresh spec
    dict, since ``ManifestDoc`` is shared and its ``spec`` is mutable.
    """
    from tests.conftest import ManifestDoc

    return ManifestDoc(
        kind="vm-site",
        name="proxmox",
        spec={
            "platform": {
                "name": "proxmox",
                "api_url": "https://pve:8006",
                "node": "pve1",
                "token_id": "agw@pam!agw",
                "template_vmid": 9000,
            }
        },
    )


def write_operator_config(
    tmp_path: Path,
    body: str = "",
    *,
    manifests: Sequence[ManifestDoc | str] = (),
) -> Config:
    """Write an operator config (with a throwaway SSH keypair) plus
    ``body``, optionally seed ``resources/*.yaml`` manifests beside it, and
    load it: the shared bottom half of every orchestrated suite's
    ``make_config``."""
    from agentworks.config import load_config
    from tests.conftest import write_manifests
    from tests.ssh_fixtures import write_test_ssh_keypair

    key = tmp_path / "id_ed25519"
    write_test_ssh_keypair(key)
    path = tmp_path / "config.toml"
    path.write_text(f'[operator]\nssh_public_key = "{key}.pub"\nssh_private_key = "{key}"\n' + body)
    if manifests:
        write_manifests(tmp_path, *manifests)
    return load_config(path, warn_issues=False, warn_deprecations=False)


@pytest.fixture
def make_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    """The dominant ``make_config`` shape: the proxmox token in the env,
    the proxmox plugin enabled (a settings section), and the proxmox site
    declared as a manifest, with extra TOML settings appended per test.

    ``manifests`` seeds additional ``resources/*.yaml`` declarations beside
    the proxmox site (templates, git credentials, secrets), the declarative
    replacement for the resource TOML that used to ride in ``extra``."""
    monkeypatch.setenv("AW_SECRET_PROXMOX_TOKEN", "pve-token")

    def _make(extra: str = "", *, manifests: Sequence[ManifestDoc | str] = ()):  # noqa: ANN202
        return write_operator_config(tmp_path, PLUGINS_ENABLED + extra, manifests=[proxmox_site(), *manifests])

    return _make


@pytest.fixture
def resolve_counter(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Record every backend-loop pass (the prompt-session oracle)."""
    from agentworks.secrets import resolve as secrets_resolve

    calls: list[list[str]] = []
    from agentworks.secrets.resolve import ResolutionBatch

    real = secrets_resolve.resolve_batch

    def _counting(secrets: list[object], *args: Any, **kwargs: Any) -> ResolutionBatch:
        calls.append([getattr(s, "name", str(s)) for s in secrets])
        return real(secrets, *args, **kwargs)

    monkeypatch.setattr(secrets_resolve, "resolve_batch", _counting)
    return calls
