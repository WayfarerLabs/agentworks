"""WSL2 ``create`` rollback on failure and interrupt (#340).

``create`` makes the install directory, imports the rootfs as a distro,
and configures it in-guest (packages, swap, user, systemd) before a
final restart. The caller (``create_vm``) deletes only the DB row on
failure or interrupt, so a distro or install directory left behind
would be orphaned with nothing left to target it. ``create`` therefore
runs the delete op's teardown sequence (the shared
``_teardown_distro``: ``wsl --unregister`` + install-dir removal) on
plain failure AND on interrupt, re-raising the original; a SECOND
interrupt during the cleanup abandons it loudly, naming the removal
command. The azure precedent is test_azure_create_interrupt.py (#338).

The rootfs-download atomicity suite at the bottom covers the cache the
rollback deliberately keeps: a failed download must never leave a
truncated tarball at the cache path.

The backend seams (module-level ``_wsl`` / ``_powershell`` /
``_download_debian_rootfs``, and the Docker-registry HTTP round trips
for the download suite) are monkeypatched; no test runs wsl.exe or
touches the network.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.capabilities.vm_platform import ProvisionRequest, wsl2
from agentworks.capabilities.vm_platform.wsl2 import WSL2Platform
from agentworks.errors import StateError

if TYPE_CHECKING:
    from tests.conftest import CapturedOutput


@pytest.fixture(autouse=True)
def _local_app_data(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The install path resolves under %LOCALAPPDATA%; point it at a
    real temp dir so path handling stays honest off Windows."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))


def _request() -> ProvisionRequest:
    return ProvisionRequest(
        vm_name="vm1",
        hostname="vm1",
        system_slug=None,
        admin_username="agw",
        ssh_public_key="ssh-ed25519 AAAA test",
        ssh_private_key=None,
        # wsl2 always defers Tailscale to Phase A.
        tailscale_auth_key=None,
    )


class _Calls:
    """Recorded backend invocations, in one sequence-preserving log."""

    def __init__(self) -> None:
        # ("wsl", <joined args>) or ("ps", <script>), in call order.
        self.events: list[tuple[str, str]] = []

    def wsl(self, needle: str) -> list[str]:
        return [c for kind, c in self.events if kind == "wsl" and needle in c]

    def ps(self, needle: str) -> list[str]:
        return [c for kind, c in self.events if kind == "ps" and needle in c]


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    *,
    errors: dict[str, BaseException] | None = None,
) -> _Calls:
    """Mock the backend seams; return the recorded calls.

    ``errors`` maps a substring (of the joined wsl args or the
    PowerShell script) to the exception the seam raises when it sees it
    (the call is still recorded first, so tests can pin exactly-once
    attempts).
    """
    calls = _Calls()

    def _raise_if_matched(text: str) -> None:
        for needle, exc in (errors or {}).items():
            if needle in text:
                raise exc

    def _fake_wsl(args: list[str], *, check: bool = True, timeout: int = 300) -> str:
        joined = " ".join(args)
        calls.events.append(("wsl", joined))
        _raise_if_matched(joined)
        return ""

    def _fake_powershell(script: str, *, check: bool = True, timeout: int = 120) -> str:
        calls.events.append(("ps", script))
        _raise_if_matched(script)
        return ""

    monkeypatch.setattr(wsl2, "_wsl", _fake_wsl)
    monkeypatch.setattr(wsl2, "_powershell", _fake_powershell)
    monkeypatch.setattr(wsl2, "_download_debian_rootfs", lambda tarball: None)
    monkeypatch.setattr(WSL2Platform, "_distro_exists", staticmethod(lambda name: False))
    return calls


def _assert_teardown_ran(calls: _Calls) -> None:
    assert calls.wsl("--unregister vm1") == ["--unregister vm1"]
    (remove,) = calls.ps("Remove-Item")
    assert str(wsl2._wsl_base_path() / "vm1") in remove


def test_failure_mid_provision_cleans_up_and_reraises(
    monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
) -> None:
    """A plain backend failure (here: the in-guest package install, the
    longest step) unregisters the distro and removes the install
    directory, then re-raises unwrapped per wsl2's error convention;
    the interrupt messaging never appears."""
    calls = _wire(monkeypatch, errors={"apt-get": RuntimeError("apt exploded")})

    with pytest.raises(RuntimeError, match="apt exploded"):
        WSL2Platform("wsl2", {}).create(_request(), RunContext())

    _assert_teardown_ran(calls)
    assert not any("Interrupted" in w for w in captured_output.warnings)
    assert not any("Cleanup abandoned" in w for w in captured_output.warnings)


def test_interrupt_during_the_final_restart_cleans_up_and_reraises_the_original(
    monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
) -> None:
    """A Ctrl-C at the very end of the span (the systemd restart boot)
    still tears down the fully imported distro, and the ORIGINAL
    interrupt propagates for the caller's row unwind (identity pin)."""
    interrupt = KeyboardInterrupt("first")
    calls = _wire(monkeypatch, errors={"echo ok": interrupt})

    with pytest.raises(KeyboardInterrupt) as exc:
        WSL2Platform("wsl2", {}).create(_request(), RunContext())

    assert exc.value is interrupt
    _assert_teardown_ran(calls)
    assert any("Ctrl-C again to abandon" in w for w in captured_output.warnings)


def test_second_interrupt_abandons_cleanup_loudly(
    monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
) -> None:
    """A second Ctrl-C during the cleanup abandons it instead of
    wedging: the unregister is attempted exactly once, the install-dir
    removal is never reached, the warning names the exact removal
    command, and the ORIGINAL interrupt still propagates."""
    interrupt = KeyboardInterrupt("first")
    calls = _wire(
        monkeypatch,
        errors={"echo ok": interrupt, "--unregister": KeyboardInterrupt("second")},
    )

    with pytest.raises(KeyboardInterrupt) as exc:
        WSL2Platform("wsl2", {}).create(_request(), RunContext())

    assert exc.value is interrupt
    assert len(calls.wsl("--unregister")) == 1
    assert calls.ps("Remove-Item") == []
    (abandoned,) = [w for w in captured_output.warnings if "Cleanup abandoned" in w]
    assert "'wsl --unregister vm1'" in abandoned


def test_cleanup_failure_warns_and_does_not_mask_the_original(
    monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
) -> None:
    """The rollback is best-effort: a broken teardown warns with the
    manual removal command and the ORIGINAL failure still propagates."""
    calls = _wire(
        monkeypatch,
        errors={"apt-get": RuntimeError("original failure"), "--unregister": RuntimeError("cleanup broke")},
    )

    with pytest.raises(RuntimeError, match="original failure"):
        WSL2Platform("wsl2", {}).create(_request(), RunContext())

    assert len(calls.wsl("--unregister")) == 1
    warned = "\n".join(captured_output.warnings)
    assert "could not clean up the partial WSL2 distro 'vm1'" in warned
    assert "'wsl --unregister vm1'" in warned


def test_pre_mutation_failure_makes_no_cleanup_calls(
    monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
) -> None:
    """A failure before anything is created (the name-collision
    pre-flight) must not fire any teardown: there is nothing of ours to
    delete, and the colliding distro is NOT ours to touch."""
    calls = _wire(monkeypatch)
    monkeypatch.setattr(WSL2Platform, "_distro_exists", staticmethod(lambda name: True))

    with pytest.raises(StateError, match="already registered"):
        WSL2Platform("wsl2", {}).create(_request(), RunContext())

    assert calls.events == []


# -- Rootfs-download atomicity ----------------------------------------------
#
# The cache survives the create rollback by design, which is only safe if
# the cache path can never hold a truncated tarball: a corrupt cache would
# poison every retried create with a baffling `wsl --import` error. The
# download therefore writes to a temp name and renames into place.


class _JsonResp:
    """Context-manager stand-in for a urllib response carrying JSON."""

    def __init__(self, payload: object) -> None:
        self._payload = json.dumps(payload).encode()

    def __enter__(self) -> _JsonResp:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self._payload


class _BlobResp:
    """Blob response streaming ``chunks``, then raising ``error`` (or
    ending normally when ``error`` is None)."""

    def __init__(self, chunks: list[bytes], error: BaseException | None = None) -> None:
        self._chunks = list(chunks)
        self._error = error

    def __enter__(self) -> _BlobResp:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self, _n: int = -1) -> bytes:
        if self._chunks:
            return self._chunks.pop(0)
        if self._error is not None:
            raise self._error
        return b""


def _stub_registry(monkeypatch: pytest.MonkeyPatch, blob: _BlobResp) -> None:
    """Stub the Docker Hub round trips: token, single-arch manifest (no
    ``manifests`` list, so no second manifest fetch), then the blob."""

    def _fake_urlopen(url_or_req: object) -> _JsonResp:
        url = url_or_req if isinstance(url_or_req, str) else getattr(url_or_req, "full_url", "")
        if "auth.docker.io" in url:
            return _JsonResp({"token": "t"})
        return _JsonResp({"layers": [{"digest": "sha256:abc", "size": 8}]})

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    monkeypatch.setattr(wsl2, "_blob_opener", SimpleNamespace(open=lambda req: blob))


@pytest.mark.parametrize("error", [RuntimeError("network died"), KeyboardInterrupt("mid-download")])
def test_failed_download_leaves_no_file_at_the_cache_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    captured_output: CapturedOutput,
    error: BaseException,
) -> None:
    """A download that dies mid-stream (network failure or Ctrl-C) must
    leave the cache path empty: no truncated tarball, no temp leftover."""
    _stub_registry(monkeypatch, _BlobResp([b"x" * 1024], error=error))
    tarball = tmp_path / "debian-bookworm-amd64-rootfs.tar.gz"

    with pytest.raises(type(error)):
        wsl2._download_debian_rootfs(tarball)

    assert not tarball.exists()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("error", [OSError("rename died"), KeyboardInterrupt("in the rename window")])
def test_failed_rename_leaves_no_residue_either(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    captured_output: CapturedOutput,
    error: BaseException,
) -> None:
    """The no-residue guarantee covers the rename window too: a failure
    (or Ctrl-C) in os.replace itself unlinks the completed .partial and
    leaves nothing at the final path."""
    _stub_registry(monkeypatch, _BlobResp([b"rootfs-bytes"]))

    def _explode(src: object, dst: object) -> None:
        raise error

    # wsl2 calls os.replace through its module-level `import os`, so
    # patching the os module itself intercepts it (reverted by pytest).
    monkeypatch.setattr("os.replace", _explode)
    tarball = tmp_path / "debian-bookworm-amd64-rootfs.tar.gz"

    with pytest.raises(type(error)):
        wsl2._download_debian_rootfs(tarball)

    assert not tarball.exists()
    assert list(tmp_path.iterdir()) == []


def test_completed_download_lands_at_the_cache_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, captured_output: CapturedOutput
) -> None:
    """The rename lands the complete tarball at the final name, with no
    temp file left beside it."""
    _stub_registry(monkeypatch, _BlobResp([b"rootfs-", b"bytes"]))
    tarball = tmp_path / "debian-bookworm-amd64-rootfs.tar.gz"

    wsl2._download_debian_rootfs(tarball)

    assert tarball.read_bytes() == b"rootfs-bytes"
    assert list(tmp_path.iterdir()) == [tarball]


def test_delete_op_issues_the_shared_teardown_sequence(
    monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
) -> None:
    """delete() is unchanged by the dedup: unregister first, then the
    install-dir removal, exactly the sequence the rollback also uses."""
    calls = _wire(monkeypatch)
    vm = SimpleNamespace(name="vm1", platform_metadata={"distro_name": "vm1"})

    WSL2Platform("wsl2", {}).delete(vm, RunContext())  # type: ignore[arg-type]

    kinds_and_calls = calls.events
    assert len(kinds_and_calls) == 2
    assert kinds_and_calls[0] == ("wsl", "--unregister vm1")
    assert kinds_and_calls[1][0] == "ps"
    assert "Remove-Item" in kinds_and_calls[1][1]
    assert str(wsl2._wsl_base_path() / "vm1") in kinds_and_calls[1][1]
