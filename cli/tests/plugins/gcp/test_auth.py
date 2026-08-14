"""GCP credential selection, privacy, and per-kind client caching."""

from __future__ import annotations

import json
from typing import Any

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.errors import ProvisioningError
from agentworks.plugins.gcp.auth import (
    GcpClientCache,
    build_ambient_credential,
    build_service_account_credential,
)
from agentworks.plugins.gcp.config import GcpGCEConfig, GcpServiceAccountAuth
from agentworks.schema import RefOwner, filled_defaults

_SENTINEL = "private-key-'quote-\"-newline\\n-SENTINEL"


class _Secrets:
    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    def get(self, name: str) -> str:
        return self._values[name]


def _ctx(name: str = "svc-json", value: str = _SENTINEL) -> RunContext:
    return RunContext(secrets=_Secrets({name: value}))  # type: ignore[arg-type]


def _config(auth: dict[str, object]) -> GcpGCEConfig:
    raw = {
        "name": "gcp-gce",
        "project_id": "project-a",
        "zone": "us-central1-a",
        "auth": auth,
    }
    filled = filled_defaults(GcpGCEConfig, raw, RefOwner("vm-site", "gcp-site"))
    return GcpGCEConfig.model_validate(filled)


def _exception_graph(root: BaseException) -> list[object]:
    seen: set[int] = set()
    pending: list[object] = [root]
    found: list[object] = []
    while pending:
        value = pending.pop()
        if id(value) in seen:
            continue
        seen.add(id(value))
        found.append(value)
        if isinstance(value, BaseException):
            pending.extend(value.args)
            pending.extend(v for v in vars(value).values() if v is not None)
            if value.__cause__ is not None:
                pending.append(value.__cause__)
            if value.__context__ is not None:
                pending.append(value.__context__)
        elif isinstance(value, dict):
            pending.extend(value.keys())
            pending.extend(value.values())
        elif isinstance(value, list | tuple | set | frozenset):
            pending.extend(value)
    return found


def _assert_sentinel_absent(exc: BaseException) -> None:
    assert _SENTINEL not in "\n".join(f"{value!s}\n{value!r}" for value in _exception_graph(exc))
    assert exc.__cause__ is None
    assert exc.__context__ is None


def test_ambient_uses_adc_with_cloud_platform_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    credential = object()
    calls: list[dict[str, object]] = []

    def fake_default(**kwargs: object) -> tuple[object, str]:
        calls.append(kwargs)
        return credential, "credential-project"

    monkeypatch.setattr("google.auth.default", fake_default)
    assert build_ambient_credential("gcp-site") is credential
    assert calls == [{"scopes": ("https://www.googleapis.com/auth/cloud-platform",)}]


def test_ambient_failure_is_safe_and_detached(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = RuntimeError(_SENTINEL)
    provider.__cause__ = ValueError(_SENTINEL)
    monkeypatch.setattr("google.auth.default", lambda **_kwargs: (_ for _ in ()).throw(provider))

    with pytest.raises(ProvisioningError) as caught:
        build_ambient_credential("gcp-site")

    assert "gcp-site" in str(caught.value)
    _assert_sentinel_absent(caught.value)


def test_service_account_receives_whole_json_and_cloud_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    document = json.dumps({"type": "service_account", "client_email": "svc@example.test", "private_key": _SENTINEL})
    credential = object()
    calls: list[tuple[dict[str, object], tuple[str, ...]]] = []

    def fake_from_info(info: dict[str, object], *, scopes: tuple[str, ...]) -> object:
        calls.append((info, scopes))
        return credential

    monkeypatch.setattr(
        "google.oauth2.service_account.Credentials.from_service_account_info",
        fake_from_info,
    )
    auth = GcpServiceAccountAuth(mode="service-account", secret="svc-json")

    assert build_service_account_credential(auth, document, "gcp-site") is credential
    assert calls == [
        (
            {"type": "service_account", "client_email": "svc@example.test", "private_key": _SENTINEL},
            ("https://www.googleapis.com/auth/cloud-platform",),
        )
    ]


@pytest.mark.parametrize(
    "newline",
    [pytest.param("\n", id="lf"), pytest.param("\r\n", id="crlf")],
)
def test_real_env_source_and_operation_resolver_deliver_exact_downloaded_json(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    newline: str,
) -> None:
    from agentworks.bootstrap import build_registry
    from agentworks.capabilities.base import RunContext
    from agentworks.config import load_config
    from agentworks.orchestration.secrets import ScopedSecrets
    from agentworks.secrets.policy import InteractionPolicy
    from agentworks.secrets.resolver import Resolver
    from tests.conftest import write_cfg

    document = newline.join(
        (
            "{",
            '  "type": "service_account",',
            '  "client_email": "svc@example.test",',
            '  "private_key": "-----BEGIN PRIVATE KEY-----\\nSENTINEL\\n-----END PRIVATE KEY-----\\n"',
            "}",
            "",
        )
    )
    config = load_config(
        write_cfg(
            tmp_path,
            settings='[secret_config]\nsources = ["env-var"]\n',
        ),
        warn_issues=False,
        warn_deprecations=False,
    )
    registry = build_registry(config)
    monkeypatch.setenv("AW_SECRET_SVC_JSON", document)
    resolver = Resolver(config, registry, interaction=InteractionPolicy.REFUSE)
    resolver.register_name("svc-json")
    resolver.resolve()

    parsed_inputs: list[str] = []
    real_loads = json.loads

    def recording_loads(value: str) -> object:
        parsed_inputs.append(value)
        return real_loads(value)

    monkeypatch.setattr("agentworks.plugins.gcp.auth.json.loads", recording_loads)
    factory_inputs: list[dict[str, object]] = []
    credential = object()

    def fake_from_info(info: dict[str, object], *, scopes: tuple[str, ...]) -> object:
        factory_inputs.append(info)
        assert scopes == ("https://www.googleapis.com/auth/cloud-platform",)
        return credential

    monkeypatch.setattr(
        "google.oauth2.service_account.Credentials.from_service_account_info",
        fake_from_info,
    )
    cache = GcpClientCache(
        "gcp-site",
        _config({"mode": "service-account", "secret": "svc-json"}),
    )
    ctx = RunContext(secrets=ScopedSecrets(resolver.values, ("svc-json",)))

    assert cache.credential(ctx) is credential
    assert parsed_inputs == [document]
    assert document.endswith(newline)
    assert factory_inputs == [
        {
            "type": "service_account",
            "client_email": "svc@example.test",
            "private_key": "-----BEGIN PRIVATE KEY-----\nSENTINEL\n-----END PRIVATE KEY-----\n",
        }
    ]


def test_service_account_remediation_accepts_downloaded_json_without_compaction() -> None:
    auth = GcpServiceAccountAuth(mode="service-account", secret="svc-json")
    with pytest.raises(ProvisioningError) as caught:
        build_service_account_credential(auth, "not-json", "gcp-site")

    assert caught.value.hint is not None
    assert "exactly as downloaded" in caught.value.hint
    assert "do not compact" in caught.value.hint


@pytest.mark.parametrize(
    "document",
    [
        _SENTINEL,
        f'"{_SENTINEL}"',
        "[]",
    ],
    ids=("malformed", "scalar", "array"),
)
def test_malformed_service_account_is_secret_free(document: str) -> None:
    auth = GcpServiceAccountAuth(mode="service-account", secret="svc-json")
    with pytest.raises(ProvisioningError) as caught:
        build_service_account_credential(auth, document, "gcp-site")
    assert "svc-json" in str(caught.value)
    _assert_sentinel_absent(caught.value)


def test_sdk_validation_failure_drops_entire_exception_graph(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = ValueError(_SENTINEL)
    provider.__cause__ = RuntimeError(_SENTINEL)

    def fail(*_args: object, **_kwargs: object) -> object:
        raise provider

    monkeypatch.setattr("google.oauth2.service_account.Credentials.from_service_account_info", fail)
    auth = GcpServiceAccountAuth(mode="service-account", secret="svc-json")
    document = json.dumps({"type": "service_account", "private_key": _SENTINEL})

    with pytest.raises(ProvisioningError) as caught:
        build_service_account_credential(auth, document, "gcp-site")

    _assert_sentinel_absent(caught.value)


def test_json_decoder_recursion_failure_drops_entire_exception_graph(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = RecursionError(_SENTINEL)
    provider.__cause__ = ValueError(_SENTINEL)
    monkeypatch.setattr("json.loads", lambda _value: (_ for _ in ()).throw(provider))
    auth = GcpServiceAccountAuth(mode="service-account", secret="svc-json")

    with pytest.raises(ProvisioningError) as caught:
        build_service_account_credential(auth, _SENTINEL, "gcp-site")

    _assert_sentinel_absent(caught.value)


def test_deeply_nested_json_is_a_typed_detached_auth_failure() -> None:
    auth = GcpServiceAccountAuth(mode="service-account", secret="svc-json")
    document = "[" * 2000 + "0" + "]" * 2000

    with pytest.raises(ProvisioningError) as caught:
        build_service_account_credential(auth, document, "gcp-site")

    assert "svc-json" in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_explicit_mode_never_calls_ambient_and_failed_build_is_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config({"mode": "service-account", "secret": "svc-json"})
    cache = GcpClientCache("gcp-site", config)
    calls = {"ambient": 0, "explicit": 0}

    def ambient(_site: str) -> object:
        calls["ambient"] += 1
        return object()

    def explicit(*_args: object) -> object:
        calls["explicit"] += 1
        raise ProvisioningError("safe")

    monkeypatch.setattr("agentworks.plugins.gcp.auth.build_ambient_credential", ambient)
    monkeypatch.setattr("agentworks.plugins.gcp.auth.build_service_account_credential", explicit)

    with pytest.raises(ProvisioningError):
        cache.credential(_ctx())
    with pytest.raises(ProvisioningError):
        cache.credential(_ctx())

    assert calls == {"ambient": 0, "explicit": 2}
    assert cache._credential_cached is None
    assert _SENTINEL not in repr(vars(cache))


def test_credential_and_each_concrete_client_are_cached_once(monkeypatch: pytest.MonkeyPatch) -> None:
    from google.cloud import compute_v1

    config = _config({"mode": "ambient"})
    cache = GcpClientCache("gcp-site", config)
    credential = object()
    credential_calls: list[str] = []
    builds: list[tuple[str, object]] = []

    def ambient(site: str) -> object:
        credential_calls.append(site)
        return credential

    monkeypatch.setattr("agentworks.plugins.gcp.auth.build_ambient_credential", ambient)

    kinds = {
        "projects": "ProjectsClient",
        "zones": "ZonesClient",
        "networks": "NetworksClient",
        "subnetworks": "SubnetworksClient",
        "machine-types": "MachineTypesClient",
        "images": "ImagesClient",
        "instances": "InstancesClient",
        "firewalls": "FirewallsClient",
    }
    for _kind, class_name in kinds.items():
        constructor = type(
            class_name,
            (),
            {
                "__init__": lambda self, *, credentials, _name=class_name: builds.append((_name, credentials)),
            },
        )
        monkeypatch.setattr(compute_v1, class_name, constructor)

    first: dict[str, Any] = {}
    for kind in kinds:
        first[kind] = cache.client(kind, RunContext())  # type: ignore[arg-type]
        assert cache.client(kind, RunContext()) is first[kind]  # type: ignore[arg-type]

    assert credential_calls == ["gcp-site"]
    assert builds == [(class_name, credential) for class_name in kinds.values()]
    assert set(cache._clients) == set(kinds)


def test_client_construction_failure_is_mode_named_detached_and_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    from google.cloud import compute_v1

    config = _config({"mode": "service-account", "secret": "svc-json"})
    cache = GcpClientCache("gcp-site", config)
    provider = RuntimeError(_SENTINEL)
    provider.__cause__ = ValueError(_SENTINEL)

    class FailingProjects:
        def __init__(self, **_kwargs: object) -> None:
            raise provider

    monkeypatch.setattr("agentworks.plugins.gcp.auth.build_service_account_credential", lambda *_args: object())
    monkeypatch.setattr(compute_v1, "ProjectsClient", FailingProjects)

    with pytest.raises(ProvisioningError) as caught:
        cache.client("projects", _ctx())

    assert "gcp-site" in str(caught.value)
    assert "service-account" in str(caught.value)
    assert "svc-json" in str(caught.value)
    _assert_sentinel_absent(caught.value)
    assert cache._clients == {}


def test_gcp_import_publishes_only_through_installed_plugin_registration() -> None:
    from agentworks.capabilities.vm_platform import VM_PLATFORM_REGISTRY
    from agentworks.plugins import SYSTEM_PLUGINS

    assert "gcp" in SYSTEM_PLUGINS
    assert "gcp-gce" in VM_PLATFORM_REGISTRY
    assert SYSTEM_PLUGINS["gcp"].capabilities["vm-platform"] == (VM_PLATFORM_REGISTRY["gcp-gce"],)
