"""Real Azure credentials over an isolated synthetic HTTP boundary."""

from __future__ import annotations

import base64
import io
import json
import os
import socket
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import azure.identity
import azure.keyvault.secrets
import pytest
import requests
from azure.core.pipeline.transport import RequestsTransport
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from requests.adapters import BaseAdapter

_TENANT = "00000000-0000-0000-0000-000000000002"
_CLIENT = "00000000-0000-0000-0000-000000000001"


def certificate_bytes(password: bytes | None = None) -> bytes:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "synthetic-local-only")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(datetime.now(UTC) - timedelta(days=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    encryption = serialization.NoEncryption() if password is None else serialization.BestAvailableEncryption(password)
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, encryption
    ) + certificate.public_bytes(serialization.Encoding.PEM)


class FileHTTP(BaseAdapter):
    def __init__(self, authority: str = "login.microsoftonline.com", resource: str = "vault.azure.net") -> None:
        self.authority = authority
        self.resource = resource
        self.requests: list[tuple[str, str]] = []
        self.forms: list[dict[str, list[str]]] = []
        self.on_token: Callable[[], object] | None = None
        self.expires_in = 0

    def send(self, request: requests.PreparedRequest, *args: Any, **kwargs: Any) -> requests.Response:
        url = str(request.url)
        self.requests.append((str(request.method), url))
        headers = {"Content-Type": "application/json"}
        status = 200
        body: dict[str, Any]
        base = f"https://{self.authority}/{_TENANT}/oauth2/v2.0/"
        if ".well-known/openid-configuration" in url:
            body = {"authorization_endpoint": base + "authorize", "token_endpoint": base + "token"}
        elif "/userrealm/" in url:
            body = {"account_type": "Managed"}
        elif urlsplit(url).hostname == self.authority:
            data = request.body.decode() if isinstance(request.body, bytes) else str(request.body)
            self.forms.append(parse_qs(data))
            # Short-lived tokens force real SDK refresh on subsequent requests.
            body = {"access_token": "synthetic-access-token", "expires_in": self.expires_in, "token_type": "Bearer"}
            if self.forms[-1].get("grant_type") == ["password"]:
                claims = {
                    "iss": f"https://{self.authority}/{_TENANT}",
                    "tid": _TENANT,
                    "sub": "synthetic-subject",
                    "aud": _CLIENT,
                    "preferred_username": "synthetic@example.test",
                }
                encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
                body["id_token"] = "e30." + encoded + "."
            if self.on_token is not None:
                self.on_token()
        elif not request.headers.get("Authorization"):
            status = 401
            headers["WWW-Authenticate"] = (
                f'Bearer authorization="https://{self.authority}/{_TENANT}", resource="https://{self.resource}"'
            )
            body = {}
        else:
            body = {"value": "synthetic-secret-value"}
        response = requests.Response()
        response.status_code = status
        response.headers.update(headers)
        response._content = json.dumps(body).encode()
        response.raw = io.BytesIO(response._content)
        response.request = request
        response.url = url
        return response

    def close(self) -> None:
        pass


class FileSDK:
    """Inject transport, keeping native constructors and token acquisition intact."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, http: FileHTTP | None = None) -> None:
        self.http = http or FileHTTP()
        self.constructed: list[str] = []
        self.closed: list[str] = []
        self.close_failure: str | None = None

        def forbid_network(*args: Any, **kwargs: Any) -> None:
            pytest.fail("unexpected real network access")

        monkeypatch.setattr(socket.socket, "connect", forbid_network)
        monkeypatch.setattr(socket, "getaddrinfo", forbid_network)

        def wrap(name: str, constructor: Any) -> Any:
            def create(*args: Any, **kwargs: Any) -> Any:
                session = requests.Session()
                session.trust_env = False
                session.mount("https://", self.http)
                kwargs["transport"] = RequestsTransport(session=session, session_owner=True)
                credential = constructor(*args, **kwargs)
                self.constructed.append(name)
                close = credential.close

                def tracked_close() -> None:
                    self.closed.append(name)
                    close()
                    if self.close_failure == name:
                        raise ValueError("synthetic-cleanup-diagnostic")

                monkeypatch.setattr(credential, "close", tracked_close)
                return credential

            return create

        for name in (
            "DefaultAzureCredential",
            "EnvironmentCredential",
            "CertificateCredential",
            "ClientAssertionCredential",
        ):
            monkeypatch.setattr(azure.identity, name, wrap(name, getattr(azure.identity, name)))
        monkeypatch.setattr(
            azure.keyvault.secrets, "SecretClient", wrap("SecretClient", azure.keyvault.secrets.SecretClient)
        )


def input_probe() -> None:
    """Measure only synthetic subprocess input; never invoked against the operator's stdin."""
    from agentworks.capabilities.secret_backend import (
        OperatorImpact,
        PreviewIntent,
        SecretLookupRequest,
        TtyInteractionAccess,
    )
    from agentworks.plugins.azure.keyvault import AzureKeyVaultBackend
    from agentworks.plugins.azure.keyvault_config import AzureKeyVaultMapping, AzureKeyVaultSourceConfig

    monkeypatch = pytest.MonkeyPatch()
    sdk = FileSDK(monkeypatch)
    path = Path(os.environ["PROBE_PATH"])
    if os.environ.get("PROBE_REPLACE_AFTER_CHECK"):
        original = os.fstat
        expected = path.stat()
        swapped = False

        def fstat(descriptor: int) -> os.stat_result:
            nonlocal swapped
            info = original(descriptor)
            if not swapped and (info.st_dev, info.st_ino) == (expected.st_dev, expected.st_ino):
                swapped = True
                path.unlink()
                path.symlink_to("/dev/stdin")
            return info

        monkeypatch.setattr(os, "fstat", fstat)
    if os.environ.get("PROBE_REPLACE_ON_REFRESH"):

        def replace() -> None:
            path.unlink()
            path.symlink_to("/dev/stdin")

        sdk.http.on_token = replace
    variable = (
        "AZURE_CLIENT_CERTIFICATE_PATH" if os.environ["PROBE_KIND"] == "certificate" else "AZURE_FEDERATED_TOKEN_FILE"
    )
    monkeypatch.setenv(variable, str(path))
    request = SecretLookupRequest("token", AzureKeyVaultMapping.model_validate("synthetic-token"))
    requests_to_make: tuple[SecretLookupRequest, ...] = (request,)
    if os.environ.get("PROBE_REPLACE_ON_REFRESH"):
        requests_to_make += (SecretLookupRequest("refreshed", request.mapping),)
    config = AzureKeyVaultSourceConfig(name="azure-keyvault", vault_url="https://probe-test.vault.azure.net")
    with AzureKeyVaultBackend.create_client(
        config=config,
        intent=PreviewIntent(OperatorImpact.NONE),
        tty_access=TtyInteractionAccess.DISABLED,
        interaction_broker=None,
    ) as client:
        result = client.preview(requests_to_make)
    print(
        json.dumps(
            {
                "results": {name: type(outcome).__name__ for name, outcome in result.items()},
                "remaining": len(sys.stdin.buffer.read()),
                "token_requests": len(sdk.http.forms),
                "constructed": sdk.constructed,
                "closed": sdk.closed,
            }
        )
    )
