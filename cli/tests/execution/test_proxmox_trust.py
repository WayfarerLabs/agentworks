"""Explicit Proxmox trust against an owned loopback HTTPS endpoint."""

from __future__ import annotations

import ssl
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path, PurePath
from unittest.mock import MagicMock

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from agentworks.errors import ValidationError
from agentworks.execution.carrier import CarrierIO, Deadline, Dispatch, ExitStatus, Failure, PreparedInvocation
from agentworks.execution.carriers.proxmox import ProxmoxCarrier, ProxmoxConnection

_TOKEN = "synthetic-secret-token"


def _certificate(
    key: ec.EllipticCurvePrivateKey,
    signer: ec.EllipticCurvePrivateKey,
    subject: x509.Name,
    issuer: x509.Name,
    *,
    ca: bool,
) -> x509.Certificate:
    now = datetime.now(UTC)
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=0 if ca else None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=ca,
                crl_sign=ca,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(signer.public_key()), critical=False)
    )
    if not ca:
        builder = builder.add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        builder = builder.add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
    return builder.sign(signer, hashes.SHA256())


class _TLSServer(HTTPServer):
    def __init__(self, context: ssl.SSLContext, handler: type[BaseHTTPRequestHandler]) -> None:
        self.context = context
        self.connections = 0
        super().__init__(("127.0.0.1", 0), handler)

    def get_request(self) -> tuple[ssl.SSLSocket, tuple[str, int]]:
        connection, address = super().get_request()
        self.connections += 1
        connection.settimeout(2)
        try:
            return self.context.wrap_socket(connection, server_side=True), address
        except BaseException:
            connection.close()
            raise


@dataclass
class _Endpoint:
    server: _TLSServer
    ca_bundle: Path
    unknown_ca: Path
    requests: list[tuple[str, str, str | None]]

    @property
    def url(self) -> str:
        return f"https://localhost:{self.server.server_port}"


@pytest.fixture
def endpoint(tmp_path: Path) -> Iterator[_Endpoint]:
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Fixture cluster CA")])
    ca_bundle = tmp_path / "cluster CA.pem"
    ca_bundle.write_bytes(
        _certificate(ca_key, ca_key, ca_name, ca_name, ca=True).public_bytes(serialization.Encoding.PEM)
    )
    other_key = ec.generate_private_key(ec.SECP256R1())
    unknown_ca = tmp_path / "other CA.pem"
    unknown_ca.write_bytes(
        _certificate(other_key, other_key, ca_name, ca_name, ca=True).public_bytes(serialization.Encoding.PEM)
    )
    server_key = ec.generate_private_key(ec.SECP256R1())
    server_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    certificate = tmp_path / "server.pem"
    certificate.write_bytes(
        _certificate(server_key, ca_key, server_name, ca_name, ca=False).public_bytes(serialization.Encoding.PEM)
    )
    private_key = tmp_path / "server-key.pem"
    private_key.write_bytes(
        server_key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate, private_key)
    requests: list[tuple[str, str, str | None]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers["Content-Length"]))
            self.reply(b'{"data":{"pid":42}}')

        def do_GET(self) -> None:
            self.reply(b'{"data":{"exited":1,"exitcode":0}}')

        def reply(self, body: bytes) -> None:
            requests.append((self.command, self.path, self.headers.get("Authorization")))
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    with _TLSServer(context, Handler) as server:
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        try:
            yield _Endpoint(server, ca_bundle, unknown_ca, requests)
        finally:
            server.shutdown()
            thread.join(timeout=5)
            assert not thread.is_alive()


def _execute(url: str, ca_bundle: Path | None):
    connection = ProxmoxConnection(url, "node1", 123, "test@pve!token", _TOKEN, ca_bundle=ca_bundle)
    return ProxmoxCarrier(connection).execute(
        PreparedInvocation(("/bin/true",)), io=CarrierIO(), deadline=Deadline.after(5)
    )


# These exercise workstation TLS, path serialization and the real owned worker.
@pytest.mark.windows
def test_matching_host_and_explicit_ca_deliver_authenticated_request(endpoint: _Endpoint) -> None:
    report = _execute(endpoint.url, endpoint.ca_bundle)
    assert report.failure is None
    assert report.completion == ExitStatus(code=0)
    assert endpoint.requests == [
        ("POST", "/api2/json/nodes/node1/qemu/123/agent/exec", f"PVEAPIToken=test@pve!token={_TOKEN}"),
        ("GET", "/api2/json/nodes/node1/qemu/123/agent/exec-status?pid=42", f"PVEAPIToken=test@pve!token={_TOKEN}"),
    ]


@pytest.mark.windows
def test_none_uses_normal_default_trust(endpoint: _Endpoint, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SSL_CERT_FILE", str(endpoint.ca_bundle))
    report = _execute(endpoint.url, None)
    assert report.failure is None
    assert report.completion == ExitStatus(code=0)
    assert len(endpoint.requests) == 2


@pytest.mark.windows
def test_explicit_ca_does_not_fall_back_to_default_trust(endpoint: _Endpoint, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SSL_CERT_FILE", str(endpoint.ca_bundle))
    report = _execute(endpoint.url, endpoint.unknown_ca)
    assert report.failure == Failure.DISPATCH
    assert report.completion is None
    assert not endpoint.requests


@pytest.mark.windows
@pytest.mark.parametrize("failure", ["unknown-ca", "default-trust", "hostname", "missing-ca", "invalid-ca"])
def test_invalid_trust_never_dispatches_http(endpoint: _Endpoint, tmp_path: Path, failure: str) -> None:
    url = endpoint.url
    bundle: Path | None = endpoint.ca_bundle
    if failure == "unknown-ca":
        bundle = endpoint.unknown_ca
    elif failure == "default-trust":
        bundle = None
    elif failure == "hostname":
        url = f"https://127.0.0.1:{endpoint.server.server_port}"
    elif failure == "missing-ca":
        bundle = tmp_path / "missing.pem"
    else:
        bundle = tmp_path / "invalid.pem"
        bundle.write_text(_TOKEN)
    report = _execute(url, bundle)
    assert report.failure == Failure.DISPATCH
    assert report.dispatch == Dispatch.UNKNOWN
    assert report.completion is None
    assert not endpoint.requests
    assert report.stdout.data == report.stderr.data == b""
    assert _TOKEN not in repr(report)
    if failure in ("missing-ca", "invalid-ca"):
        assert endpoint.server.connections == 0
    else:
        assert endpoint.server.connections == 1


@pytest.mark.parametrize("bundle", ["/tmp/ca.pem", False, PurePath("ca.pem"), Path("invalid\0ca.pem")])
def test_ca_bundle_requires_a_path_without_network(monkeypatch: pytest.MonkeyPatch, bundle: object) -> None:
    worker = MagicMock()
    monkeypatch.setattr("subprocess.Popen", worker)
    with pytest.raises(ValidationError) as raised:
        ProxmoxConnection("https://pve.example", "node1", 123, "token", _TOKEN, ca_bundle=bundle)
    assert raised.value.__cause__ is None and raised.value.__context__ is None
    worker.assert_not_called()


@pytest.mark.parametrize("field", ["token_id", "token_secret"])
@pytest.mark.parametrize(
    "value", ["", None, 42, "synthetic-secret\r", "synthetic-secret\n", "synthetic-secret\r\ninjected"]
)
def test_invalid_tokens_are_refused_without_disclosure(
    monkeypatch: pytest.MonkeyPatch, field: str, value: object
) -> None:
    worker = MagicMock()
    monkeypatch.setattr("subprocess.Popen", worker)
    tokens = {"token_id": "test@pve!token", "token_secret": _TOKEN, field: value}
    with pytest.raises(ValidationError) as raised:
        ProxmoxConnection("https://pve.example", "node1", 123, **tokens)
    assert "synthetic-secret" not in repr(raised.value)
    assert raised.value.__cause__ is None and raised.value.__context__ is None
    worker.assert_not_called()
