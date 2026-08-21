"""Provider-aware, value-free secret description behavior."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from agentworks.bootstrap import build_registry
from agentworks.capabilities.secret_backend import OperatorImpact, TtyInteractionAccess
from agentworks.config import load_config
from agentworks.secrets.inspect import describe_secret, secret_description_data
from agentworks.secrets.preview import PreviewStatus
from tests.conftest import ManifestDoc, write_manifests


def _configured_secret(tmp_path: Path) -> tuple[object, object]:
    public_key = tmp_path / "id.pub"
    private_key = tmp_path / "id"
    public_key.write_text("ssh-ed25519 X")
    private_key.write_text("private")
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        dedent(
            f"""\
            [operator]
            ssh_public_key = "{public_key}"
            ssh_private_key = "{private_key}"

            [secret_config]
            sources = ["env-var", "prompt"]
            """
        )
    )
    write_manifests(
        tmp_path,
        ManifestDoc(
            "secret",
            "api-key",
            description="API key",
            spec={"backend_mappings": {"env-var": "TEST_API_KEY"}},
        ),
    )
    config = load_config(config_path, warn_issues=False)
    return config, build_registry(config)


def test_default_impact_goes_as_far_as_possible_without_prompting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, registry = _configured_secret(tmp_path)
    monkeypatch.delenv("TEST_API_KEY", raising=False)
    monkeypatch.setattr(
        "agentworks.output.prompt_secret",
        lambda *args, **kwargs: pytest.fail("zero-impact describe prompted"),
    )
    description = describe_secret(
        config,  # type: ignore[arg-type]
        registry,  # type: ignore[arg-type]
        "api-key",
        impact=OperatorImpact.NONE,
        tty_access=TtyInteractionAccess.AVAILABLE,
    )
    assert description.preview.status is PreviewStatus.INDETERMINATE
    assert [attempt.source for attempt in description.preview.attempts] == ["env-var", "prompt"]


def test_allow_impact_gets_definitive_preview_without_returning_the_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, registry = _configured_secret(tmp_path)
    monkeypatch.delenv("TEST_API_KEY", raising=False)
    monkeypatch.setattr("agentworks.output.prompt_secret", lambda *args, **kwargs: "sentinel-value")
    description = describe_secret(
        config,  # type: ignore[arg-type]
        registry,  # type: ignore[arg-type]
        "api-key",
        impact=OperatorImpact.ALLOW,
        tty_access=TtyInteractionAccess.AVAILABLE,
    )
    assert description.preview.status is PreviewStatus.AVAILABLE
    assert "sentinel-value" not in repr(description)
    assert "sentinel-value" not in repr(secret_description_data(description))


def test_json_v1_static_fields_remain_and_provider_preview_is_nested(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, registry = _configured_secret(tmp_path)
    monkeypatch.setenv("TEST_API_KEY", "discarded")
    data = secret_description_data(
        describe_secret(
            config,  # type: ignore[arg-type]
            registry,  # type: ignore[arg-type]
            "api-key",
            impact=OperatorImpact.NONE,
            tty_access=TtyInteractionAccess.DISABLED,
        )
    )
    secret = data["secret"]
    assert isinstance(secret, dict)
    source_mappings = secret["source_mappings"]
    assert isinstance(source_mappings, list)
    first_mapping = source_mappings[0]
    assert isinstance(first_mapping, dict)
    assert first_mapping["would_attempt"] is True
    resolution = secret["resolution"]
    assert isinstance(resolution, dict)
    assert set(("category", "source", "identifier", "skipped_not_ready", "preview")) <= set(resolution)
    preview = resolution["preview"]
    assert isinstance(preview, dict)
    assert preview["status"] == "available"
