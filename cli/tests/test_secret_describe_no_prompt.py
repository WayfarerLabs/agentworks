"""Tests that ``agw secret describe`` never prompts and never resolves
a secret value (per FRD R10: "Describe does not prompt and does not
resolve secret values; it reports state").
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from agentworks.bootstrap import build_registry
from agentworks.config import load_config
from agentworks.secrets.inspect import describe_secret


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


def test_describe_secret_never_resolves_through_interactive_backends(
    tmp_path: Path,
    ssh_keys: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Monkeypatch the prompt provider's ``batch_get`` to fail loudly if
    anything calls it during describe. The resolution PREVIEW may probe
    non-interactive backends (env-var); an interactive backend must be
    reported on ``would_attempt`` alone -- probing it would BE the
    prompt.
    """
    cfg = _write_cfg(
        tmp_path,
        """\
        [secrets.api-key]
        description = "API key"

        [secret_config]
        backends = ["env-var", "prompt"]
        """,
        ssh_keys,
    )
    config = load_config(cfg, warn_issues=False)

    def _fail_batch_get(*args: object, **kwargs: object) -> None:
        raise AssertionError(
            "describe_secret invoked the prompt provider; interactive "
            "backends must be previewed via would_attempt alone (FRD R10)"
        )

    from agentworks.secrets import SECRET_PROVIDER_REGISTRY

    registry = build_registry(config)
    monkeypatch.setattr(
        SECRET_PROVIDER_REGISTRY["prompt"], "batch_get", _fail_batch_get
    )

    # Should complete without invoking the prompt provider.
    describe_secret(config, registry, "api-key")


def test_describe_secret_does_not_run_the_resolve_loop(
    tmp_path: Path,
    ssh_keys: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``resolve_secrets`` is the command resolution path. Describe must
    never route through it (its per-backend probe calls the door's
    ``resolve`` directly, one non-interactive backend at a time).
    """
    cfg = _write_cfg(
        tmp_path,
        """\
        [secrets.api-key]
        description = "API key"

        [secret_config]
        backends = ["env-var", "prompt"]
        """,
        ssh_keys,
    )
    config = load_config(cfg, warn_issues=False)

    def _fail_resolve_secrets(*args: object, **kwargs: object) -> None:
        raise AssertionError(
            "describe_secret ran the resolve loop; inspection must ask "
            "the backends directly"
        )

    registry = build_registry(config)
    monkeypatch.setattr(
        "agentworks.secrets.resolve.resolve_secrets", _fail_resolve_secrets
    )

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
    cfg = _write_cfg(
        tmp_path,
        """\
        [secrets.api-key]
        description = "API key"

        [secret_config]
        backends = ["env-var", "prompt"]
        """,
        ssh_keys,
    )
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
