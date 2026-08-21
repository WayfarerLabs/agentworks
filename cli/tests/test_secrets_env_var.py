"""Environment-variable source behavior through the typed core."""

from __future__ import annotations

import pytest

from agentworks.capabilities.secret_backend import OperatorImpact, ResolutionIntent, TtyInteractionAccess
from agentworks.capabilities.secret_backend.env_var import EnvVarBackend, EnvVarSourceConfig
from agentworks.resources.graph import Readiness
from agentworks.schema import CapabilityBlock
from agentworks.secrets import SecretDecl, SecretSourceDecl
from agentworks.secrets.outcomes import ResolutionMissing, ResolutionStatus
from agentworks.secrets.preview import PreviewStatus, preview_batch
from agentworks.secrets.resolve import ActiveSource, resolve_batch


def _source() -> ActiveSource:
    return ActiveSource(
        source=SecretSourceDecl(name="env-var", backend=CapabilityBlock.of("env-var")),
        backend_class=EnvVarBackend,
        config=EnvVarSourceConfig(name="env-var"),
        readiness=Readiness.ready(),
    )


def _resolve(decl: SecretDecl) -> tuple[dict[str, str], str | None]:
    batch = resolve_batch(
        [decl],
        [_source()],
        tty_access=TtyInteractionAccess.DISABLED,
        interaction_broker=None,
    )
    return batch.complete_or_raise(), batch.outcomes[0].identifier


def test_factory_and_context_entry_do_not_read_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "agentworks.capabilities.secret_backend.env_var._read",
        lambda request: pytest.fail("environment read started"),
    )
    context = EnvVarBackend.create_client(
        source_name="env-var",
        config=EnvVarSourceConfig(name="env-var"),
        intent=ResolutionIntent(),
        tty_access=TtyInteractionAccess.UNAVAILABLE,
        interaction_broker=None,
        remaining_time=lambda: None,
    )
    client = context.__enter__()
    context.__exit__(None, None, None)
    assert client is not None


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("line-one\nline-two\n", id="lf-with-terminal-newline"),
        pytest.param("line-one\r\nline-two\r\n", id="crlf-with-terminal-newline"),
        pytest.param("  internal value  ", id="surrounding-whitespace"),
    ],
)
def test_resolution_preserves_exact_value(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("AW_SECRET_GITHUB_TOKEN", value)
    values, identifier = _resolve(SecretDecl(name="github-token", description="GitHub PAT"))
    assert values == {"github-token": value}
    assert identifier == "AW_SECRET_GITHUB_TOKEN"


def test_override_uses_alternate_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "from-existing-env")
    values, identifier = _resolve(
        SecretDecl(
            name="github-token",
            description="GitHub PAT",
            backend_mappings={"env-var": "GITHUB_TOKEN"},
        )
    )
    assert values == {"github-token": "from-existing-env"}
    assert identifier == "GITHUB_TOKEN"


def test_unset_env_is_ordinary_missing_and_preview_is_value_free(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AW_SECRET_MISSING", raising=False)
    decl = SecretDecl(name="missing", description="missing")
    batch = resolve_batch(
        [decl],
        [_source()],
        tty_access=TtyInteractionAccess.UNAVAILABLE,
        interaction_broker=None,
    )
    assert batch.outcomes[0].status is ResolutionStatus.MISSING
    assert isinstance(batch.outcomes[0].result, ResolutionMissing)
    preview = preview_batch(
        [decl],
        [_source()],
        impact=OperatorImpact.NONE,
        tty_access=TtyInteractionAccess.UNAVAILABLE,
        interaction_broker=None,
    )["missing"]
    assert preview.status is PreviewStatus.MISSING


def test_false_mapping_has_no_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AW_SECRET_FORCED", "value")
    decl = SecretDecl(name="forced", description="forced", backend_mappings={"env-var": False})
    preview = preview_batch(
        [decl],
        [_source()],
        impact=OperatorImpact.NONE,
        tty_access=TtyInteractionAccess.AVAILABLE,
        interaction_broker=None,
    )["forced"]
    assert preview.status is PreviewStatus.BLOCKED
    assert preview.reason == "no-candidate"
