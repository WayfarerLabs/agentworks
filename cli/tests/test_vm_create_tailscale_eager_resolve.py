"""Tests for the vm-create Tailscale-resolution path.

The Tailscale secret resolves through the operation's resolver at the
preflight boundary, BEFORE any state-mutating provisioning starts (the
vm-template node declares it, the walk union registers it, and the
node's preflight predicts it centrally); the install runner receives
the value as a keyword argument; no ``env=`` injection.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from agentworks.config import load_config


def _resolve_tailscale_key(config, registry, vm_tmpl) -> str:  # type: ignore[no-untyped-def]
    """The create-path shape: the vm-template node declares the key
    (its ``secret_refs``, which the walk union registers), its
    preflight predicts it centrally, the boundary resolve runs, ops
    read from the cache."""
    from agentworks.capabilities.base import RunContext
    from agentworks.secrets.resolver import Resolver
    from agentworks.vms.nodes import vm_template_node

    resolver = Resolver(config, registry)
    node = vm_template_node(vm_tmpl, registry)
    for name in node.secret_refs():
        resolver.register_name(name)
    node.preflight(RunContext(config=config))
    resolver.resolve()
    return resolver.get(vm_tmpl.tailscale_auth_key)


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


def test_boundary_resolves_tailscale_from_env_var(
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
    from agentworks.bootstrap import build_registry
    from agentworks.vms.templates import resolve_template

    registry = build_registry(config)
    vm_tmpl = resolve_template(registry, "default")
    assert _resolve_tailscale_key(config, registry, vm_tmpl) == "tskey-from-env"


def test_boundary_uses_custom_tailscale_secret_name(
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

    from agentworks.bootstrap import build_registry
    from agentworks.vms.templates import resolve_template

    registry = build_registry(config)
    vm_tmpl = resolve_template(registry, "azure-prod")
    assert _resolve_tailscale_key(config, registry, vm_tmpl) == "tskey-custom"


def test_template_preflight_fails_on_unresolvable_key(
    tmp_path: Path,
    ssh_keys: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With only the env-var backend active and the variable unset, the
    vm-template's preflight fails the prediction BEFORE any resolve pass
    (and therefore before any prompt or mutation).
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
    monkeypatch.delenv("AW_SECRET_TAILSCALE_AUTH_KEY", raising=False)

    from agentworks.bootstrap import build_registry
    from agentworks.capabilities.base import RunContext
    from agentworks.errors import ConfigError
    from agentworks.vms.nodes import vm_template_node
    from agentworks.vms.templates import resolve_template

    registry = build_registry(config)
    vm_tmpl = resolve_template(registry, "default")
    with pytest.raises(ConfigError, match="not resolvable"):
        vm_template_node(vm_tmpl, registry).preflight(RunContext(config=config))


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


# -- --ignore-env (rekey) masking ------------------------------------------


def test_mask_env_var_backend_pops_framework_default_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``--ignore-env`` masker pops the framework's
    ``AW_SECRET_<UPPER_NAME>`` convention for the resolved secret name,
    so the env-var backend silently skips and the next backend runs.
    """
    import os

    from agentworks.secrets.base import SecretDecl
    from agentworks.vms.manager import _mask_env_var_backend_for

    monkeypatch.setenv("AW_SECRET_TAILSCALE_AUTH_KEY", "stale-value")
    decl = SecretDecl(name="tailscale-auth-key", description="")
    with _mask_env_var_backend_for(decl, masked=True):
        assert "AW_SECRET_TAILSCALE_AUTH_KEY" not in os.environ
    # Restored on exit.
    assert os.environ["AW_SECRET_TAILSCALE_AUTH_KEY"] == "stale-value"


def test_mask_env_var_backend_pops_operator_typed_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the operator sets ``backend_mappings.env-var = "CUSTOM_VAR"``
    in ``[secrets.<name>]``, the masker pops THAT name (in addition to
    the framework's default convention).
    """
    import os

    from agentworks.secrets.base import SecretDecl
    from agentworks.vms.manager import _mask_env_var_backend_for

    monkeypatch.setenv("AW_SECRET_TAILSCALE_AUTH_KEY", "default-stale")
    monkeypatch.setenv("CUSTOM_TS_VAR", "operator-stale")
    decl = SecretDecl(
        name="tailscale-auth-key",
        description="",
        backend_mappings={"env-var": "CUSTOM_TS_VAR"},
    )
    with _mask_env_var_backend_for(decl, masked=True):
        assert "AW_SECRET_TAILSCALE_AUTH_KEY" not in os.environ
        assert "CUSTOM_TS_VAR" not in os.environ
    # Restored on exit.
    assert os.environ["AW_SECRET_TAILSCALE_AUTH_KEY"] == "default-stale"
    assert os.environ["CUSTOM_TS_VAR"] == "operator-stale"


def test_mask_env_var_backend_no_op_when_unmasked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``masked=False`` (the default ``vm rekey`` shape) is a pass-through
    -- the env vars stay set throughout the block.
    """
    import os

    from agentworks.secrets.base import SecretDecl
    from agentworks.vms.manager import _mask_env_var_backend_for

    monkeypatch.setenv("AW_SECRET_TAILSCALE_AUTH_KEY", "present")
    decl = SecretDecl(name="tailscale-auth-key", description="")
    with _mask_env_var_backend_for(decl, masked=False):
        assert os.environ["AW_SECRET_TAILSCALE_AUTH_KEY"] == "present"
    assert os.environ["AW_SECRET_TAILSCALE_AUTH_KEY"] == "present"


def test_mask_env_var_backend_no_error_when_var_unset() -> None:
    """When neither the framework name nor the operator override is set
    in the environment, masking is a no-op rather than a KeyError.
    """
    import os

    from agentworks.secrets.base import SecretDecl
    from agentworks.vms.manager import _mask_env_var_backend_for

    # Make sure no relevant var is set; monkeypatch isn't needed since we
    # don't introduce one. (Best-effort hygiene if the test runner inherits
    # a polluted env.)
    os.environ.pop("AW_SECRET_TAILSCALE_AUTH_KEY", None)
    decl = SecretDecl(name="tailscale-auth-key", description="")
    with _mask_env_var_backend_for(decl, masked=True):
        pass  # no exception is the assertion


def test_mask_env_var_backend_restores_on_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raise inside the block (e.g., operator hits Ctrl-C during a
    prompt) must still restore the masked env vars.
    """
    import os

    from agentworks.secrets.base import SecretDecl
    from agentworks.vms.manager import _mask_env_var_backend_for

    monkeypatch.setenv("AW_SECRET_TAILSCALE_AUTH_KEY", "before-raise")
    decl = SecretDecl(name="tailscale-auth-key", description="")
    with pytest.raises(KeyboardInterrupt), _mask_env_var_backend_for(decl, masked=True):
        assert "AW_SECRET_TAILSCALE_AUTH_KEY" not in os.environ
        raise KeyboardInterrupt
    assert os.environ["AW_SECRET_TAILSCALE_AUTH_KEY"] == "before-raise"
