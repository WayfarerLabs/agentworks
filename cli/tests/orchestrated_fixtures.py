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

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

    from agentworks.config import Config

PROXMOX_SECTION = """
[proxmox]
api_url = "https://pve:8006"
node = "pve1"
token_id = "agw@pam!agw"
template_vmid = 9000
"""


def write_operator_config(tmp_path: Path, body: str = "") -> Config:
    """Write an operator config (with a throwaway SSH keypair) plus
    ``body``, and load it: the shared bottom half of every orchestrated
    suite's ``make_config``."""
    from agentworks.config import load_config

    key = tmp_path / "id_ed25519"
    key.write_text("private")
    (tmp_path / "id_ed25519.pub").write_text("public")
    path = tmp_path / "config.toml"
    path.write_text(f'[operator]\nssh_public_key = "{key}.pub"\nssh_private_key = "{key}"\n' + body)
    return load_config(path, warn_issues=False, warn_deprecations=False)


@pytest.fixture
def make_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    """The dominant ``make_config`` shape: the proxmox token in the
    env, the proxmox section baked in, extra sections appended per
    test."""
    monkeypatch.setenv("AW_SECRET_PROXMOX_TOKEN", "pve-token")

    def _make(extra: str = ""):  # noqa: ANN202
        return write_operator_config(tmp_path, PROXMOX_SECTION + extra)

    return _make


@pytest.fixture
def resolve_counter(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Record every backend-loop pass (the prompt-session oracle)."""
    from agentworks.secrets import resolve as secrets_resolve

    calls: list[list[str]] = []
    real = secrets_resolve.resolve_secrets

    def _counting(secrets: list[object], *args: object, **kwargs: object) -> dict[str, str]:
        calls.append([getattr(s, "name", str(s)) for s in secrets])
        return real(secrets, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(secrets_resolve, "resolve_secrets", _counting)
    return calls
