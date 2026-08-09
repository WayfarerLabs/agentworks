"""Secret-backend classes are implementation capabilities selected by sources."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent
from typing import Any

import pytest

from agentworks.bootstrap import build_registry
from agentworks.capabilities.config import capability_mapping_references
from agentworks.capabilities.secret_backend import SECRET_BACKEND_REGISTRY
from agentworks.config import load_config
from agentworks.errors import ConfigError
from agentworks.schema import RefOwner
from agentworks.secrets.resolve import active_sources


def _config(tmp_path: Path, body: str = "") -> Any:
    pub = tmp_path / "k.pub"
    priv = tmp_path / "k"
    pub.write_text("ssh-ed25519 AAAA test")
    priv.write_text("key")
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        dedent(
            f"""\
            [operator]
            ssh_public_key = "{pub.as_posix()}"
            ssh_private_key = "{priv.as_posix()}"
            """
        )
        + dedent(body)
    )
    return load_config(cfg, warn_issues=False)


@pytest.mark.parametrize(
    ("backend", "mapping"),
    [
        pytest.param("env-var", "NPM_TOKEN", id="env-var"),
        pytest.param("onepassword", "op://Work/npm/token", id="onepassword"),
    ],
)
def test_shipped_mapping_implies_no_agentworks_resource(backend: str, mapping: object) -> None:
    assert (
        capability_mapping_references(
            kind="secret-backend",
            name=backend,
            mapping=mapping,
            owner=RefOwner(kind="secret", name="npm-token"),
        )
        == ()
    )


def test_one_descriptor_row_per_backend_class_and_builtin_sources(tmp_path: Path) -> None:
    registry = build_registry(_config(tmp_path))
    assert sorted(row.name for row in registry.iter_kind("secret-backend")) == sorted(SECRET_BACKEND_REGISTRY)
    assert [row.name for row in registry.iter_kind("secret-source")] == ["env-var", "prompt"]


def test_active_chain_selects_source_rows_not_backend_rows(tmp_path: Path) -> None:
    config = _config(tmp_path, '[secret_config]\nbackends = ["env-var", "prompt"]\n')
    sources = active_sources(config, build_registry(config))
    assert [source.name for source in sources] == ["env-var", "prompt"]
    assert [source.backend_class.name for source in sources] == ["env-var", "prompt"]


def test_direct_backend_name_in_chain_gets_source_rewrite(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        '[plugins]\nsystem = ["onepassword"]\n[secret_config]\nbackends = ["onepassword"]\n',
    )
    with pytest.raises(ConfigError) as caught:
        build_registry(config)
    assert "references unknown secret-source 'onepassword'" in str(caught.value)
    assert "kind: secret-source" in (caught.value.hint or "")


def test_would_attempt_is_pure_class_policy() -> None:
    from agentworks.capabilities.secret_backend.env_var import EnvVarBackend
    from agentworks.capabilities.secret_backend.prompt import PromptBackend
    from agentworks.plugins.onepassword.backend import OnePasswordBackend

    assert EnvVarBackend.would_attempt("s1", mapping_present=False) is True
    assert PromptBackend.would_attempt("s1", mapping_present=False) is True
    assert OnePasswordBackend.would_attempt("s1", mapping_present=False) is False
    assert OnePasswordBackend.would_attempt("s1", mapping_present=True) is True
