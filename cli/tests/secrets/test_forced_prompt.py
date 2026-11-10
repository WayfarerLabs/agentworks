"""Operation-local prompt selection preserves ordinary source behavior."""

from __future__ import annotations

import pytest

from agentworks.capabilities.secret_backend import BlockReason, OperatorImpact, TtyInteractionAccess
from agentworks.capabilities.secret_backend.env_var import EnvVarBackend, EnvVarSourceConfig
from agentworks.errors import UserAbort
from agentworks.resources.graph import Readiness
from agentworks.schema import CapabilityBlock
from agentworks.secrets import SecretDecl, SecretSourceDecl
from agentworks.secrets.preview import PreviewStatus, preview_batch
from agentworks.secrets.resolve import ActiveSource, _operation_prompt_source, resolve_batch


class Broker:
    def __init__(self, *, abort: bool = False) -> None:
        self.names: list[str] = []
        self.abort = abort

    def request_secret(self, name: str, /) -> str:
        self.names.append(name)
        if self.abort:
            raise UserAbort("cancelled")
        return f"typed-{name}"


def env_source() -> ActiveSource:
    return ActiveSource(
        source=SecretSourceDecl(name="env-var", backend=CapabilityBlock(name="env-var")),
        backend_class=EnvVarBackend,
        config=EnvVarSourceConfig(name="env-var"),
        readiness=Readiness.ready(),
    )


@pytest.mark.parametrize("configured_prompt", [False, True])
def test_forced_prompt_bypasses_sources_and_opt_out_without_affecting_other_names(
    monkeypatch: pytest.MonkeyPatch, configured_prompt: bool
) -> None:
    monkeypatch.setenv("AW_SECRET_AUTH", "stale-auth")
    monkeypatch.setenv("AW_SECRET_SITE", "site-value")
    declarations = [
        SecretDecl(name="auth", description="auth", backend_mappings={"prompt": False}),
        SecretDecl(name="site", description="site"),
    ]
    sources = [env_source()]
    if configured_prompt:
        sources.append(_operation_prompt_source())
    broker = Broker()
    preview = preview_batch(
        declarations,
        sources,
        impact=OperatorImpact.NONE,
        tty_access=TtyInteractionAccess.AVAILABLE,
        interaction_broker=None,
        prompt_names=frozenset({"auth"}),
    )
    assert preview["auth"].status is PreviewStatus.INDETERMINATE
    assert [(attempt.source, type(attempt.result).__name__) for attempt in preview["auth"].attempts] == [
        ("prompt", "PreviewIndeterminate")
    ]
    assert preview["site"].status is PreviewStatus.AVAILABLE
    assert preview["site"].source == "env-var"
    assert broker.names == []

    batch = resolve_batch(
        declarations,
        sources,
        tty_access=TtyInteractionAccess.AVAILABLE,
        interaction_broker=broker,
        prompt_names=frozenset({"auth"}),
    )
    assert batch.complete_or_raise() == {"auth": "typed-auth", "site": "site-value"}
    assert broker.names == ["auth"]
    assert declarations[0].backend_mappings == {"prompt": False}


@pytest.mark.parametrize(
    ("access", "reason"),
    [
        (TtyInteractionAccess.DISABLED, BlockReason.TTY_INTERACTION_DISABLED),
        (TtyInteractionAccess.UNAVAILABLE, BlockReason.TTY_UNAVAILABLE),
    ],
)
def test_forced_prompt_has_the_same_tty_refusal_in_preview_and_resolution(
    monkeypatch: pytest.MonkeyPatch, access: TtyInteractionAccess, reason: BlockReason
) -> None:
    monkeypatch.setenv("AW_SECRET_AUTH", "must-not-be-used")
    declarations = [SecretDecl(name="auth", description="auth")]
    preview = preview_batch(
        declarations,
        [env_source()],
        impact=OperatorImpact.NONE,
        tty_access=access,
        interaction_broker=None,
        prompt_names=frozenset({"auth"}),
    )["auth"]
    outcome = resolve_batch(
        declarations,
        [env_source()],
        tty_access=access,
        interaction_broker=None,
        prompt_names=frozenset({"auth"}),
    ).outcomes[0]
    assert preview.status is PreviewStatus.BLOCKED
    assert preview.reason == outcome.reason == reason


def test_forced_turn_does_not_enable_prompt_for_other_names_in_an_empty_chain() -> None:
    declarations = [SecretDecl(name=name, description=name) for name in ("auth", "site")]
    broker = Broker()
    batch = resolve_batch(
        declarations,
        [],
        tty_access=TtyInteractionAccess.AVAILABLE,
        interaction_broker=broker,
        prompt_names=frozenset({"auth"}),
    )
    assert batch.outcomes[1].reason is BlockReason.NO_ACTIVE_SOURCE
    assert broker.names == []  # A doomed complete batch never starts a prompt.
    preview = preview_batch(
        declarations,
        [],
        impact=OperatorImpact.NONE,
        tty_access=TtyInteractionAccess.AVAILABLE,
        interaction_broker=None,
        prompt_names=frozenset({"auth"}),
    )
    baseline = preview_batch(
        declarations,
        [],
        impact=OperatorImpact.NONE,
        tty_access=TtyInteractionAccess.AVAILABLE,
        interaction_broker=None,
    )
    assert preview["site"] == baseline["site"]


def test_configured_prompt_still_accepts_ordinary_names() -> None:
    declarations = [SecretDecl(name=name, description=name) for name in ("auth", "site")]
    broker = Broker()
    batch = resolve_batch(
        declarations,
        [_operation_prompt_source()],
        tty_access=TtyInteractionAccess.AVAILABLE,
        interaction_broker=broker,
        prompt_names=frozenset({"auth"}),
    )
    assert batch.complete_or_raise() == {"auth": "typed-auth", "site": "typed-site"}
    assert broker.names == ["site", "auth"]


def test_prompt_abort_propagates_without_consuming_an_environment_value(monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    monkeypatch.setenv("AW_SECRET_AUTH", "still-present")
    with pytest.raises(UserAbort):
        resolve_batch(
            [SecretDecl(name="auth", description="auth")],
            [env_source()],
            tty_access=TtyInteractionAccess.AVAILABLE,
            interaction_broker=Broker(abort=True),
            prompt_names=frozenset({"auth"}),
        )
    assert os.environ["AW_SECRET_AUTH"] == "still-present"


def test_forced_prompt_bypasses_non_env_provider_and_closes_its_context(monkeypatch: pytest.MonkeyPatch) -> None:
    from contextlib import contextmanager

    from agentworks.capabilities.secret_backend import BackendResolved, PreviewAvailable
    from tests.secrets.test_batch_completion import _Client, _source

    class Provider(_Client):
        results = {"auth": BackendResolved("stale-provider-key"), "site": BackendResolved("provider-site")}
        calls = []

        def preview(self, requests):
            return {request.name: PreviewAvailable() for request in requests}

    source = _source("provider", Provider)
    events = []

    @contextmanager
    def create_client(**kwargs):
        events.append("enter")
        try:
            yield Provider(None)
        finally:
            events.append("exit")

    monkeypatch.setattr(source.backend_class, "create_client", create_client)
    declarations = [SecretDecl(name=name, description=name) for name in ("auth", "site")]
    preview = preview_batch(
        declarations,
        [source],
        impact=OperatorImpact.NONE,
        tty_access=TtyInteractionAccess.AVAILABLE,
        interaction_broker=None,
        prompt_names=frozenset({"auth"}),
    )
    assert preview["auth"].status is PreviewStatus.INDETERMINATE
    assert preview["site"].status is PreviewStatus.AVAILABLE
    broker = Broker()
    batch = resolve_batch(
        declarations,
        [source],
        tty_access=TtyInteractionAccess.AVAILABLE,
        interaction_broker=broker,
        prompt_names=frozenset({"auth"}),
    )
    assert batch.complete_or_raise() == {"auth": "typed-auth", "site": "provider-site"}
    assert Provider.calls == [("site",)]
    assert broker.names == ["auth"]
    assert events == ["enter", "exit", "enter", "exit"]
