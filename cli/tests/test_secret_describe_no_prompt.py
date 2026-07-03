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
from agentworks.secrets import resolver_for
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


def test_describe_secret_does_not_invoke_resolve_all(
    tmp_path: Path,
    ssh_keys: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Monkeypatch ``resolve_all`` on the resolver to fail loudly if
    anything calls it during describe. The describe path must report
    state via ``would_attempt`` / ``describe_lookup`` only, never
    actually resolve.
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

    def _fail_resolve_all(*args: object, **kwargs: object) -> None:
        raise AssertionError(
            "describe_secret called resolve_all; the function must "
            "report state without resolving values per FRD R10"
        )

    registry = build_registry(config)
    monkeypatch.setattr(
        resolver_for(registry), "resolve_all", _fail_resolve_all
    )

    # Should complete without invoking resolve_all.
    describe_secret(registry, config, "api-key")


def test_describe_secret_does_not_invoke_render(
    tmp_path: Path,
    ssh_keys: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``render`` is the env-block resolution path. Describe must not
    consume the env table.
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

    def _fail_render(*args: object, **kwargs: object) -> None:
        raise AssertionError(
            "describe_secret called render; the function must not "
            "resolve env-table values"
        )

    registry = build_registry(config)
    monkeypatch.setattr(resolver_for(registry), "render", _fail_render)

    describe_secret(registry, config, "api-key")


def test_describe_secret_does_not_invoke_prompt_backend(
    tmp_path: Path,
    ssh_keys: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The prompt backend's ``get`` / ``batch_get`` methods are the
    interactive surfaces; ``describe`` must call neither.
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

    # Find the prompt source in the active chain.
    registry = build_registry(config)
    prompt_source = None
    for source in resolver_for(registry).sources:
        if source.kind == "prompt":
            prompt_source = source
            break
    assert prompt_source is not None

    def _fail(*args: object, **kwargs: object) -> None:
        raise AssertionError(
            "describe_secret invoked the prompt backend; the function "
            "must report state via would_attempt / describe_lookup only"
        )

    monkeypatch.setattr(prompt_source, "get", _fail)
    monkeypatch.setattr(prompt_source, "batch_get", _fail)

    registry = build_registry(config)
    describe_secret(registry, config, "api-key")
