"""File-backed ambient credentials preserve the source's input boundary."""

from __future__ import annotations

import base64
import concurrent.futures
import errno
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from azure.core.exceptions import ClientAuthenticationError

from agentworks.capabilities.secret_backend import (
    FailureReason,
    OperatorImpact,
    PreviewAvailable,
    PreviewFailed,
    PreviewIndeterminate,
    PreviewIntent,
    SecretLookupRequest,
    TtyInteractionAccess,
)
from agentworks.plugins.azure.keyvault import AzureKeyVaultBackend
from agentworks.plugins.azure.keyvault_auth import _read_regular_file
from agentworks.plugins.azure.keyvault_config import AzureKeyVaultMapping, AzureKeyVaultSourceConfig
from agentworks.resources.graph import Readiness
from agentworks.schema import CapabilityBlock
from agentworks.secrets import SecretDecl, SecretSourceDecl
from agentworks.secrets.resolve import ActiveSource, resolve_batch
from tests.plugins._keyvault_file_support import _CLIENT, _TENANT, FileHTTP, FileSDK, certificate_bytes


@pytest.fixture(autouse=True)
def isolated_azure_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "AZURE_TOKEN_CREDENTIALS",
        "AZURE_TENANT_ID",
        "AZURE_CLIENT_ID",
        "AZURE_CLIENT_SECRET",
        "AZURE_CLIENT_CERTIFICATE_PATH",
        "AZURE_CLIENT_CERTIFICATE_PASSWORD",
        "AZURE_CLIENT_SEND_CERTIFICATE_CHAIN",
        "AZURE_FEDERATED_TOKEN_FILE",
        "AZURE_USERNAME",
        "AZURE_PASSWORD",
        "AZURE_AUTHORITY_HOST",
        "IDENTITY_ENDPOINT",
        "IDENTITY_HEADER",
        "IDENTITY_SERVER_THUMBPRINT",
        "IMDS_ENDPOINT",
        "MSI_ENDPOINT",
        "MSI_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AZURE_CLIENT_ID", _CLIENT)
    monkeypatch.setenv("AZURE_TENANT_ID", _TENANT)


def _context(vault: str = "https://file-test.vault.azure.net") -> Any:
    return AzureKeyVaultBackend.create_client(
        config=AzureKeyVaultSourceConfig(name="azure-keyvault", vault_url=vault),
        intent=PreviewIntent(OperatorImpact.NONE),
        tty_access=TtyInteractionAccess.DISABLED,
        interaction_broker=None,
    )


def _request(name: str = "token") -> SecretLookupRequest:
    return SecretLookupRequest(name, AzureKeyVaultMapping.model_validate("synthetic-token"))


@pytest.mark.windows
@pytest.mark.parametrize("selector", ["", "prod", "EnvironmentCredential"])
def test_native_certificate_uses_checked_bytes_with_password_and_chain(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, selector: str
) -> None:
    certificate = tmp_path / "certificate.pem"
    certificate.write_bytes(certificate_bytes(b"synthetic-password"))
    monkeypatch.setenv("AZURE_CLIENT_CERTIFICATE_PATH", str(certificate))
    monkeypatch.setenv("AZURE_CLIENT_CERTIFICATE_PASSWORD", "synthetic-password")
    # Native EnvironmentCredential treats every non-empty string as true.
    monkeypatch.setenv("AZURE_CLIENT_SEND_CERTIFICATE_CHAIN", "false")
    monkeypatch.setenv("AZURE_TOKEN_CREDENTIALS", selector)
    sdk = FileSDK(monkeypatch)
    with _context() as client:
        result = client.preview((_request(),))
    assert result == {"token": PreviewAvailable()}
    [form] = sdk.http.forms
    assert form["client_id"] == [_CLIENT]
    assertion = form["client_assertion"][0]
    header = json.loads(base64.urlsafe_b64decode(assertion.split(".")[0] + "=="))
    assert header["x5c"]
    assert "CertificateCredential" in sdk.closed
    if selector == "EnvironmentCredential":
        assert "DefaultAzureCredential" not in sdk.constructed


@pytest.mark.windows
@pytest.mark.parametrize("selector", ["", "prod", "WorkloadIdentityCredential"])
def test_native_workload_supplier_reads_a_regular_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, selector: str
) -> None:
    token = tmp_path / "token.jwt"
    token.write_text("synthetic-assertion")
    monkeypatch.setenv("AZURE_FEDERATED_TOKEN_FILE", str(token))
    monkeypatch.setenv("AZURE_TOKEN_CREDENTIALS", selector)
    sdk = FileSDK(monkeypatch)
    with _context() as client:
        result = client.preview((_request(),))
    assert result == {"token": PreviewAvailable()}
    assert sdk.http.forms[0]["client_assertion"] == ["synthetic-assertion"]
    assert "ClientAssertionCredential" in sdk.closed
    if selector == "WorkloadIdentityCredential":
        assert "DefaultAzureCredential" not in sdk.constructed


@pytest.mark.windows
@pytest.mark.parametrize(
    ("resource", "authority"),
    [
        ("vault.azure.net", "login.microsoftonline.com"),
        ("vault.usgovcloudapi.net", "login.microsoftonline.us"),
        ("vault.azure.cn", "login.chinacloudapi.cn"),
    ],
)
def test_guarded_workload_uses_the_vault_cloud_authority(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, resource: str, authority: str
) -> None:
    token = tmp_path / "token.jwt"
    token.write_text("synthetic-assertion")
    monkeypatch.setenv("AZURE_FEDERATED_TOKEN_FILE", str(token))
    monkeypatch.setenv("AZURE_TOKEN_CREDENTIALS", "WorkloadIdentityCredential")
    sdk = FileSDK(monkeypatch, FileHTTP(authority, resource))
    with _context(f"https://authority-test.{resource}") as client:
        assert client.preview((_request(),)) == {"token": PreviewAvailable()}
    assert any(
        url.startswith(f"https://{authority}/{_TENANT}/") for method, url in sdk.http.requests if method == "POST"
    )


@pytest.mark.windows
def test_workload_rotation_is_read_on_each_actual_token_refresh(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    token = tmp_path / "token.jwt"
    token.write_text("first-assertion")
    monkeypatch.setenv("AZURE_FEDERATED_TOKEN_FILE", str(token))
    monkeypatch.setenv("AZURE_TOKEN_CREDENTIALS", "WorkloadIdentityCredential")
    sdk = FileSDK(monkeypatch)
    sdk.http.on_token = lambda: token.write_text("rotated-assertion")
    with _context() as client:
        result = client.preview((_request("first"), _request("second")))
    assert result == {"first": PreviewAvailable(), "second": PreviewAvailable()}
    assert [form["client_assertion"] for form in sdk.http.forms] == [["first-assertion"], ["rotated-assertion"]]


@pytest.mark.windows
@pytest.mark.parametrize("kind", ["certificate", "workload"])
def test_regular_file_symlinks_are_supported(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, kind: str) -> None:
    target = tmp_path / "credential"
    target.write_bytes(certificate_bytes() if kind == "certificate" else b"synthetic-assertion")
    link = tmp_path / "projected-volume"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("host does not permit file symlinks")
    variable = "AZURE_CLIENT_CERTIFICATE_PATH" if kind == "certificate" else "AZURE_FEDERATED_TOKEN_FILE"
    selector = "EnvironmentCredential" if kind == "certificate" else "WorkloadIdentityCredential"
    monkeypatch.setenv(variable, str(link))
    monkeypatch.setenv("AZURE_TOKEN_CREDENTIALS", selector)
    sdk = FileSDK(monkeypatch)
    with _context() as client:
        assert client.preview((_request(),)) == {"token": PreviewAvailable()}
    assert len(sdk.http.forms) == 1


@pytest.mark.windows
def test_selected_workload_bypasses_an_invalid_environment_certificate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    token = tmp_path / "token.jwt"
    token.write_text("synthetic-assertion")
    monkeypatch.setenv("AZURE_CLIENT_CERTIFICATE_PATH", str(tmp_path / "missing-certificate"))
    monkeypatch.setenv("AZURE_FEDERATED_TOKEN_FILE", str(token))
    monkeypatch.setenv("AZURE_TOKEN_CREDENTIALS", "WorkloadIdentityCredential")
    sdk = FileSDK(monkeypatch)
    with _context() as client:
        assert client.preview((_request(),)) == {"token": PreviewAvailable()}
    assert "CertificateCredential" not in sdk.constructed


@pytest.mark.windows
def test_bad_selected_certificate_does_not_fall_through_to_workload(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    token = tmp_path / "token.jwt"
    token.write_text("synthetic-assertion")
    monkeypatch.setenv("AZURE_CLIENT_CERTIFICATE_PATH", str(tmp_path / "missing-certificate"))
    monkeypatch.setenv("AZURE_FEDERATED_TOKEN_FILE", str(token))
    monkeypatch.setenv("AZURE_TOKEN_CREDENTIALS", "prod")
    sdk = FileSDK(monkeypatch)
    with _context() as client:
        result = client.preview((_request(),))
    assert isinstance(result["token"], PreviewIndeterminate)
    assert sdk.http.forms == []
    assert "ClientAssertionCredential" not in sdk.constructed


@pytest.mark.windows
@pytest.mark.parametrize("kind", ["certificate", "workload"])
def test_malformed_regular_file_contents_remain_value_free_failures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, kind: str, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "malformed"
    path.write_bytes(b"synthetic-sensitive-value\xff")
    variable = "AZURE_CLIENT_CERTIFICATE_PATH" if kind == "certificate" else "AZURE_FEDERATED_TOKEN_FILE"
    selector = "EnvironmentCredential" if kind == "certificate" else "WorkloadIdentityCredential"
    monkeypatch.setenv(variable, str(path))
    monkeypatch.setenv("AZURE_TOKEN_CREDENTIALS", selector)
    sdk = FileSDK(monkeypatch)
    with _context() as client:
        result = client.preview((_request(),))
    assert "synthetic-sensitive-value" not in repr(result) + caplog.text
    assert sdk.http.forms == []
    if kind == "certificate":
        assert result == {"token": PreviewFailed(FailureReason.EXTERNAL)}
    else:
        assert isinstance(result["token"], PreviewIndeterminate)


def test_partially_constructed_prefix_is_closed_when_a_later_constructor_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import azure.identity

    token = tmp_path / "token.jwt"
    token.write_text("synthetic-assertion")
    monkeypatch.setenv("AZURE_FEDERATED_TOKEN_FILE", str(token))
    sdk = FileSDK(monkeypatch)

    def fail(*args: Any, **kwargs: Any) -> None:
        raise ValueError("synthetic-construction-diagnostic")

    monkeypatch.setattr(azure.identity, "ClientAssertionCredential", fail)
    with _context() as client:
        result = client.preview((_request(),))
    assert result == {"token": PreviewFailed(FailureReason.EXTERNAL)}
    assert sdk.closed == ["EnvironmentCredential"]


def test_prefix_cleanup_failure_does_not_skip_other_owned_credentials_or_change_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    token = tmp_path / "token.jwt"
    token.write_text("synthetic-assertion")
    monkeypatch.setenv("AZURE_FEDERATED_TOKEN_FILE", str(token))
    sdk = FileSDK(monkeypatch)
    sdk.close_failure = "ClientAssertionCredential"
    config = AzureKeyVaultSourceConfig(name="azure-keyvault", vault_url="https://cleanup-test.vault.azure.net")
    source = ActiveSource(
        source=SecretSourceDecl(
            name="work-kv", backend=CapabilityBlock.of("azure-keyvault", vault_url=config.vault_url)
        ),
        backend_class=AzureKeyVaultBackend,
        config=config,
        readiness=Readiness.ready(),
    )
    batch = resolve_batch(
        [SecretDecl(name="token", description="test", backend_mappings={"work-kv": "synthetic-token"})],
        [source],
        tty_access=TtyInteractionAccess.DISABLED,
        interaction_broker=None,
    )
    assert batch.complete_or_raise() == {"token": "synthetic-secret-value"}
    assert sdk.closed == [
        "SecretClient",
        "DefaultAzureCredential",
        "ClientAssertionCredential",
        "EnvironmentCredential",
    ]
    captured = capsys.readouterr()
    assert "synthetic-cleanup-diagnostic" not in captured.out + captured.err


@pytest.mark.parametrize("guarded", [False, True])
def test_native_username_password_authentication_is_preserved(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, guarded: bool
) -> None:
    if guarded:
        monkeypatch.setenv("AZURE_FEDERATED_TOKEN_FILE", str(tmp_path / "unneeded-assertion"))
        monkeypatch.setenv("AZURE_TOKEN_CREDENTIALS", "prod")
    else:
        monkeypatch.delenv("AZURE_TENANT_ID")
        monkeypatch.setenv("AZURE_CLIENT_CERTIFICATE_PATH", str(tmp_path / "absent"))
        monkeypatch.setenv("AZURE_TOKEN_CREDENTIALS", "EnvironmentCredential")
    monkeypatch.setenv("AZURE_USERNAME", "synthetic@example.test")
    monkeypatch.setenv("AZURE_PASSWORD", "synthetic-password")
    sdk = FileSDK(monkeypatch)
    sdk.http.expires_in = 3600
    with _context() as client:
        result = client.preview((_request(),))
    assert result == {"token": PreviewAvailable()}, sdk.http.requests
    assert sdk.http.forms[0]["username"] == ["synthetic@example.test"]
    assert sdk.http.forms[0]["password"] == ["synthetic-password"]


@pytest.mark.windows
def test_credential_read_matches_native_parent_segment_resolution(tmp_path: Path) -> None:
    target = tmp_path / "target"
    nested = target / "nested"
    nested.mkdir(parents=True)
    physical = b"synthetic-physical-parent"
    lexical = b"synthetic-lexical-parent"
    (target / "credential").write_bytes(physical)
    (tmp_path / "credential").write_bytes(lexical)
    link = tmp_path / "directory-link"
    try:
        link.symlink_to(nested, target_is_directory=True)
    except OSError:
        pytest.skip("host does not permit directory symlinks")
    configured = link / ".." / "credential"
    native = configured.read_bytes()
    assert native in (physical, lexical)
    assert _read_regular_file(str(configured)) == native


def _close_then_fail(monkeypatch: pytest.MonkeyPatch, failure: BaseException | None = None) -> list[int]:
    real_close = os.close
    closed: list[int] = []

    def close(descriptor: int) -> None:
        real_close(descriptor)
        closed.append(descriptor)
        if failure is not None:
            raise failure
        raise OSError(errno.EIO, "synthetic-sensitive-close-diagnostic")

    monkeypatch.setattr(os, "close", close)
    return closed


@pytest.mark.windows
@pytest.mark.parametrize("cancellation", [KeyboardInterrupt(), concurrent.futures.CancelledError()])
def test_file_close_failure_preserves_primary_cancellation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cancellation: BaseException
) -> None:
    path = tmp_path / "credential"
    path.write_bytes(b"synthetic-credential")
    real_fdopen = os.fdopen
    opened: list[int] = []

    class InterruptedRead:
        def read(self) -> bytes:
            raise cancellation

    @contextmanager
    def fdopen(descriptor: int, *args: Any, **kwargs: Any) -> Iterator[InterruptedRead]:
        with real_fdopen(descriptor, *args, **kwargs):
            opened.append(descriptor)
            yield InterruptedRead()

    monkeypatch.setattr(os, "fdopen", fdopen)
    closed = _close_then_fail(monkeypatch)
    with pytest.raises(type(cancellation)) as raised:
        _read_regular_file(str(path))
    assert raised.value is cancellation
    assert closed == opened
    assert len(closed) == 1
    with pytest.raises(OSError) as released:
        os.fstat(closed[0])
    assert released.value.errno == errno.EBADF


@pytest.mark.windows
def test_standalone_file_close_failure_is_an_opaque_authentication_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "credential"
    path.write_bytes(b"synthetic-credential")
    closed = _close_then_fail(monkeypatch)
    with pytest.raises(ClientAuthenticationError) as raised:
        _read_regular_file(str(path))
    assert "synthetic-sensitive-close-diagnostic" not in str(raised.value)
    assert len(closed) == 1
    with pytest.raises(OSError) as released:
        os.fstat(closed[0])
    assert released.value.errno == errno.EBADF


@pytest.mark.windows
@pytest.mark.parametrize("cancellation", [KeyboardInterrupt(), SystemExit(17)])
def test_new_cancellation_during_close_escapes_an_ordinary_read_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cancellation: BaseException
) -> None:
    path = tmp_path / "credential"
    path.write_bytes(b"synthetic-credential")
    real_fdopen = os.fdopen

    class FailedRead:
        def read(self) -> bytes:
            raise OSError(errno.EIO, "synthetic-read-failure")

    @contextmanager
    def fdopen(descriptor: int, *args: Any, **kwargs: Any) -> Iterator[FailedRead]:
        with real_fdopen(descriptor, *args, **kwargs):
            yield FailedRead()

    monkeypatch.setattr(os, "fdopen", fdopen)
    closed = _close_then_fail(monkeypatch, cancellation)
    with pytest.raises(type(cancellation)) as raised:
        _read_regular_file(str(path))
    assert raised.value is cancellation
    assert len(closed) == 1
    with pytest.raises(OSError) as released:
        os.fstat(closed[0])
    assert released.value.errno == errno.EBADF
