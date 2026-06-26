"""Tests for Phase 1c's vm-create Tailscale-resolution path.

The framework eager-resolves the Tailscale secret BEFORE any
state-mutating provisioning starts; the install runner receives the
value as a keyword argument; no ``env=`` injection.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from agentworks.config import load_config
from agentworks.vms.manager import _collect_secrets


@pytest.fixture()
def ssh_keys(tmp_path: Path) -> tuple[Path, Path]:
    pub = tmp_path / "id.pub"
    priv = tmp_path / "id"
    pub.write_text("ssh-ed25519 X")
    priv.write_text("-----BEGIN-----")
    return pub, priv


def _write_cfg(tmp_path: Path, body: str, ssh_keys: tuple[Path, Path]) -> Path:
    pub, priv = ssh_keys
    p = tmp_path / "c.toml"
    p.write_text(
        dedent(
            f"""\
            [operator]
            ssh_public_key = "{pub}"
            ssh_private_key = "{priv}"

            """
        )
        + dedent(body)
    )
    return p


def test_collect_secrets_resolves_tailscale_from_env_var(
    tmp_path: Path,
    ssh_keys: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The framework's env-var backend picks up ``AW_SECRET_TAILSCALE_AUTH_KEY``
    (default mapping for the ``tailscale-auth-key`` secret). The legacy
    ``AW_TAILSCALE_AUTH_KEY`` name is gone -- the framework's default
    backend convention is ``AW_SECRET_<UPPER_NAME>``.
    """
    cfg = _write_cfg(
        tmp_path,
        """\
        [secret_config]
        backends = ["env-var"]
        """,
        ssh_keys,
    )
    config = load_config(cfg, warn_issues=False)

    # The framework's env-var convention: AW_SECRET_<NAME>, dashes -> _.
    monkeypatch.setenv("AW_SECRET_TAILSCALE_AUTH_KEY", "tskey-from-env")

    # Build a resolved template instance directly (no DB scaffolding).
    from agentworks.vms.templates import resolve_template

    vm_tmpl = resolve_template(config, "default")
    ts_auth_key, git_tokens = _collect_secrets(config, {}, "test-vm", vm_tmpl)
    assert ts_auth_key == "tskey-from-env"
    assert git_tokens == {}


def test_collect_secrets_uses_custom_tailscale_secret_name(
    tmp_path: Path,
    ssh_keys: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A VMTemplate that overrides ``tailscale_auth_key = "custom-ts"``
    resolves the ``custom-ts`` secret name, not the default
    ``tailscale-auth-key``.
    """
    cfg = _write_cfg(
        tmp_path,
        """\
        [vm_templates.azure-prod]
        tailscale_auth_key = "custom-ts"

        [secret_config]
        backends = ["env-var"]
        """,
        ssh_keys,
    )
    config = load_config(cfg, warn_issues=False)

    monkeypatch.setenv("AW_SECRET_CUSTOM_TS", "tskey-custom")

    from agentworks.vms.templates import resolve_template

    vm_tmpl = resolve_template(config, "azure-prod")
    ts_auth_key, _ = _collect_secrets(config, {}, "test-vm", vm_tmpl)
    assert ts_auth_key == "tskey-custom"


def test_collect_secrets_signature_is_keyword_safe(
    tmp_path: Path,
    ssh_keys: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The framework-routed Tailscale path returns a non-None auth key
    when an env-var backend provides one. This is the precondition for
    the install runner's ``auth_key: str`` kwarg (no None fallback).
    """
    cfg = _write_cfg(
        tmp_path,
        """\
        [secret_config]
        backends = ["env-var"]
        """,
        ssh_keys,
    )
    config = load_config(cfg, warn_issues=False)
    monkeypatch.setenv("AW_SECRET_TAILSCALE_AUTH_KEY", "non-empty")

    from agentworks.vms.templates import resolve_template

    vm_tmpl = resolve_template(config, "default")
    ts_auth_key, _ = _collect_secrets(config, {}, "test-vm", vm_tmpl)
    assert ts_auth_key is not None
    assert isinstance(ts_auth_key, str)


def test_join_tailscale_signature_requires_auth_key_kwarg() -> None:
    """``_join_tailscale`` must require ``auth_key`` as a keyword arg --
    the SDD's hermetic-provisioning contract is broken if callers can
    pass None or omit it.
    """
    import inspect

    from agentworks.vms.initializer import _join_tailscale

    sig = inspect.signature(_join_tailscale)
    assert "auth_key" in sig.parameters
    param = sig.parameters["auth_key"]
    # Keyword-only (after the `*`).
    assert param.kind == inspect.Parameter.KEYWORD_ONLY
    # No default; required.
    assert param.default is inspect.Parameter.empty
