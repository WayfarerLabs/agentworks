"""Environment-variable source behavior through the typed core."""

from __future__ import annotations

import pytest

from agentworks.capabilities.secret_backend.env_var import EnvVarBackend, EnvVarSourceConfig
from agentworks.resources.graph import Readiness
from agentworks.schema import CapabilityBlock
from agentworks.secrets import SecretDecl, SecretSourceDecl
from agentworks.secrets.outcomes import ResolutionCategory, ResolutionOutcome
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.secrets.resolve import (
    ActiveSource,
    CompletionPolicy,
    ResolutionPolicy,
    resolve_batch,
)


def _source() -> ActiveSource:
    return ActiveSource(
        source=SecretSourceDecl(name="env-var", backend=CapabilityBlock.of("env-var")),
        backend_class=EnvVarBackend,
        config=EnvVarSourceConfig(name="env-var"),
        readiness=Readiness.ready(),
    )


def _resolve(decl: SecretDecl) -> tuple[dict[str, str], ResolutionOutcome]:
    batch = resolve_batch(
        [decl],
        [_source()],
        policy=ResolutionPolicy(
            interaction=TtyInteractionPolicy.REFUSE,
            completion=CompletionPolicy.COMPLETE,
        ),
        interaction_broker=None,
    )
    return batch.complete_or_raise(), batch.outcomes[0]


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("line-one\nline-two\n", id="lf-with-terminal-newline"),
        pytest.param("line-one\r\nline-two\r\n", id="crlf-with-terminal-newline"),
    ],
)
def test_default_convention_preserves_exact_multiline_value(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv("AW_SECRET_GITHUB_TOKEN", value)
    values, outcome = _resolve(SecretDecl(name="github-token", description="GitHub PAT"))
    assert values == {"github-token": value}
    assert outcome.category is ResolutionCategory.RESOLVED
    assert outcome.identifier == "AW_SECRET_GITHUB_TOKEN"


def test_override_uses_alternate_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "from-existing-env")
    values, outcome = _resolve(
        SecretDecl(
            name="github-token",
            description="GitHub PAT",
            backend_mappings={"env-var": "GITHUB_TOKEN"},
        )
    )
    assert values == {"github-token": "from-existing-env"}
    assert outcome.identifier == "GITHUB_TOKEN"


def test_opt_out_is_per_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AW_SECRET_FORCED", "value")
    decl = SecretDecl(name="forced", description="forced", backend_mappings={"env-var": False})
    source = _source()
    assert source.would_attempt(decl) is False
    batch = resolve_batch(
        [decl],
        [source],
        policy=ResolutionPolicy(TtyInteractionPolicy.REFUSE, CompletionPolicy.COMPLETE),
        interaction_broker=None,
    )
    assert batch.outcomes[0].category is not ResolutionCategory.RESOLVED


def test_unset_env_is_soft_miss(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AW_SECRET_MISSING", raising=False)
    batch = resolve_batch(
        [SecretDecl(name="missing", description="missing")],
        [_source()],
        policy=ResolutionPolicy(TtyInteractionPolicy.REFUSE, CompletionPolicy.COMPLETE),
        interaction_broker=None,
    )
    assert batch.outcomes[0].detail.value == "soft-miss"


def test_internal_whitespace_is_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AW_SECRET_TOKEN", "  internal value  ")
    values, _outcome = _resolve(SecretDecl(name="token", description="token"))
    assert values == {"token": "  internal value  "}
