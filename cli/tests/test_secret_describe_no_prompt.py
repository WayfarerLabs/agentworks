"""Tests that ``agw secret describe`` never prompts and never resolves
a secret value (per FRD R10: "Describe does not prompt and does not
resolve secret values; it reports state").
"""

from __future__ import annotations

from textwrap import dedent
from typing import TYPE_CHECKING

import pytest

from agentworks.bootstrap import build_registry
from agentworks.config import load_config
from agentworks.secrets.inspect import describe_secret
from tests.conftest import ManifestDoc, write_manifests

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture()
def ssh_keys(tmp_path: Path) -> tuple[Path, Path]:
    pub = tmp_path / "id.pub"
    priv = tmp_path / "id"
    pub.write_text("ssh-ed25519 X")
    priv.write_text("-----BEGIN-----")
    return pub, priv


def _write_api_key_cfg(tmp_path: Path, ssh_keys: tuple[Path, Path]) -> Path:
    """Write a config with one operator secret (``api-key``) declared as a
    YAML manifest and the env-var/prompt source chain."""
    pub, priv = ssh_keys
    p = tmp_path / "c.toml"
    p.write_text(
        dedent(
            f"""\
            [operator]
            ssh_public_key = "{pub}"
            ssh_private_key = "{priv}"

            [secret_config]
            sources = ["env-var", "prompt"]
            """
        )
    )
    write_manifests(tmp_path, ManifestDoc("secret", "api-key", description="API key"))
    return p


def test_describe_secret_never_opens_interactive_sources(
    tmp_path: Path,
    ssh_keys: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pure preview uses metadata only and never creates a prompt client."""
    cfg = _write_api_key_cfg(tmp_path, ssh_keys)
    config = load_config(cfg, warn_issues=False)

    def _fail_batch_get(*args: object, **kwargs: object) -> None:
        raise AssertionError(
            "describe_secret invoked the prompt provider; interactive "
            "sources must be previewed via metadata alone (FRD R10)"
        )

    from agentworks.capabilities.secret_backend import SECRET_BACKEND_REGISTRY

    registry = build_registry(config)
    monkeypatch.setattr(SECRET_BACKEND_REGISTRY["prompt"], "create_client", _fail_batch_get)

    # Should complete without invoking the prompt source's client factory.
    describe_secret(config, registry, "api-key")


def test_describe_secret_does_not_run_the_resolve_loop(
    tmp_path: Path,
    ssh_keys: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``resolve_batch`` is the command resolution path. Describe must
    never route through it; pure preview reads source projection and
    readiness metadata without probing any current value.
    """
    cfg = _write_api_key_cfg(tmp_path, ssh_keys)
    config = load_config(cfg, warn_issues=False)

    def _fail_resolve_batch(*args: object, **kwargs: object) -> None:
        raise AssertionError("describe_secret ran the resolve loop; inspection must remain non-probing")

    registry = build_registry(config)
    monkeypatch.setattr("agentworks.secrets.resolve.resolve_batch", _fail_resolve_batch)

    describe_secret(config, registry, "api-key")


def test_describe_secret_does_not_invoke_prompt_backend(
    tmp_path: Path,
    ssh_keys: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``output.prompt_secret`` is the actual operator-interaction
    surface; ``describe`` must never reach it (belt to the provider
    batch_get guard's suspenders).
    """
    cfg = _write_api_key_cfg(tmp_path, ssh_keys)
    config = load_config(cfg, warn_issues=False)

    def _fail(*args: object, **kwargs: object) -> None:
        raise AssertionError(
            "describe_secret prompted the operator; the function must "
            "report state via would_attempt / describe_lookup only"
        )

    from agentworks import output

    monkeypatch.setattr(output, "prompt_secret", _fail)
    monkeypatch.setattr(output, "is_interactive", lambda: True)

    registry = build_registry(config)
    describe_secret(config, registry, "api-key")
