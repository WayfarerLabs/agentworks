"""Final variadic CLI over the shared multi-name verification service."""

from __future__ import annotations

import re
import subprocess
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest
from typer.testing import CliRunner

from agentworks import output
from agentworks.bootstrap import build_registry
from agentworks.capabilities.secret_backend.client import (
    SecretClientFailure,
    SecretClientFailureKind,
    SecretClientRemediation,
    SecretClientTimeout,
)
from agentworks.capabilities.secret_backend.prompt import PromptBackend, PromptSourceConfig
from agentworks.cli import app
from agentworks.config import load_config
from agentworks.errors import NotFoundError, StateError, UserAbort, ValidationError
from agentworks.plugins import SYSTEM_PLUGINS
from agentworks.plugins.onepassword.backend import (
    OnePasswordBackend,
    OnePasswordSourceConfig,
)
from agentworks.resources.graph import Readiness
from agentworks.schema import CapabilityBlock
from agentworks.secrets import SecretDecl, SecretSourceDecl
from agentworks.secrets.outcomes import (
    ResolutionCategory,
    ResolutionDetail,
    ResolutionOutcome,
    ResolutionRemediation,
    complete_resolution_error,
    format_outcome,
)
from agentworks.secrets.policy import InteractionPolicy
from agentworks.secrets.resolve import (
    ActiveSource,
    CompletionPolicy,
    ResolutionPolicy,
    active_sources,
    resolve_batch,
)
from agentworks.secrets.verification import render_verification, verify_secrets
from tests.conftest import ManifestDoc, write_cfg
from tests.secrets.test_resolution_lifecycle import _Backend, _source

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _plain(text: str) -> str:
    """Strip Rich SGR styling before asserting option-token text."""
    return _ANSI_RE.sub("", text)


class _InteractiveBackend(_Backend):
    interactive: ClassVar[bool] = True


class _SeparatorBackend(_Backend):
    separator: ClassVar[str] = "\u2028"

    @classmethod
    def describe_lookup(cls, secret_name: str, mapping: object) -> str:
        del secret_name, mapping
        return f"safe{cls.separator}forged-row"


class _HostileName(str):
    def __repr__(self) -> str:
        return "sentinel-hostile-name-repr"


class _Registry:
    def __init__(self, declarations: dict[str, SecretDecl]) -> None:
        self.declarations = declarations
        self.lookups: list[str] = []

    def lookup(self, kind: str, name: str) -> SecretDecl:
        assert kind == "secret"
        self.lookups.append(name)
        try:
            return self.declarations[name]
        except KeyError:
            raise KeyError(name) from None


@pytest.fixture(autouse=True)
def _reset() -> object:
    _Backend.events = []
    _Backend.values = {}
    _Backend.failure = None
    output.set_non_interactive(False)
    yield
    output.set_non_interactive(False)


def _registry(*names: str) -> _Registry:
    return _Registry({name: SecretDecl(name=name, description=name) for name in names})


def _install_cli_registry(monkeypatch: pytest.MonkeyPatch, *names: str) -> _Registry:
    registry = _registry(*names)
    monkeypatch.setattr("agentworks.config.load_config", lambda: SimpleNamespace())
    monkeypatch.setattr("agentworks.bootstrap.load_request_registry", lambda config: registry)
    monkeypatch.setattr("agentworks.secrets.resolve.active_sources", lambda config, candidate: [_source()])
    return registry


def _prompt_source() -> ActiveSource:
    return ActiveSource(
        source=SecretSourceDecl(name="prompt", backend=CapabilityBlock.of("prompt")),
        backend_class=PromptBackend,
        config=PromptSourceConfig(name="prompt"),
        readiness=Readiness.ready(),
    )


def _onepassword_source() -> ActiveSource:
    return ActiveSource(
        source=SecretSourceDecl(name="work-op", backend=CapabilityBlock.of("onepassword")),
        backend_class=OnePasswordBackend,
        config=OnePasswordSourceConfig(name="onepassword"),
        readiness=Readiness.ready(),
    )


def _run_main(monkeypatch: pytest.MonkeyPatch, *argv: str) -> int:
    """Run through the real CLI entry point and return its process exit code."""
    from agentworks import cli as cli_module

    monkeypatch.setattr("sys.argv", ["agentworks", *argv])
    monkeypatch.setenv("AGW_DEBUG", "")
    with pytest.raises(SystemExit) as caught:
        cli_module.main()
    return 0 if caught.value.code is None else int(caught.value.code)


def _verify(monkeypatch: pytest.MonkeyPatch, *names: str) -> tuple[ResolutionOutcome, ...]:
    monkeypatch.setattr("agentworks.secrets.resolve.active_sources", lambda config, registry: [_source()])
    return verify_secrets(
        SimpleNamespace(),  # type: ignore[arg-type]
        _registry(*names),  # type: ignore[arg-type]
        names,
        interaction=InteractionPolicy.REFUSE,
    )


def _outcome(
    name: str,
    detail: ResolutionDetail,
    *,
    source: str | None = "fixture",
    identifier: str | None = "safe-id",
) -> ResolutionOutcome:
    rules = {
        ResolutionDetail.RESOLVED: (ResolutionCategory.RESOLVED, ResolutionRemediation.NONE),
        ResolutionDetail.SOFT_MISS: (
            ResolutionCategory.UNAVAILABLE,
            ResolutionRemediation.CONFIGURE_SOURCE,
        ),
        ResolutionDetail.INTERACTION_REFUSED: (
            ResolutionCategory.REFUSED_INTERACTION,
            ResolutionRemediation.ALLOW_INTERACTION,
        ),
        ResolutionDetail.DEADLINE_EXCEEDED: (
            ResolutionCategory.TIMEOUT,
            ResolutionRemediation.INCREASE_TIMEOUT,
        ),
        ResolutionDetail.AUTHENTICATION: (
            ResolutionCategory.RESOLUTION_FAILURE,
            ResolutionRemediation.SIGN_IN,
        ),
        ResolutionDetail.CONNECTIVITY: (
            ResolutionCategory.RESOLUTION_FAILURE,
            ResolutionRemediation.CHECK_CONNECTIVITY,
        ),
        ResolutionDetail.BACKEND_PROTOCOL: (
            ResolutionCategory.RESOLUTION_FAILURE,
            ResolutionRemediation.REPORT_BACKEND,
        ),
    }
    category, remediation = rules[detail]
    return ResolutionOutcome(
        name=name,
        category=category,
        detail=detail,
        remediation=remediation,
        source=source,
        identifier=None if detail is ResolutionDetail.BACKEND_PROTOCOL else identifier,
    )


def test_verify_returns_value_free_shared_outcomes(monkeypatch: pytest.MonkeyPatch) -> None:
    _Backend.values = {"token": "sentinel-secret-value"}
    outcomes = _verify(monkeypatch, "token")
    assert len(outcomes) == 1
    assert outcomes[0].category is ResolutionCategory.RESOLVED
    assert outcomes[0].detail is ResolutionDetail.RESOLVED
    assert "sentinel" not in repr(outcomes)


def test_verify_preserves_first_order_dedupe_in_one_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    _Backend.values = {"a": "one", "b": "two"}
    monkeypatch.setattr("agentworks.secrets.resolve.active_sources", lambda config, registry: [_source()])
    registry = _registry("a", "b")
    outcomes = verify_secrets(
        SimpleNamespace(),  # type: ignore[arg-type]
        registry,  # type: ignore[arg-type]
        ["b", "a", "b"],
        interaction=InteractionPolicy.REFUSE,
    )
    assert [outcome.name for outcome in outcomes] == ["b", "a"]
    assert registry.lookups == ["b", "a"]
    assert _Backend.events == ["factory", "enter", "prepare", "resolve", "exit"]


def test_verify_returns_soft_miss_instead_of_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    (outcome,) = _verify(monkeypatch, "token")
    assert outcome.category is ResolutionCategory.UNAVAILABLE
    assert outcome.detail is ResolutionDetail.SOFT_MISS


def test_verify_disabled_sources_collapse_to_no_active_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agentworks.secrets.resolve.active_sources", lambda config, registry: [])
    (outcome,) = verify_secrets(
        SimpleNamespace(),  # type: ignore[arg-type]
        _registry("token"),  # type: ignore[arg-type]
        ["token"],
        interaction=InteractionPolicy.REFUSE,
    )
    assert outcome.detail is ResolutionDetail.NO_ACTIVE_SOURCE
    assert _Backend.events == []


def test_verify_not_ready_source_never_constructs_client(monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel = "PRINTABLE-READINESS-SENTINEL"
    source = replace(_source(ready=False), readiness=Readiness.blocked(sentinel))
    monkeypatch.setattr(
        "agentworks.secrets.resolve.active_sources",
        lambda config, registry: [source],
    )
    (outcome,) = verify_secrets(
        SimpleNamespace(),  # type: ignore[arg-type]
        _registry("token"),  # type: ignore[arg-type]
        ["token"],
        interaction=InteractionPolicy.REFUSE,
    )
    assert outcome.detail is ResolutionDetail.SOURCE_NOT_READY
    assert outcome.remediation is ResolutionRemediation.ENABLE_SOURCE
    assert outcome.remediation_target is None
    rendered: list[str] = []
    monkeypatch.setattr(output, "info", rendered.append)
    render_verification((outcome,))
    assert sentinel not in repr(outcome)
    assert sentinel not in format_outcome(outcome)
    assert sentinel not in str(complete_resolution_error((outcome,)))
    assert sentinel not in "\n".join(rendered)
    assert _Backend.events == []


def test_verify_disabled_plugin_source_discards_printable_readiness_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = "PRINTABLE-PLUGIN-READINESS-SENTINEL"
    source = replace(
        _source(ready=False),
        readiness=Readiness.blocked(sentinel),
        disabled_backend_plugin="Vault.Plugin",
    )
    monkeypatch.setattr("agentworks.secrets.resolve.active_sources", lambda config, registry: [source])

    (outcome,) = verify_secrets(
        SimpleNamespace(),  # type: ignore[arg-type]
        _registry("token"),  # type: ignore[arg-type]
        ["token"],
        interaction=InteractionPolicy.REFUSE,
    )

    assert outcome.detail is ResolutionDetail.SOURCE_BACKEND_PLUGIN_DISABLED
    assert outcome.remediation is ResolutionRemediation.ENABLE_PLUGIN
    assert outcome.remediation_target == "Vault.Plugin"
    assert format_outcome(outcome).endswith("remediation=enable plugin `Vault.Plugin`")
    assert sentinel not in repr(outcome)
    assert sentinel not in str(complete_resolution_error((outcome,)))


def test_verify_declared_source_retains_disabled_backend_plugin_remediation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = write_cfg(
        tmp_path,
        ManifestDoc(
            "secret-source",
            "work-op",
            {"backend": {"name": "onepassword"}},
        ),
        ManifestDoc(
            "secret",
            "token",
            {"backend_mappings": {"work-op": "op://Work/item/password"}},
            description="token",
        ),
        settings='[secret_config]\nsources = ["work-op"]\n',
    )
    config = load_config(config_path, warn_issues=False)
    registry = build_registry(config)
    (outcome,) = verify_secrets(
        config,
        registry,
        ["token"],
        interaction=InteractionPolicy.REFUSE,
    )
    assert outcome.detail is ResolutionDetail.SOURCE_BACKEND_PLUGIN_DISABLED
    assert outcome.remediation is ResolutionRemediation.ENABLE_PLUGIN
    assert outcome.remediation_target == "onepassword"
    assert format_outcome(outcome).endswith("remediation=enable plugin `onepassword`")
    monkeypatch.setattr("agentworks.config.load_config", lambda: config)
    monkeypatch.setattr("agentworks.bootstrap.load_request_registry", lambda candidate: registry)

    result = CliRunner().invoke(app, ["secret", "verify", "token"])

    assert result.exit_code == 1
    assert "source-backend-plugin-disabled" in result.stdout
    assert _plain(result.stdout).count("enable plugin `onepassword`") == 1


def test_verify_refuses_interactive_source_without_construction(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "agentworks.secrets.resolve.active_sources",
        lambda config, registry: [_source(backend_class=_InteractiveBackend)],
    )
    (outcome,) = verify_secrets(
        SimpleNamespace(),  # type: ignore[arg-type]
        _registry("token"),  # type: ignore[arg-type]
        ["token"],
        interaction=InteractionPolicy.REFUSE,
    )
    assert outcome.detail is ResolutionDetail.INTERACTION_REFUSED
    assert _Backend.events == []


def test_verify_rejects_non_enum_policy_before_any_source_work(monkeypatch: pytest.MonkeyPatch) -> None:
    """A caller-supplied ``"refuse"`` is equal to the enum but not identical to it.

    Every consumer branches by identity, so without the boundary check this call would
    take the not-refuse path and attempt the interactive source it meant to refuse.
    """
    registry = _registry("token")
    monkeypatch.setattr(
        "agentworks.secrets.resolve.active_sources",
        lambda config, candidate: [_source(backend_class=_InteractiveBackend)],
    )
    with pytest.raises(StateError):
        verify_secrets(
            SimpleNamespace(),  # type: ignore[arg-type]
            registry,  # type: ignore[arg-type]
            ["token"],
            interaction="refuse",  # type: ignore[arg-type]
        )
    assert registry.lookups == []
    assert _Backend.events == []


@pytest.mark.parametrize(
    ("failure", "expected"),
    (
        (SecretClientTimeout(), ResolutionDetail.DEADLINE_EXCEEDED),
        (
            SecretClientFailure(
                kind=SecretClientFailureKind.HARD_MAPPING,
                remediation=SecretClientRemediation.CHECK_MAPPING,
            ),
            ResolutionDetail.HARD_MAPPING,
        ),
        (
            SecretClientFailure(
                kind=SecretClientFailureKind.AUTHENTICATION,
                remediation=SecretClientRemediation.SIGN_IN,
            ),
            ResolutionDetail.AUTHENTICATION,
        ),
        (
            SecretClientFailure(
                kind=SecretClientFailureKind.CONNECTIVITY,
                remediation=SecretClientRemediation.CHECK_CONNECTIVITY,
            ),
            ResolutionDetail.CONNECTIVITY,
        ),
        (
            SecretClientFailure(
                kind=SecretClientFailureKind.EXTERNAL,
                remediation=SecretClientRemediation.RETRY,
            ),
            ResolutionDetail.EXTERNAL,
        ),
        (RuntimeError("provider-native-sentinel"), ResolutionDetail.UNEXPECTED),
    ),
)
def test_verify_translates_hard_failures_without_provider_payload(
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
    expected: ResolutionDetail,
) -> None:
    _Backend.failure = failure
    (outcome,) = _verify(monkeypatch, "token")
    assert outcome.detail is expected
    assert "provider-native-sentinel" not in repr(outcome)


def test_verify_validates_every_name_before_lookup_or_source_work(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = _registry("valid")
    monkeypatch.setattr(
        "agentworks.secrets.resolve.active_sources",
        lambda config, candidate: pytest.fail("source work must not start"),
    )
    with pytest.raises(ValidationError) as caught:
        verify_secrets(
            SimpleNamespace(),  # type: ignore[arg-type]
            registry,  # type: ignore[arg-type]
            ["valid", "bad\nname"],
            interaction=InteractionPolicy.REFUSE,
        )
    assert registry.lookups == []
    assert "bad\nname" not in str(caught.value)


@pytest.mark.parametrize(
    ("rejected", "sentinel"),
    [
        pytest.param("sentinel-newline\nforged", "sentinel-newline", id="newline"),
        pytest.param("sentinel-control\x00forged", "sentinel-control", id="control"),
        pytest.param("sentinel-separator\u2028forged", "sentinel-separator", id="unicode-line-separator"),
        pytest.param(_HostileName("token"), "sentinel-hostile-name-repr", id="hostile-str-subclass"),
    ],
)
def test_invalid_name_error_is_safe_and_context_free(
    rejected: object,
    sentinel: str,
) -> None:
    with pytest.raises(ValidationError) as caught:
        verify_secrets(
            SimpleNamespace(),  # type: ignore[arg-type]
            _registry(),  # type: ignore[arg-type]
            [rejected],  # type: ignore[list-item]
            interaction=InteractionPolicy.REFUSE,
        )

    assert str(caught.value) == (
        "invalid secret name; expected 1-253 lowercase alphanumeric characters, "
        "hyphens or underscores, with an alphanumeric first and last character "
        "and no consecutive hyphens"
    )
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert sentinel not in repr(caught.value)
    assert sentinel not in repr(caught.value.args)


def test_unexpected_name_validator_failure_propagates_by_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    unexpected = RuntimeError("sentinel-validator-failure")
    registry = _registry("token")
    monkeypatch.setattr(
        "agentworks.secrets.verification.validate_name",
        lambda name, *, max_length: (_ for _ in ()).throw(unexpected),
    )
    monkeypatch.setattr(
        "agentworks.secrets.resolve.active_sources",
        lambda config, candidate: pytest.fail("source work must not start"),
    )

    with pytest.raises(RuntimeError) as caught:
        verify_secrets(
            SimpleNamespace(),  # type: ignore[arg-type]
            registry,  # type: ignore[arg-type]
            ["token"],
            interaction=InteractionPolicy.REFUSE,
        )

    assert caught.value is unexpected
    assert registry.lookups == []


def test_verify_looks_up_every_unique_name_before_source_work(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = _registry("present")
    monkeypatch.setattr(
        "agentworks.secrets.resolve.active_sources",
        lambda config, candidate: pytest.fail("source work must not start"),
    )
    with pytest.raises(NotFoundError, match="secret 'missing' not found"):
        verify_secrets(
            SimpleNamespace(),  # type: ignore[arg-type]
            registry,  # type: ignore[arg-type]
            ["present", "missing"],
            interaction=InteractionPolicy.REFUSE,
        )
    assert registry.lookups == ["present", "missing"]


@pytest.mark.parametrize("names", [[], [""], ["--bad"], ["bad\nname"], ["double--hyphen"]])
def test_verify_rejects_empty_or_unsafe_names_without_echo(names: list[str]) -> None:
    with pytest.raises(ValidationError) as caught:
        verify_secrets(
            SimpleNamespace(),  # type: ignore[arg-type]
            _registry(),  # type: ignore[arg-type]
            names,
            interaction=InteractionPolicy.REFUSE,
        )
    assert "bad\nname" not in str(caught.value)
    assert caught.value.__cause__ is None


@pytest.mark.parametrize(
    "separator",
    ["\n", "\r", "\r\n", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"],
)
def test_outcome_rejects_every_splitlines_separator(separator: str) -> None:
    forged = f"safe{separator}forged-row"
    assert len(forged.splitlines()) > 1
    with pytest.raises(ValueError, match="invalid resolution outcome identifier"):
        _outcome("token", ResolutionDetail.RESOLVED, identifier=forged)


@pytest.mark.parametrize("separator", ["\u2028", "\u2029"])
def test_service_and_renderer_reduce_unicode_separator_to_one_protocol_row(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    separator: str,
) -> None:
    _SeparatorBackend.separator = separator
    monkeypatch.setattr(
        "agentworks.secrets.resolve.active_sources",
        lambda config, registry: [_source(backend_class=_SeparatorBackend)],
    )

    outcomes = verify_secrets(
        SimpleNamespace(),  # type: ignore[arg-type]
        _registry("token"),  # type: ignore[arg-type]
        ["token"],
        interaction=InteractionPolicy.REFUSE,
    )

    assert len(outcomes) == 1
    assert outcomes[0].detail is ResolutionDetail.BACKEND_PROTOCOL
    assert outcomes[0].identifier is None
    render_verification(outcomes)
    rendered = capsys.readouterr().out
    assert len(rendered.splitlines()) == 3
    assert "forged-row" not in rendered


def test_an_auto_declared_name_cannot_split_one_outcome_into_two_rendered_rows(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """One outcome renders as one row whatever the operator named the secret.

    A secret name reaches no naming validator anywhere: the ``secret`` kind
    auto-declares any name a reference uses, with no name restriction
    (``secrets/kinds.py``). The name on a rendered diagnostic row is
    therefore operator-authored text arriving at a render boundary, and a
    line separator inside it splits one row into two, the second carrying
    the operator's own text where a resolution result belongs.

    Driven from a manifest rather than a hand-built row, because the premise
    the screen rests on is exactly that this name reaches the registry
    unvalidated. ``ResolutionOutcome`` screens it, so no row is built at all;
    what is asserted is the property that holds whichever way that goes,
    which is that the table's body carries one line per outcome.
    """
    forged = "token\nSecret: forged"
    config_path = write_cfg(
        tmp_path,
        ManifestDoc("admin-template", "default", {"env": {"API_KEY": {"secret": forged}}}),
        settings="[secret_config]\nsources = []\n",
    )
    config = load_config(config_path, warn_issues=False)
    registry = build_registry(config)
    decl = registry.lookup("secret", forged)
    assert decl.name == forged

    outcomes: tuple[ResolutionOutcome, ...] = ()
    with suppress(ValueError):
        outcomes = resolve_batch(
            [decl],
            active_sources(config, registry),
            policy=ResolutionPolicy(
                interaction=InteractionPolicy.REFUSE,
                completion=CompletionPolicy.COMPLETE,
            ),
            interaction_broker=None,
        ).outcomes

    render_verification(outcomes)
    body = capsys.readouterr().out.splitlines()[2:]
    assert len(body) == len(outcomes)


def test_render_verification_uses_only_shared_outcome_fields(capsys: pytest.CaptureFixture[str]) -> None:
    outcomes = (
        _outcome("ok", ResolutionDetail.RESOLVED),
        _outcome("missing", ResolutionDetail.SOFT_MISS),
        _outcome("prompted", ResolutionDetail.INTERACTION_REFUSED),
        _outcome("slow", ResolutionDetail.DEADLINE_EXCEEDED),
        _outcome("auth", ResolutionDetail.AUTHENTICATION),
        _outcome("network", ResolutionDetail.CONNECTIVITY),
        _outcome("broken", ResolutionDetail.BACKEND_PROTOCOL),
    )
    render_verification(outcomes)
    rendered = capsys.readouterr().out
    assert rendered.splitlines()[0].split() == [
        "NAME",
        "CATEGORY",
        "SOURCE",
        "IDENTIFIER",
        "DETAIL",
        "REMEDIATION",
    ]
    assert len(rendered.splitlines()) == len(outcomes) + 2
    for outcome in outcomes:
        assert outcome.name in rendered
        assert outcome.category.value in rendered
        assert outcome.detail.value in rendered
        assert outcome.remediation.value in rendered
    assert "broken" in rendered and "fixture" in rendered
    assert "safe-id" in rendered and "  -" in rendered


def test_service_allow_is_explicit_under_global_noninteractive_and_uses_onepassword_plugin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="sentinel-onepassword-value", stderr="")

    plugin_backends = SYSTEM_PLUGINS["onepassword"].capabilities["secret-backend"]
    assert OnePasswordBackend in plugin_backends
    output.set_non_interactive(True)
    monkeypatch.setattr(
        output,
        "non_interactive",
        lambda: pytest.fail("verification service must not read ambient interaction state"),
    )
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(
        "agentworks.secrets.resolve.active_sources",
        lambda config, registry: [_onepassword_source()],
    )
    registry = _Registry(
        {
            "token": SecretDecl(
                name="token",
                description="token",
                backend_mappings={"work-op": "op://Work/item/password"},
            )
        }
    )

    outcomes = verify_secrets(
        SimpleNamespace(),  # type: ignore[arg-type]
        registry,  # type: ignore[arg-type]
        ["token"],
        interaction=InteractionPolicy.ALLOW,
    )

    assert outcomes[0].category is ResolutionCategory.RESOLVED
    assert calls == [["op", "read", "--no-newline", "op://Work/item/password"]]
    assert "sentinel-onepassword-value" not in repr(outcomes)


def test_prompt_user_abort_propagates_by_identity_without_exposing_prior_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    abort = UserAbort("sentinel-user-abort")
    prompts: list[str] = []

    def prompt_secret(label: str, hint: str | None = None) -> str:
        del hint
        prompts.append(label)
        if len(prompts) == 2:
            raise abort
        return "sentinel-earlier-prompt-value"

    monkeypatch.setattr(output, "prompt_secret", prompt_secret)
    monkeypatch.setattr(
        "agentworks.secrets.resolve.active_sources",
        lambda config, registry: [_prompt_source()],
    )

    with pytest.raises(UserAbort) as caught:
        verify_secrets(
            SimpleNamespace(),  # type: ignore[arg-type]
            _registry("first", "second"),  # type: ignore[arg-type]
            ["first", "second"],
            interaction=InteractionPolicy.ALLOW,
        )

    assert caught.value is abort
    assert len(prompts) == 2
    assert "sentinel-earlier-prompt-value" not in str(caught.value)
    assert "sentinel-earlier-prompt-value" not in repr(caught.value)
    assert "sentinel-earlier-prompt-value" not in repr(caught.value.args)


def test_complete_batch_dooms_remaining_names_before_prompt_interaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _Backend.failure = SecretClientFailure(
        kind=SecretClientFailureKind.HARD_MAPPING,
        remediation=SecretClientRemediation.CHECK_MAPPING,
    )
    registry = _Registry(
        {
            "blocked": SecretDecl(name="blocked", description="blocked"),
            "would-prompt": SecretDecl(
                name="would-prompt",
                description="would prompt",
                backend_mappings={"primary": False},
            ),
        }
    )
    monkeypatch.setattr(
        "agentworks.secrets.resolve.active_sources",
        lambda config, candidate: [_source(), _prompt_source()],
    )
    monkeypatch.setattr(
        output,
        "prompt_secret",
        lambda *args, **kwargs: pytest.fail("prompt interaction must not start"),
    )

    outcomes = verify_secrets(
        SimpleNamespace(),  # type: ignore[arg-type]
        registry,  # type: ignore[arg-type]
        ["blocked", "would-prompt"],
        interaction=InteractionPolicy.ALLOW,
    )

    assert [outcome.detail for outcome in outcomes] == [
        ResolutionDetail.HARD_MAPPING,
        ResolutionDetail.BATCH_DOOMED,
    ]
    assert _Backend.events == ["factory", "enter", "prepare", "resolve", "exit"]


def test_secret_verify_cli_renders_variadic_success_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    _Backend.values = {"alpha": "sentinel-alpha", "beta": "sentinel-beta"}
    registry = _install_cli_registry(monkeypatch, "alpha", "beta")

    result = CliRunner().invoke(app, ["secret", "verify", "beta", "alpha", "beta"])

    assert result.exit_code == 0
    assert result.stderr == ""
    assert result.stdout.splitlines()[0].split() == [
        "NAME",
        "CATEGORY",
        "SOURCE",
        "IDENTIFIER",
        "DETAIL",
        "REMEDIATION",
    ]
    assert [line.split()[0] for line in result.stdout.splitlines()[2:]] == ["beta", "alpha"]
    assert registry.lookups == ["beta", "alpha"]
    assert "sentinel-alpha" not in result.output
    assert "sentinel-beta" not in result.output


def test_secret_verify_cli_mixed_batch_renders_all_rows_then_exits_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _Backend.values = {"resolved": "sentinel-resolved"}
    _install_cli_registry(monkeypatch, "resolved", "missing")

    result = CliRunner().invoke(app, ["secret", "verify", "resolved", "missing"])

    assert result.exit_code == 1
    rows = result.stdout.splitlines()[2:]
    assert [line.split()[0] for line in rows] == ["resolved", "missing"]
    assert "resolved" in rows[0]
    assert "unavailable" in rows[1]
    assert result.stderr == ""
    assert "sentinel-resolved" not in result.output


@pytest.mark.parametrize("separator", ["\u2028", "\u2029"])
def test_secret_verify_cli_unicode_separator_cannot_forge_a_row(
    monkeypatch: pytest.MonkeyPatch,
    separator: str,
) -> None:
    _SeparatorBackend.separator = separator
    _install_cli_registry(monkeypatch, "token")
    monkeypatch.setattr(
        "agentworks.secrets.resolve.active_sources",
        lambda config, candidate: [_source(backend_class=_SeparatorBackend)],
    )

    result = CliRunner().invoke(app, ["secret", "verify", "token"])

    assert result.exit_code == 1
    assert len(result.stdout.splitlines()) == 3
    assert "backend-protocol" in result.stdout
    assert "forged-row" not in result.output


def test_secret_verify_cli_default_refusal_does_not_read_global_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_cli_registry(monkeypatch, "token")
    monkeypatch.setattr(
        "agentworks.cli.commands.secret.output.non_interactive",
        lambda: pytest.fail("default refusal must not read global interaction state"),
    )

    result = CliRunner().invoke(app, ["secret", "verify", "token"])

    assert result.exit_code == 1
    assert "unavailable" in result.stdout


def test_secret_verify_cli_allow_interaction_resolves_interactive_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _Backend.values = {"token": "sentinel-interactive"}
    _install_cli_registry(monkeypatch, "token")
    monkeypatch.setattr(
        "agentworks.secrets.resolve.active_sources",
        lambda config, candidate: [_source(backend_class=_InteractiveBackend)],
    )

    result = CliRunner().invoke(app, ["secret", "verify", "token", "--allow-interaction"])

    assert result.exit_code == 0
    assert "resolved" in result.stdout
    assert "sentinel-interactive" not in result.output


def test_secret_verify_cli_onepassword_plugin_requires_and_honors_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="sentinel-op-value", stderr="")

    registry = _Registry(
        {
            "token": SecretDecl(
                name="token",
                description="token",
                backend_mappings={"work-op": "op://Work/item/password"},
            )
        }
    )
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr("agentworks.config.load_config", lambda: SimpleNamespace())
    monkeypatch.setattr("agentworks.bootstrap.load_request_registry", lambda config: registry)
    monkeypatch.setattr(
        "agentworks.secrets.resolve.active_sources",
        lambda config, candidate: [_onepassword_source()],
    )

    refused = CliRunner().invoke(app, ["secret", "verify", "token"])
    allowed = CliRunner().invoke(app, ["secret", "verify", "token", "--allow-interaction"])

    assert refused.exit_code == 1
    assert "refused-interaction" in refused.stdout
    assert calls == [["op", "read", "--no-newline", "op://Work/item/password"]]
    assert allowed.exit_code == 0
    assert "resolved" in allowed.stdout
    assert "sentinel-op-value" not in refused.output
    assert "sentinel-op-value" not in allowed.output


def test_secret_verify_entrypoint_prompt_user_abort_exits_one_without_table(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    abort = UserAbort("sentinel-entrypoint-user-abort")
    monkeypatch.setattr("agentworks.config.load_config", lambda: SimpleNamespace())
    monkeypatch.setattr("agentworks.bootstrap.load_request_registry", lambda config: _registry("token"))
    monkeypatch.setattr(
        "agentworks.secrets.resolve.active_sources",
        lambda config, registry: [_prompt_source()],
    )
    monkeypatch.setattr(output, "prompt_secret", lambda *args, **kwargs: (_ for _ in ()).throw(abort))

    exit_code = _run_main(monkeypatch, "secret", "verify", "token", "--allow-interaction")

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert captured.err == "Aborted.\n"
    assert "CATEGORY" not in captured.out
    assert "sentinel-entrypoint-user-abort" not in captured.err


def test_secret_verify_entrypoint_prompt_eof_exits_one_without_table(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("agentworks.config.load_config", lambda: SimpleNamespace())
    monkeypatch.setattr("agentworks.bootstrap.load_request_registry", lambda config: _registry("token"))
    monkeypatch.setattr(
        "agentworks.secrets.resolve.active_sources",
        lambda config, registry: [_prompt_source()],
    )
    monkeypatch.setattr(
        "click.termui.hidden_prompt_func",
        lambda prompt: (_ for _ in ()).throw(EOFError("sentinel-prompt-eof")),
    )

    exit_code = _run_main(monkeypatch, "secret", "verify", "token", "--allow-interaction")

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert captured.err.endswith("Aborted.\n")
    assert "CATEGORY" not in captured.out
    assert "sentinel-prompt-eof" not in captured.err


def test_provider_keyboard_interrupt_preserves_identity_cleanup_and_cli_exit_130(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    interrupt = KeyboardInterrupt("sentinel-provider-interrupt")
    _Backend.failure = interrupt
    monkeypatch.setattr(
        "agentworks.secrets.resolve.active_sources",
        lambda config, registry: [_source()],
    )
    with pytest.raises(KeyboardInterrupt) as caught:
        verify_secrets(
            SimpleNamespace(),  # type: ignore[arg-type]
            _registry("token"),  # type: ignore[arg-type]
            ["token"],
            interaction=InteractionPolicy.REFUSE,
        )
    assert caught.value is interrupt
    assert _Backend.events == ["factory", "enter", "prepare", "resolve", "exit"]

    _Backend.events = []
    _Backend.failure = KeyboardInterrupt("sentinel-cli-provider-interrupt")
    monkeypatch.setattr("agentworks.config.load_config", lambda: SimpleNamespace())
    monkeypatch.setattr("agentworks.bootstrap.load_request_registry", lambda config: _registry("token"))
    exit_code = _run_main(monkeypatch, "secret", "verify", "token")

    captured = capsys.readouterr()
    assert exit_code == 130
    assert captured.out == ""
    assert "CATEGORY" not in captured.out
    assert "sentinel" not in captured.err
    assert _Backend.events == ["factory", "enter", "prepare", "resolve", "exit"]


def test_secret_verify_cli_global_refusal_wins_before_config(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        "agentworks.config.load_config",
        lambda: pytest.fail("config must not load for conflicting interaction flags"),
    )
    exit_code = _run_main(
        monkeypatch,
        "--non-interactive",
        "secret",
        "verify",
        "token",
        "--allow-interaction",
    )
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "--allow-interaction cannot be used with --non-interactive" in captured.err
    assert captured.out == ""


@pytest.mark.parametrize("color", [False, True])
def test_secret_verify_cli_rejects_removed_flag(color: bool) -> None:
    result = CliRunner().invoke(
        app,
        ["secret", "verify", "token", "--allow-interactive"],
        color=color,
    )
    plain = _plain(result.stderr)
    assert result.exit_code == 2
    assert "No such option: --allow-interactive (Possible options: --allow-interaction)" in plain


def test_secret_verify_cli_requires_at_least_one_name() -> None:
    result = CliRunner().invoke(app, ["secret", "verify"])
    assert result.exit_code == 2
    assert "Missing argument 'NAMES...'" in result.stderr


def test_secret_verify_cli_invalid_name_has_no_table_or_echo(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("agentworks.config.load_config", lambda: SimpleNamespace())
    monkeypatch.setattr("agentworks.bootstrap.load_request_registry", lambda config: _registry())
    exit_code = _run_main(monkeypatch, "secret", "verify", "bad\nname")
    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert "bad\nname" not in captured.err
    assert "invalid secret name" in captured.err


def test_secret_verify_cli_unknown_name_has_no_table(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("agentworks.config.load_config", lambda: SimpleNamespace())
    monkeypatch.setattr("agentworks.bootstrap.load_request_registry", lambda config: _registry())
    exit_code = _run_main(monkeypatch, "secret", "verify", "missing")
    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert "secret 'missing' not found" in captured.err


def test_secret_verify_cli_provider_payload_never_reaches_renderer(monkeypatch: pytest.MonkeyPatch) -> None:
    _Backend.failure = RuntimeError("hostile-provider-sentinel")
    _install_cli_registry(monkeypatch, "token")
    result = CliRunner().invoke(app, ["secret", "verify", "token"])
    assert result.exit_code == 1
    assert "resolution-failure" in result.stdout
    assert "unexpected" in result.stdout
    assert "hostile-provider-sentinel" not in result.output
