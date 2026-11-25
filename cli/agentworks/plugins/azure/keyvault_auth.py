"""Ambient Azure credentials with checked file-backed inputs."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping
    from contextlib import ExitStack

    from azure.core.credentials import TokenCredential


def _descriptor_alias(path: Path) -> bool:
    return (
        re.fullmatch(
            r"/(?:dev/(?:stdin|stdout|stderr|fd/[0-9]+)|proc/(?:self|thread-self|[0-9]+)/fd/[0-9]+)", path.as_posix()
        )
        is not None
    )


def _read_regular_file(path: str) -> bytes:
    """Read an environment-selected credential file through one checked descriptor.

    Opening nonblocking prevents FIFO aliases from hanging where supported.
    Resolve ordinary symlinks for projected workload token volumes, while
    rejecting descriptor aliases. The opened descriptor's type gates reading;
    no standard-input descriptor is inspected or read.
    """
    from azure.core.exceptions import ClientAuthenticationError

    try:
        configured = Path(path).absolute()
        if _descriptor_alias(configured):
            raise ClientAuthenticationError("ambient Azure credential input cannot be a descriptor alias")
        resolved = configured.resolve(strict=True)
        if _descriptor_alias(resolved):
            raise ClientAuthenticationError("ambient Azure credential input cannot be a descriptor alias")
        flags = os.O_RDONLY
        for name in ("O_NONBLOCK", "O_NOCTTY", "O_CLOEXEC", "O_BINARY", "O_NOFOLLOW"):
            flags |= getattr(os, name, 0)
        descriptor = os.open(resolved, flags)
        primary_failed = False
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                raise ClientAuthenticationError("ambient Azure credential input must be a readable regular file")
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                return stream.read()
        except BaseException:
            primary_failed = True
            raise
        finally:
            try:
                os.close(descriptor)
            except OSError:
                # Cleanup must not replace a primary cancellation or failure.
                if not primary_failed:
                    raise
    except (OSError, ValueError, RuntimeError):
        raise ClientAuthenticationError("ambient Azure credential file could not be read") from None


def _read_assertion(path: str) -> str:
    """Read each refreshed assertion through the same file boundary."""
    from azure.core.exceptions import ClientAuthenticationError

    try:
        # Match the SDK's UTF-8 text-mode universal newline handling.
        return _read_regular_file(path).decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    except UnicodeError:
        raise ClientAuthenticationError("ambient Azure workload assertion is not UTF-8") from None


def _environment_uses_certificate() -> bool:
    """Apply native EnvironmentCredential precedence before opening any file."""
    client_secret = ("AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET")
    certificate = ("AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_CERTIFICATE_PATH")
    return not all(name in os.environ for name in client_secret) and all(name in os.environ for name in certificate)


def _workload_is_configured() -> bool:
    return all(os.environ.get(name) for name in ("AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_FEDERATED_TOKEN_FILE"))


def ambient_credential(
    *,
    authority: str,
    selector: str,
    exclusions: Mapping[str, bool],
    resources: ExitStack,
) -> TokenCredential:
    """Own a guarded deployed prefix and the remaining native default chain.

    The native default implementation remains responsible for selectors and
    non-file credential behavior. File-backed deployed credentials use public
    byte/assertion APIs so the SDK never reopens an unchecked path. Directly
    constructed credentials have independent cleanup ownership; the public
    composed chain is used only to request tokens, never for aggregate close.
    """
    from azure.identity import (
        CertificateCredential,
        ChainedTokenCredential,
        ClientAssertionCredential,
        DefaultAzureCredential,
        EnvironmentCredential,
    )

    environment_selected = selector in ("", "prod", "environmentcredential")
    workload_selected = selector in ("", "prod", "workloadidentitycredential")
    certificate = environment_selected and _environment_uses_certificate()
    workload = workload_selected and _workload_is_configured()
    if not certificate and not workload:
        credential = DefaultAzureCredential(
            authority=authority,
            process_timeout=10,
            connection_timeout=10,
            read_timeout=30,
            retry_total=2,
            logging_enable=False,
            **exclusions,
        )
        resources.callback(credential.close)
        return credential

    credentials: list[TokenCredential] = []
    if environment_selected:
        if certificate:
            environment: CertificateCredential | EnvironmentCredential = CertificateCredential(
                tenant_id=os.environ["AZURE_TENANT_ID"],
                client_id=os.environ["AZURE_CLIENT_ID"],
                certificate_data=_read_regular_file(os.environ["AZURE_CLIENT_CERTIFICATE_PATH"]),
                password=os.environ.get("AZURE_CLIENT_CERTIFICATE_PASSWORD"),
                send_certificate_chain=bool(os.environ.get("AZURE_CLIENT_SEND_CERTIFICATE_CHAIN", False)),
                authority=authority,
                connection_timeout=10,
                read_timeout=30,
                retry_total=2,
                logging_enable=False,
            )
        else:
            environment = EnvironmentCredential(
                authority=authority,
                connection_timeout=10,
                read_timeout=30,
                retry_total=2,
                logging_enable=False,
            )
        resources.callback(environment.close)
        credentials.append(environment)

    if workload:
        path = os.environ["AZURE_FEDERATED_TOKEN_FILE"]
        assertion = ClientAssertionCredential(
            tenant_id=os.environ["AZURE_TENANT_ID"],
            client_id=os.environ["AZURE_CLIENT_ID"],
            func=lambda: _read_assertion(path),
            authority=authority,
            connection_timeout=10,
            read_timeout=30,
            retry_total=2,
            logging_enable=False,
        )
        resources.callback(assertion.close)
        credentials.append(assertion)

    if selector not in ("environmentcredential", "workloadidentitycredential"):
        suffix = DefaultAzureCredential(
            authority=authority,
            process_timeout=10,
            connection_timeout=10,
            read_timeout=30,
            retry_total=2,
            logging_enable=False,
            exclude_environment_credential=True,
            exclude_workload_identity_credential=True,
            **exclusions,
        )
        resources.callback(suffix.close)
        credentials.append(suffix)
    return credentials[0] if len(credentials) == 1 else ChainedTokenCredential(*credentials)
