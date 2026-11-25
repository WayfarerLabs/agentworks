"""Synthetic process input remains untouched by credential-file reads."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest

from tests.plugins._keyvault_file_support import _CLIENT, _TENANT, certificate_bytes

_LINUX = pytest.mark.skipif(sys.platform != "linux", reason="Linux descriptor aliases and named pipes")


def _probe(
    kind: str,
    path: Path,
    payload: bytes,
    *,
    fixture_dir: Path,
    stdin_path: Path | None = None,
    replace_after_check: bool = False,
    replace_on_refresh: bool = False,
    selector: str | None = None,
    client_secret: str | None = None,
) -> dict[str, Any]:
    isolated_home = fixture_dir / "probe-home"
    isolated_home.mkdir(exist_ok=True)
    environment = {
        "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
        "PATH": os.defpath,
        "HOME": str(isolated_home),
        "AZURE_CLIENT_ID": _CLIENT,
        "AZURE_TENANT_ID": _TENANT,
        "AZURE_TOKEN_CREDENTIALS": selector
        or ("EnvironmentCredential" if kind == "certificate" else "WorkloadIdentityCredential"),
        "PROBE_KIND": kind,
        "PROBE_PATH": str(path),
    }
    if sys.platform == "win32":
        environment["USERPROFILE"] = str(isolated_home)
        # Python's Windows subprocess runtime requires this for SxS assemblies.
        environment["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    if client_secret is not None:
        environment["AZURE_CLIENT_SECRET"] = client_secret
    if replace_after_check:
        environment["PROBE_REPLACE_AFTER_CHECK"] = "1"
    if replace_on_refresh:
        environment["PROBE_REPLACE_ON_REFRESH"] = "1"
    command = [sys.executable, "-c", "from tests.plugins._keyvault_file_support import input_probe; input_probe()"]
    if stdin_path is None:
        completed = subprocess.run(command, input=payload, capture_output=True, timeout=8, env=environment, check=False)
    else:
        with stdin_path.open("rb") as stdin:
            completed = subprocess.run(
                command, stdin=stdin, capture_output=True, timeout=8, env=environment, check=False
            )
    # The child's environment and credential inputs are synthetic; repr escapes
    # terminal control characters in its failure diagnostics.
    assert completed.returncode == 0, (
        f"synthetic credential probe exited {completed.returncode}; "
        f"stderr={completed.stderr.decode('utf-8', errors='replace')!r}"
    )
    assert completed.stderr == b""
    return cast("dict[str, Any]", json.loads(completed.stdout))


@_LINUX
@pytest.mark.parametrize("kind", ["certificate", "workload"])
@pytest.mark.parametrize("input_kind", ["stdin", "proc-fd", "device", "fifo", "symlink-fifo", "directory", "missing"])
def test_special_or_unreadable_credential_files_do_not_consume_piped_input(
    tmp_path: Path, kind: str, input_kind: str
) -> None:
    if input_kind == "stdin":
        path = Path("/dev/stdin")
    elif input_kind == "proc-fd":
        path = Path("/proc/self/fd/0")
    elif input_kind == "device":
        path = Path("/dev/null")
    elif input_kind in ("fifo", "symlink-fifo"):
        path = tmp_path / "credential.fifo"
        os.mkfifo(path)
        if input_kind == "symlink-fifo":
            link = tmp_path / "link"
            link.symlink_to(path)
            path = link
    elif input_kind == "directory":
        path = tmp_path
    else:
        path = tmp_path / "absent"
    payload = certificate_bytes() if kind == "certificate" else b"synthetic-stdin-assertion"
    result = _probe(kind, path, payload, fixture_dir=tmp_path)
    assert result["results"] == {"token": "PreviewIndeterminate"}
    assert result["remaining"] == len(payload)
    assert result["token_requests"] == 0


@_LINUX
@pytest.mark.parametrize("kind", ["certificate", "workload"])
@pytest.mark.parametrize("alias", ["/dev/stdin", "/proc/self/fd/0"])
def test_descriptor_aliases_are_rejected_with_regular_redirected_input(tmp_path: Path, kind: str, alias: str) -> None:
    payload = certificate_bytes() if kind == "certificate" else b"synthetic-stdin-assertion"
    stdin = tmp_path / "redirected-input"
    stdin.write_bytes(payload)
    result = _probe(kind, Path(alias), payload, fixture_dir=tmp_path, stdin_path=stdin)
    assert result["results"] == {"token": "PreviewIndeterminate"}
    assert result["remaining"] == len(payload)
    assert result["token_requests"] == 0


@pytest.mark.windows
@pytest.mark.parametrize("kind", ["certificate", "workload"])
def test_named_regular_file_is_allowed_when_stdin_references_the_same_file(tmp_path: Path, kind: str) -> None:
    payload = certificate_bytes() if kind == "certificate" else b"synthetic-assertion"
    path = tmp_path / "credential"
    path.write_bytes(payload)
    result = _probe(kind, path, payload, fixture_dir=tmp_path, stdin_path=path)
    assert result["results"] == {"token": "PreviewAvailable"}
    assert result["remaining"] == len(payload)
    assert result["token_requests"] == 1


@_LINUX
def test_certificate_path_replacement_after_fstat_does_not_cause_a_second_open(tmp_path: Path) -> None:
    certificate = tmp_path / "certificate.pem"
    certificate.write_bytes(certificate_bytes())
    payload = b"synthetic-stdin-marker"
    result = _probe("certificate", certificate, payload, fixture_dir=tmp_path, replace_after_check=True)
    assert result["results"] == {"token": "PreviewAvailable"}
    assert result["remaining"] == len(payload)


@_LINUX
def test_workload_path_replacement_is_checked_at_the_next_real_token_refresh(tmp_path: Path) -> None:
    token = tmp_path / "token.jwt"
    token.write_text("initial-assertion")
    payload = b"synthetic-stdin-marker"
    result = _probe("workload", token, payload, fixture_dir=tmp_path, replace_on_refresh=True)
    assert result["results"] == {"token": "PreviewAvailable", "refreshed": "PreviewIndeterminate"}
    assert result["remaining"] == len(payload)
    assert result["token_requests"] == 1


@_LINUX
@pytest.mark.parametrize("kind", ["certificate", "workload"])
def test_complete_client_secret_precedes_and_bypasses_file_credentials(tmp_path: Path, kind: str) -> None:
    payload = b"synthetic-stdin-marker"
    result = _probe(
        kind,
        Path("/dev/stdin"),
        payload,
        fixture_dir=tmp_path,
        selector="prod",
        client_secret="synthetic-client-secret",
    )
    assert result["results"] == {"token": "PreviewAvailable"}
    assert result["remaining"] == len(payload)
    assert result["token_requests"] == 1


@_LINUX
@pytest.mark.parametrize("kind", ["certificate", "workload"])
@pytest.mark.parametrize("selector", ["dev", "not-a-valid-selector"])
def test_selectors_are_applied_before_any_credential_file_read(tmp_path: Path, kind: str, selector: str) -> None:
    payload = b"synthetic-stdin-marker"
    result = _probe(kind, Path("/dev/stdin"), payload, fixture_dir=tmp_path, selector=selector)
    expected = "PreviewIndeterminate" if selector == "dev" else "PreviewFailed"
    assert result["results"] == {"token": expected}
    assert result["remaining"] == len(payload)
    assert result["token_requests"] == 0


@_LINUX
@pytest.mark.parametrize("kind", ["certificate", "workload"])
def test_empty_present_client_secret_does_not_fall_through_to_file_credentials(tmp_path: Path, kind: str) -> None:
    payload = b"synthetic-stdin-marker"
    result = _probe(kind, Path("/dev/stdin"), payload, fixture_dir=tmp_path, selector="prod", client_secret="")
    assert result["results"] == {"token": "PreviewFailed"}
    assert result["remaining"] == len(payload)
    assert result["token_requests"] == 0
