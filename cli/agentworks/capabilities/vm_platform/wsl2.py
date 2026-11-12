"""The WSL2 VM platform: imports Debian distros on Windows."""

from __future__ import annotations

import contextlib
import functools
import json
import os
import platform
import re
import shlex
import subprocess
import sys
import time
import urllib.request
from pathlib import Path, PureWindowsPath
from typing import TYPE_CHECKING, Any, ClassVar, Literal, NoReturn

from agentworks import output
from agentworks.capabilities.vm_platform.base import (
    ProvisionRequest,
    ProvisionResult,
    RetainedProvisioningError,
    VMPlatform,
)
from agentworks.capabilities.vm_platform.bootstrap_script import run_bootstrap
from agentworks.capabilities.vm_platform.debian_release import code_owned_release_value
from agentworks.capabilities.vm_platform.tailscale_join import (
    CREATE_RERUN_HINT,
    join_tailscale,
    settle_tailnet_address,
)
from agentworks.db import VMStatus
from agentworks.debian import DebianRelease
from agentworks.errors import ExternalError, StateError, ValidationError
from agentworks.execution import root_script, ssh_process
from agentworks.programs import resolve_program
from agentworks.schema import AgwModel
from agentworks.terminal import sanitize_terminal_output
from agentworks.topics import TopicProse

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from contextlib import AbstractContextManager

    from agentworks.capabilities.base import RunContext
    from agentworks.config import Config
    from agentworks.db import VMRow
    from agentworks.execution.root_script import RawRun
    from agentworks.resources.graph import Readiness

_STATUS_TIMEOUT_SECONDS = 10
_BOOTSTRAP_DEADLINE_SECONDS = 900


def wsl_program(*, windows: bool, environ: Mapping[str, str]) -> str:
    """The wsl.exe to run: its absolute path on Windows, never a bare name.

    Windows looks a bare name up in the current directory before ``PATH``,
    and this process is handed the Tailscale auth key on stdin, so it is
    named the way the OpenSSH resolver names ssh. Elsewhere (only tests build
    WSL2 argvs there) the bare name stands in.
    """
    if not windows:
        return "wsl"
    return str(PureWindowsPath(environ.get("SystemRoot") or r"C:\Windows", "System32", "wsl.exe"))


# The one place the wsl.exe program is named.
_WSL = wsl_program(windows=sys.platform == "win32", environ=os.environ)
# The error code other WSL releases print, with a non-zero status, for a
# listing when no distro is installed (not measured here); the one token of
# their localized output this module reads, on either stream, as wsl.exe prints
# its own errors on stdout. WSL 2.7.14.0 (measured) instead exits 0 with no
# output on either stream, which reads as an ordinary empty listing.
_NO_DISTRIBUTIONS = "WSL_E_DEFAULT_DISTRO_NOT_FOUND"

# The root channel's program inside the distro: Bash in a session of its own, with no controlling terminal.
_DETACHED_ROOT_SHELL = ("/usr/bin/setsid", "-w", "/bin/bash", "-s")


def _run_as_root(distro_name: str, script: bytes, deadline: float) -> RawRun:
    """The distro's root channel: ``wsl.exe --user root --exec setsid -w /bin/bash -s`` with ``script`` on stdin.

    ``--exec`` starts the program directly, so root's login shell and its
    startup files are not between ``wsl.exe`` and Bash, and ``--user root``
    needs no sudo. ``wsl.exe`` still makes the program a session leader with
    a pseudo-terminal as its controlling terminal, whatever its own streams
    are, so ``setsid`` (util-linux, essential in Debian) runs Bash in a new
    session with none: a ``dpkg`` maintainer script that reads or resets
    ``/dev/tty`` cannot stop the script. ``-w`` keeps the channel open until
    Bash exits; Bash inherits all three streams.
    """
    argv = [
        _WSL,
        "--distribution",
        distro_name,
        "--user",
        "root",
        "--exec",
        *_DETACHED_ROOT_SHELL,
    ]
    return root_script.run_process(argv, script, deadline)


# -- Win32 job-object machinery for orphan-proof subprocess cleanup ----------
#
# Without this, an `agw` process that gets hard-killed (SIGKILL, console
# window closed, Python crash) leaves its `wsl.exe sleep infinity` subprocess
# orphaned and still anchoring the WSL2 distro, defeating the user's
# expectation that idle-shutdown resumes after the command dies. Windows'
# job-object KILL_ON_JOB_CLOSE flag exists exactly for this case: when the
# last handle to the job closes (which the OS guarantees on process death,
# however the death happens), all processes assigned to the job are killed
# by the kernel.
#
# Wired up lazily on Windows; on other platforms _kernel32 stays None and
# every helper short-circuits to a no-op. Failures during ctypes setup are
# swallowed so that an unusual Windows configuration doesn't break WSL2
# provisioning: we fall back to terminate-only cleanup, accepting the
# orphan-on-crash risk that mode brings.

_kernel32 = None
_JOBOBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = None
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JobObjectExtendedLimitInformation = 9

if sys.platform == "win32":
    try:
        import ctypes
        from ctypes import wintypes

        _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        _kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        _kernel32.SetInformationJobObject.restype = wintypes.BOOL
        _kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]
        _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        _kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        _kernel32.CloseHandle.restype = wintypes.BOOL
        _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        class _IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", ctypes.c_ulong),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_ulong),
                ("Affinity", ctypes.c_void_p),
                ("PriorityClass", ctypes.c_ulong),
                ("SchedulingClass", ctypes.c_ulong),
            ]

        class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", _IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        _JOBOBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION
    except Exception:
        # Best-effort: any failure leaves us without orphan cleanup but does
        # not break the rest of the provisioner.
        _kernel32 = None
        _JOBOBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = None


def _create_kill_on_close_job() -> int | None:
    """Create a Win32 Job Object configured to kill its members on handle close.

    Returns the HANDLE (as int) on success, None if Win32 is unavailable or
    any call fails. The caller owns the handle and is responsible for closing
    it via :func:`_close_handle` once the keepalive subprocess no longer
    needs to be anchored.
    """
    if _kernel32 is None or _JOBOBJECT_EXTENDED_LIMIT_INFORMATION_CLASS is None:
        return None
    import ctypes

    h_job = _kernel32.CreateJobObjectW(None, None)
    if not h_job:
        return None
    info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION_CLASS()
    info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not _kernel32.SetInformationJobObject(
        h_job,
        _JobObjectExtendedLimitInformation,
        ctypes.byref(info),
        ctypes.sizeof(info),
    ):
        _kernel32.CloseHandle(h_job)
        return None
    return int(h_job)


def _assign_process_to_job(h_job: int, h_process: int) -> bool:
    """Assign a process HANDLE to a Job Object. Returns True on success."""
    if _kernel32 is None:
        return False
    return bool(_kernel32.AssignProcessToJobObject(h_job, h_process))


def _close_handle(h: int | None) -> None:
    """Close a Win32 HANDLE. Safe no-op when the handle is None or Win32 is unavailable."""
    if _kernel32 is None or not h:
        return
    _kernel32.CloseHandle(h)


def _local_app_data() -> Path:
    """Return %LOCALAPPDATA% as a resolved Path.

    PowerShell does not expand %VAR% syntax (that's cmd.exe), and neither does
    wsl.exe, so we must resolve LOCALAPPDATA in Python before handing paths to
    either tool. Falls back to ExpandEnvironmentVariables for parity with
    Windows tooling if the env var is missing (very unusual on Windows).
    """
    base = os.environ.get("LOCALAPPDATA") or os.path.expandvars("%LOCALAPPDATA%")
    if not base or base == "%LOCALAPPDATA%":
        raise RuntimeError("LOCALAPPDATA environment variable is not set")
    return Path(base)


def _wsl_base_path() -> Path:
    """Root install directory for agentworks-managed WSL2 distros."""
    return _local_app_data() / "agentworks" / "wsl"


def _cache_dir() -> Path:
    """Cache directory for downloaded rootfs tarballs."""
    return _local_app_data() / "agentworks" / "cache"


def _ps_quote(path: Path | str) -> str:
    """Quote a path for safe inclusion in a PowerShell single-quoted string."""
    return "'" + str(path).replace("'", "''") + "'"


# Docker Hub OCI registry endpoints for the official Debian image
_DOCKER_AUTH_URL = "https://auth.docker.io/token?service=registry.docker.io&scope=repository:library/debian:pull"
_DOCKER_MANIFESTS_URL = "https://registry-1.docker.io/v2/library/debian/manifests"
_DOCKER_BLOBS_URL = "https://registry-1.docker.io/v2/library/debian/blobs"
_DEBIAN_OCI_TAGS: dict[DebianRelease, str] = {
    DebianRelease.TRIXIE: "trixie",
}
# Map Python's platform.machine() to OCI architecture names
_ARCH_MAP = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}


def _oci_arch() -> str:
    """Return the OCI architecture name for the host machine."""
    machine = platform.machine().lower()
    arch = _ARCH_MAP.get(machine)
    if arch is None:
        raise RuntimeError(f"Unsupported architecture: {machine}")
    return arch


class _StripAuthRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Strip Authorization header when following redirects to a different host.

    Docker Hub blob requests return a 302 to a CDN. The CDN rejects the
    Bearer token with 400 Bad Request, so we must drop it on redirect.
    """

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> urllib.request.Request | None:
        new_req = super().redirect_request(req, fp, code, msg, headers, newurl)  # type: ignore[arg-type]
        if new_req is not None:
            new_req.remove_header("Authorization")
        return new_req


_blob_opener = urllib.request.build_opener(_StripAuthRedirectHandler)


class WSLCommandFailed(ExternalError):
    """A ``wsl.exe`` command exited non-zero; the message quotes what it printed."""


# The hostnames WSL keeps as given: its init cleans ``network.hostname`` to
# alphanumerics joined by single hyphens (dropping underscores and collapsing
# repeated hyphens), and the guest would then disagree with ``vms.hostname``.
_WSL_HOSTNAME_RE = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")


def _wsl_conf(admin_username: str, hostname: str) -> str:
    """The distro's ``/etc/wsl.conf``: the default user, systemd, and the hostname.

    Without ``[network] hostname`` WSL names the guest after the Windows
    computer at every distro start, and a later Tailscale join takes that name.
    """
    return f"[user]\ndefault={admin_username}\n\n[boot]\nsystemd=true\n\n[network]\nhostname={hostname}\n"


def _wsl(args: list[str], *, check: bool = True, timeout: int = 300) -> str:
    """Run a wsl.exe command and return stdout.

    wsl.exe emits UTF-16LE on redirected output unless WSL_UTF8=1 is
    set; decoding that as UTF-8 leaves a NUL after every ASCII char,
    which silently breaks the name-equality parsers (``status``,
    ``_distro_exists``). Strip the NULs so both encodings parse. A failure
    under ``check`` raises :class:`WSLCommandFailed` with wsl.exe's own text,
    which it prints on stdout, not stderr.
    """
    result = _run_wsl(args, timeout=timeout)
    if check and result.returncode != 0:
        printed = "\n".join(text for stream in (result.stderr, result.stdout) if (text := stream.strip()))
        # No argument carries a secret: the auth key reaches the distro on the root channel's stdin.
        command = sanitize_terminal_output(shlex.join(["wsl", *args]))
        reason = sanitize_terminal_output(printed) or "no output"
        raise WSLCommandFailed(
            f"'{command}' exited with status {result.returncode}: {reason}",
            hint=(
                "WSL may have been shut down meanwhile ('wsl --shutdown' stops every distro). "
                "Check it with 'wsl --status', then run the command again."
            ),
        )
    return result.stdout


def _run_wsl(args: list[str], *, timeout: int = 300) -> subprocess.CompletedProcess[str]:
    """Run a wsl.exe command once, both streams captured with their NULs stripped (see :func:`_wsl`)."""
    result = subprocess.run(
        [_WSL, *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout
    )
    return subprocess.CompletedProcess(
        result.args, result.returncode, result.stdout.replace("\x00", ""), result.stderr.replace("\x00", "")
    )


def _powershell(script: str, *, check: bool = True, timeout: int = 120) -> str:
    """Run a PowerShell command and return stdout."""
    result = subprocess.run(
        [resolve_program("powershell"), "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"PowerShell failed: {result.stderr.strip()}")
    return result.stdout


def _download_debian_rootfs(tarball_path: Path, *, tag: str) -> None:
    """Download the official Debian rootfs from Docker Hub OCI registry.

    Pulls the rootfs layer from the selected official Debian image without
    requiring Docker to be installed. The layer is a tar.gz that works
    directly with ``wsl --import``.
    """
    # 1. Get anonymous pull token
    output.detail("Authenticating with Docker Hub...")
    with urllib.request.urlopen(_DOCKER_AUTH_URL) as resp:
        token = json.loads(resp.read())["token"]

    # 2. Fetch image manifest to find the rootfs layer digest.
    #    The Debian tag is multi-arch, so we first get the manifest list
    #    and resolve the platform-specific manifest for the host architecture.
    output.detail(f"Fetching Debian {tag} image manifest...")
    auth_header = {"Authorization": f"Bearer {token}"}

    req = urllib.request.Request(
        f"{_DOCKER_MANIFESTS_URL}/{tag}",
        headers={
            **auth_header,
            "Accept": (
                "application/vnd.docker.distribution.manifest.list.v2+json, "
                "application/vnd.docker.distribution.manifest.v2+json"
            ),
        },
    )
    with urllib.request.urlopen(req) as resp:
        manifest = json.loads(resp.read())

    # If it's a manifest list, resolve the entry for the host architecture
    if "manifests" in manifest:
        arch = _oci_arch()
        match = next(
            (
                m
                for m in manifest["manifests"]
                if m.get("platform", {}).get("architecture") == arch and m.get("platform", {}).get("os") == "linux"
            ),
            None,
        )
        if match is None:
            raise RuntimeError(f"No {arch}/linux manifest found for debian:{tag}")
        platform_digest = match["digest"]
        manifest_url = f"https://registry-1.docker.io/v2/library/debian/manifests/{platform_digest}"
        req = urllib.request.Request(
            manifest_url,
            headers={
                **auth_header,
                "Accept": "application/vnd.docker.distribution.manifest.v2+json",
            },
        )
        with urllib.request.urlopen(req) as resp:
            manifest = json.loads(resp.read())

    digest = manifest["layers"][0]["digest"]
    total_bytes = manifest["layers"][0].get("size", 0)

    # 3. Download the rootfs layer with progress. Write to a temp name
    #    and rename into place on completion so an interrupted or failed
    #    download can never leave a truncated tarball at the cache path
    #    (a corrupt cache would poison every retried create with a
    #    baffling `wsl --import` error). os.replace is atomic within the
    #    directory (same filesystem by construction).
    blob_url = f"{_DOCKER_BLOBS_URL}/{digest}"
    req = urllib.request.Request(blob_url, headers=auth_header)
    p = output.progress("Downloading Debian rootfs", total=total_bytes or None)

    partial_path = tarball_path.with_name(tarball_path.name + ".partial")
    try:
        with _blob_opener.open(req) as resp, partial_path.open("wb") as f:
            downloaded = 0
            chunk_size = 256 * 1024
            last_update = 0
            while True:
                chunk = resp.read(chunk_size)
                if not chunk:
                    break
                f.write(chunk)
                downloaded += len(chunk)
                # Update every ~1MB to avoid flooding
                if downloaded - last_update >= 1024 * 1024:
                    p.update(downloaded)
                    last_update = downloaded
        p.done()
        os.replace(partial_path, tarball_path)
    except BaseException:
        # BaseException: a Ctrl-C must not leave the partial file behind
        # any more than a network failure may, whether it lands
        # mid-download or in the rename window (after a successful
        # rename the unlink is a missing_ok no-op). The unlink itself is
        # best-effort: a transient Windows lock (PermissionError) must
        # not replace the original error or interrupt.
        with contextlib.suppress(OSError):
            partial_path.unlink(missing_ok=True)
        raise


def _windows_status(code: int) -> str:
    """A Windows exit status as ``wsl.exe`` means it: Windows reports a 32-bit value, unsigned, so
    ``wsl.exe``'s -1 for a distro it cannot find arrives as 4294967295."""
    if code > 0x7FFFFFFF:
        return f"{code - 0x100000000} (0x{code:08X})"
    return str(code)


_KEEPER_READY = b"agentworks-wsl-keeper-ready\n"
_KEEPER_ENTRY_SECONDS = 300.0
_KEEPER_CLEANUP_SECONDS = 0.5
_KEEPER_FAILURE_SECONDS = 2.0
"""How long a keeper that did not attach gets to finish printing and exit, so a failing
``wsl.exe`` can be reported with what it printed and its status."""


def _await_keeper_attachment(proc: subprocess.Popen[bytes], distro_name: str) -> None:
    """Require this keeper's guest acknowledgement using bounded nonblocking reads."""
    assert proc.stdout is not None
    os.set_blocking(proc.stdout.fileno(), False)
    deadline = time.monotonic() + _KEEPER_ENTRY_SECONDS
    received = bytearray()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise StateError("WSL keeper guest attachment timed out", entity_kind="vm")
        try:
            chunk = os.read(proc.stdout.fileno(), len(_KEEPER_READY) + 1 - len(received))
        except BlockingIOError:
            chunk = None
        if chunk == b"":
            _refuse_unattached(proc, distro_name, received, "WSL keeper ended before guest attachment was acknowledged")
        if chunk:
            received.extend(chunk)
            if not _KEEPER_READY.startswith(received):
                _refuse_unattached(proc, distro_name, received, "WSL keeper guest acknowledgement is malformed")
            if received == _KEEPER_READY:
                if proc.poll() is not None:
                    raise StateError("WSL keeper exited during guest attachment", entity_kind="vm")
                return
        if proc.poll() is not None:
            _refuse_unattached(
                proc, distro_name, received, "WSL keeper exited before guest attachment was acknowledged"
            )
        time.sleep(min(0.01, remaining))


def _refuse_unattached(proc: subprocess.Popen[bytes], distro_name: str, received: bytearray, reason: str) -> NoReturn:
    """Raise for a keeper that did not acknowledge attachment.

    A keeper that exits non-zero is ``wsl.exe`` failing to attach (a distro
    unregistered outside Agentworks, a WSL service hiccup), and the error
    quotes what it printed: ``wsl.exe`` prints its own errors on stdout, which
    the keeper shares with stderr. Anything else is ``reason``.
    """
    assert proc.stdout is not None
    printed = bytearray(received)
    deadline = time.monotonic() + _KEEPER_FAILURE_SECONDS
    while time.monotonic() < deadline:
        try:
            chunk = os.read(proc.stdout.fileno(), 4096)
        except BlockingIOError:
            if proc.poll() is not None:
                break
            time.sleep(0.01)
            continue
        if not chunk:
            break
        printed.extend(chunk)
    try:
        status = proc.wait(timeout=max(0.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        status = None
    if status is None or status == 0:
        raise StateError(reason, entity_kind="vm")
    text = sanitize_terminal_output(printed.decode("utf-8", errors="replace").replace("\x00", "").strip())
    raise WSLCommandFailed(
        f"WSL2 distro '{distro_name}' could not be held running: "
        f"'wsl --distribution {distro_name}' exited with status {_windows_status(status)}: {text or 'no output'}",
        hint=(
            f"Check that 'wsl --list --verbose' lists '{distro_name}'. A distro unregistered outside "
            "Agentworks leaves a VM that only 'agw vm delete' can remove."
        ),
    )


def _release_keeper(proc: subprocess.Popen[bytes]) -> bool:
    """Bound terminate, kill, and reap; always close the owned acknowledgement pipe."""
    try:
        with contextlib.suppress(OSError):
            proc.terminate()
        try:
            proc.wait(timeout=_KEEPER_CLEANUP_SECONDS)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(OSError):
                proc.kill()
            try:
                proc.wait(timeout=_KEEPER_CLEANUP_SECONDS)
            except (OSError, subprocess.TimeoutExpired):
                return False
        except OSError:
            return False
        return True
    finally:
        if proc.stdout is not None:
            with contextlib.suppress(OSError):
                proc.stdout.close()


@contextlib.contextmanager
def _keepalive(distro_name: str) -> Iterator[None]:
    """Anchor the guest after this keeper acknowledges attachment, without connection repair.

    The same guest shell emits an exact marker before becoming the keeper sleep.
    Entry can boot a stopped distro. Client liveness alone does not prove guest
    attachment. Cleanup bounds local process termination without promising that
    every guest effect was cancelled. A Win32 kill-on-close job protects against
    an orphaned client when the parent dies without running cleanup.

    This is a pure power-hold. Verifying the VM answers is the shared paths'
    job (``_await_tailnet`` on ``vm start`` and gate auto-start), which run
    inside this hold, so the anchor covers the verification without
    duplicating it here.
    """
    # Both streams share one pipe: wsl.exe prints its own errors on stdout.
    proc = subprocess.Popen(
        [
            _WSL,
            "--distribution",
            distro_name,
            "--",
            "sh",
            "-c",
            "printf 'agentworks-wsl-keeper-ready\\n'; exec sleep infinity",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
    )
    h_job: int | None = None
    interruption: BaseException | None = None
    try:
        # Popen._handle is the Windows-only process HANDLE; absent from typeshed
        # (and from Popen on POSIX), hence getattr instead of attribute access.
        h_proc: int | None = getattr(proc, "_handle", None)
        h_job = _create_kill_on_close_job() if h_proc is not None else None
        if h_job is not None and h_proc is not None and not _assign_process_to_job(h_job, int(h_proc)):
            _close_handle(h_job)
            h_job = None
        if h_job is None and sys.platform == "win32":
            output.detail(
                "(note: Win32 Job Object unavailable; a hard-kill of this command may leave an orphan wsl.exe.)"
            )
        _await_keeper_attachment(proc, distro_name)
        output.detail(f"Preventing idle-shutdown of WSL2 distro {distro_name!r} for the duration of this command...")
        yield
    except BaseException as error:
        interruption = error
        raise
    finally:
        try:
            cleaned = _release_keeper(proc)
        finally:
            _close_handle(h_job)
        if not cleaned:
            if interruption is not None:
                interruption.add_note("Local WSL keeper cleanup did not complete within its bound.")
            else:
                raise StateError("local WSL keeper cleanup did not complete within its bound", entity_kind="vm")
        output.detail("Idle-shutdown prevention stopped.")


class Wsl2Config(AgwModel):
    """WSL2 takes no configuration: a WSL distribution is created from the
    host's own WSL install, so there is nothing per-site to point it at.
    The model carries only the tag that selects it, which is what makes an
    unknown key a hard error rather than a silently ignored one."""

    name: Literal["wsl2"]


class WSL2Platform(VMPlatform):
    """Runs VMs as WSL2 Debian distributions on Windows."""

    contract_version: ClassVar[int] = 2
    name: ClassVar[str] = "wsl2"
    description: ClassVar[str] = "WSL2 Debian distributions on Windows"
    config_model: ClassVar[type[Wsl2Config]] = Wsl2Config
    prose: ClassVar[TopicProse | None] = TopicProse(
        title="WSL2",
        overview="""
        WSL2 runs each VM as a Debian distribution on the Windows machine agentworks is
        running on. It takes no configuration beyond selecting it: the built-in `wsl2`
        site is all most hosts need.

        Creation runs the generated bootstrap as root through `wsl.exe`, joins Tailscale
        the same way, and returns only after the distribution reports its Tailscale
        address. A failure rolls the new distribution back before it propagates.

        It is supported only on Windows, and reports not-ready everywhere else.
        """,
    )

    # wsl.exe reaches the distro without its sshd.
    recovery_needs_guest_sshd: ClassVar[bool] = False

    @property
    def config(self) -> Wsl2Config:
        """This site's validated wsl2 config: the tag and nothing else."""
        return self._config_as(Wsl2Config)

    @classmethod
    def unsupported_reason(cls) -> str | None:
        """WSL2 is categorically Windows-only: no configuration of
        this platform can ever work elsewhere, so the whole platform
        disables off Windows (vs lima, whose remote mode keeps the
        platform supported everywhere)."""
        if sys.platform != "win32":
            return "Windows only"
        return None

    @classmethod
    def not_ready(cls, config: Mapping[str, object]) -> Readiness:
        """On Windows a wsl2 site additionally needs ``wsl.exe`` itself
        (WSL is an optional Windows feature). Off Windows the platform
        gate (``unsupported_reason``) already reports every wsl2 node
        not-ready, so this config-dependent check only adds signal on a
        supported host.

        Non-constructing (LLD c): a classmethod over ``config``, never an
        instance."""
        import shutil

        from agentworks.resources.graph import Readiness

        if not shutil.which(_WSL):
            return Readiness.blocked("wsl.exe not installed; run `wsl --install`")
        return Readiness.ready()

    def preflight(self, ctx: RunContext) -> None:
        """``wsl.exe`` must be on PATH (which also implies Windows).
        No config secrets, so the operation sweep's central prediction
        has nothing to check."""
        super().preflight(ctx)
        import shutil

        if not shutil.which(_WSL):
            from agentworks.errors import ConnectivityError

            raise ConnectivityError(
                "'wsl.exe' not found. The wsl2 platform runs VMs as WSL2 "
                "distributions and requires Windows with WSL installed.",
                hint="Install WSL (`wsl --install`) or use a different site.",
            )

    @classmethod
    def legacy_platform_metadata(cls, row: Mapping[str, Any], legacy: Mapping[str, Any]) -> dict[str, str]:
        # Pre-v27 rows recorded wsl_distro_name (always equal to the VM
        # name); read paths keyed off vm.name regardless. Either value
        # is the distro name for every existing row.
        distro = row["wsl_distro_name"] or row["name"]
        return {"distro_name": str(distro)}

    def _distro_name(self, vm: VMRow) -> str:
        distro = vm.platform_metadata.get("distro_name")
        if not distro:
            raise StateError(
                f"VM '{vm.name}' has no wsl2 distro_name in its platform metadata; the DB row is incomplete",
                entity_kind="vm",
                entity_name=vm.name,
            )
        return str(distro)

    def vm_active(self, vm: VMRow, *, config: Config | None = None) -> AbstractContextManager[None]:
        # config is part of the base-class contract (a pure power-hold on
        # every platform); wsl2's anchor needs only the distro name.
        return _keepalive(self._distro_name(vm))

    def validate_create_hostname(self, hostname: str) -> None:
        """Refuse a hostname WSL would rename (``_WSL_HOSTNAME_RE``), which the guest would then not carry.

        A VM name may hold underscores and a system slug consecutive hyphens;
        core composes the hostname from both, and no command changes the slug.
        """
        if _WSL_HOSTNAME_RE.fullmatch(hostname):
            return
        if "_" in hostname:
            hint = "Pick a VM name without underscores."
        else:
            hint = "This install's system slug has consecutive hyphens, so it cannot be part of a WSL2 hostname."
        raise ValidationError(
            f"WSL2 would rename the VM's hostname '{hostname}': it keeps only lowercase letters and digits "
            "joined by single hyphens",
            entity_kind="vm",
            hint=hint,
        )

    def create(self, request: ProvisionRequest, ctx: RunContext) -> ProvisionResult:
        image_tag = code_owned_release_value(
            _DEBIAN_OCI_TAGS,
            request.debian_release,
            platform_name=self.name,
        )
        # The platform owns the backend-side name; distro
        # names are the primary identifier, so a collision is an error.
        distro_name = f"{request.system_slug}-{request.vm_name}" if request.system_slug else request.vm_name
        if self._distro_exists(distro_name):
            raise StateError(
                f"a WSL2 distro named '{distro_name}' is already registered",
                entity_kind="vm",
                entity_name=request.vm_name,
                hint=("unregister it first (wsl --unregister) or pick a different VM name"),
            )
        vm_name = distro_name
        swap = request.swap_gib
        admin_username = request.admin_username
        output.info(f"Provisioning WSL2 VM '{vm_name}'...")

        install_path = _wsl_base_path() / vm_name

        # Rollback: spans every step from the first local mutation (the
        # install-directory New-Item) through the systemd restart at the
        # end. The caller's unwind deletes only the DB row on failure OR
        # interrupt, so a distro or install directory left behind here
        # would be orphaned with nothing to target it (#340; the azure
        # precedent is #338). Everything before this try mutates
        # nothing, so no arm ever fires a cleanup call for state that
        # was never made. The rootfs cache under _cache_dir() is
        # deliberately NOT rolled back: it is shared across creates
        # (delete() keeps it too) and is exactly what makes a retried
        # create fast; keeping it is safe because the download is
        # atomic (temp name + rename), so the cache path only ever
        # holds a complete tarball.
        try:
            try:
                _powershell(f"New-Item -ItemType Directory -Force -Path {_ps_quote(install_path)}")

                # Download Debian rootfs if not cached
                cache_dir = _cache_dir()
                tarball = cache_dir / f"debian-{image_tag}-{_oci_arch()}-rootfs.tar.gz"
                _powershell(f"New-Item -ItemType Directory -Force -Path {_ps_quote(cache_dir)}")

                if not tarball.exists():
                    _download_debian_rootfs(tarball, tag=image_tag)
                else:
                    output.detail("Using cached Debian rootfs.")

                # Import and configure the distro
                output.info("Importing rootfs into WSL2...")
                _wsl(["--import", vm_name, str(install_path), str(tarball)])

                # Strip Docker-image minimization hooks before we run any apt-get.
                # The official Debian Docker rootfs ships /usr/sbin/policy-rc.d
                # that returns 101 to refuse all service starts during image build;
                # without removing it, apt-installed daemons (e.g. tailscaled) never
                # start, leaving us with an "installed but inert" service.
                output.info("Removing Docker minimization hooks...")
                _wsl(
                    [
                        "--distribution",
                        vm_name,
                        "--user",
                        "root",
                        "--",
                        "rm",
                        "-f",
                        "/usr/sbin/policy-rc.d",
                    ]
                )

                # The Docker rootfs is minimal. Install packages to bring it up to
                # parity with the Lima/Azure cloud images.
                output.info("Installing base packages...")
                _wsl(
                    [
                        "--distribution",
                        vm_name,
                        "--user",
                        "root",
                        "--",
                        "bash",
                        "-c",
                        "DEBIAN_FRONTEND=noninteractive apt-get update -qq"
                        " && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq"
                        " -o Dpkg::Options::=--force-confnew"
                        " bash bash-completion sudo passwd"
                        " openssh-server curl git ca-certificates"
                        " tmux tmuxinator"
                        " locales procps iproute2 iputils-ping"
                        " less vim-tiny man-db"
                        " > /dev/null",
                    ]
                )

                # Configure swap file
                if swap > 0:
                    swap_mb = swap * 1024
                    output.info(f"Setting up {swap} GiB swap file...")
                    _wsl(
                        [
                            "--distribution",
                            vm_name,
                            "--user",
                            "root",
                            "--",
                            "bash",
                            "-c",
                            f"fallocate -l {swap_mb}M /swapfile"
                            " && chmod 600 /swapfile"
                            " && mkswap /swapfile"
                            " && swapon /swapfile"
                            " && echo '/swapfile none swap sw 0 0' >> /etc/fstab",
                        ]
                    )

                # Create user account
                output.info(f"Creating user '{admin_username}'...")
                _wsl(
                    [
                        "--distribution",
                        vm_name,
                        "--user",
                        "root",
                        "--",
                        "useradd",
                        "-m",
                        "-s",
                        "/bin/bash",
                        admin_username,
                    ]
                )
                _wsl(["--distribution", vm_name, "--user", "root", "--", "usermod", "-aG", "sudo", admin_username])
                import shlex

                _wsl(
                    [
                        "--distribution",
                        vm_name,
                        "--user",
                        "root",
                        "--",
                        "bash",
                        "-c",
                        f"echo {shlex.quote(f'{admin_username} ALL=(ALL) NOPASSWD:ALL')}"
                        f" > /etc/sudoers.d/{shlex.quote(admin_username)}",
                    ]
                )

                # Configure wsl.conf: default user, systemd and the hostname
                output.info("Enabling systemd...")
                _wsl(
                    [
                        "--distribution",
                        vm_name,
                        "--user",
                        "root",
                        "--",
                        "bash",
                        "-c",
                        f"printf '%s' {shlex.quote(_wsl_conf(admin_username, request.hostname))} > /etc/wsl.conf",
                    ]
                )

                # Restart the distro so systemd takes effect
                output.info("Restarting distro...")
                _wsl(["--terminate", vm_name])
                # Run a command to trigger the distro to start with systemd
                _wsl(["--distribution", vm_name, "--user", "root", "--", "bash", "-c", "echo ok"])

                # WSL2's generated script is its primary create-time bootstrap,
                # so it stays inside the same distro rollback span, as do the
                # join and the address read over the same root channel. The
                # manager owns the progress sink and records the address later.
                # The bootstrap runs synchronously: a detached run would be in
                # its own systemd-logind session, where KillUserProcesses can
                # reap it when the wsl.exe that started it exits.
                output.info("Bootstrapping VM...")
                run_as_root = functools.partial(_run_as_root, distro_name)
                run_bootstrap(
                    run_as_root,
                    admin_username=admin_username,
                    ssh_public_key=request.ssh_public_key,
                    hostname=request.hostname,
                    swap=0,
                    deadline=_BOOTSTRAP_DEADLINE_SECONDS,
                    progress=request.progress,
                )
                join_tailscale(run_as_root, request.tailscale_auth_key, node=request.hostname)
                tailnet_address = settle_tailnet_address(
                    run_as_root, node=request.hostname, rerun_hint=CREATE_RERUN_HINT
                )
            except Exception as failure:
                # WSL2's error convention holds: the errors from _wsl and
                # _powershell propagate unwrapped; the only new obligation
                # is the teardown.
                output.detail(f"Cleaning up the partial WSL2 distro '{vm_name}'...")
                self._cleanup_partial_create(vm_name, vm_name=request.vm_name, failure=failure)
                raise
        except KeyboardInterrupt:
            self._rollback_create_on_interrupt(vm_name, vm_name=request.vm_name)
            raise

        output.detail(f"WSL2 VM '{vm_name}' provisioned.")
        return ProvisionResult(
            platform_metadata={"distro_name": distro_name},
            tailnet_address=tailnet_address,
        )

    def _cleanup_partial_create(self, distro_name: str, *, vm_name: str, failure: Exception | None) -> None:
        """Tear down the distro / install directory a failed or interrupted
        ``create`` made (only ever state this create named: the pre-flight
        collision check guarantees the name was free when we started).

        A teardown that fails, or that cannot show the distro gone (see
        :meth:`_teardown_distro`), warns with the manual removal command and
        hands the distro to core as :class:`RetainedProvisioningError`,
        whose message carries the original ``failure`` (``None`` for an
        interrupt), so the row survives for ``vm delete`` to retry. A
        ``KeyboardInterrupt`` during it is not contained: called from the
        failure arm, it escapes to ``create``'s outer interrupt arm, which
        restarts the cleanup under :meth:`_rollback_create_on_interrupt`;
        called from there, it is the second Ctrl-C that abandons the
        cleanup.
        """
        try:
            remains = not self._teardown_distro(distro_name)
            detail = "it could not be shown unregistered"
            cause: Exception | None = None
        except Exception as e:
            remains, detail, cause = True, str(e), e
        if not remains:
            return
        output.warn(f"could not clean up the partial WSL2 distro '{distro_name}': {detail}")
        output.warn(self._manual_removal_hint(distro_name))
        what = "create was interrupted" if failure is None else f"create failed ({failure})"
        raise self._retained(vm_name, distro_name, f"{what}, and removing the distro failed: {detail}") from cause

    def _rollback_create_on_interrupt(self, distro_name: str, *, vm_name: str) -> None:
        """Roll back the partially created distro after an operator
        interrupt inside :meth:`create` (the azure precedent:
        ``rollback_create_on_interrupt``, #338).

        A SECOND interrupt during the cleanup abandons it cleanly
        instead of wedging, warning with the exact removal command, and
        raises :class:`RetainedProvisioningError` in place of the
        interrupt, as a failed cleanup does, so the row survives for
        ``vm delete``. A cleanup that completes returns, and the caller
        re-raises the ORIGINAL interrupt, which then reaches
        ``create_vm``, whose unwind deletes the DB row it no longer
        needs."""
        output.warn(
            f"Interrupted: cleaning up the partial WSL2 distro '{distro_name}', "
            "please wait (Ctrl-C again retains it for explicit deletion)..."
        )
        try:
            self._cleanup_partial_create(distro_name, vm_name=vm_name, failure=None)
        except KeyboardInterrupt as second:
            output.warn(
                f"Cleanup abandoned: the WSL2 distro '{distro_name}' may remain; "
                + self._manual_removal_hint(distro_name)
            )
            raise self._retained(
                vm_name, distro_name, "create was interrupted, and so was removing the distro"
            ) from second

    def _retained(self, vm_name: str, distro_name: str, detail: str) -> RetainedProvisioningError:
        """The retained-row handoff: the row keeps the distro name, so ``vm delete`` can retry its removal."""
        return RetainedProvisioningError(
            f"the WSL2 distro '{distro_name}' may remain: {detail}",
            platform_metadata={"distro_name": distro_name},
            entity_name=vm_name,
            hint=f"Once the cause is fixed, run 'agw vm delete {vm_name}', or "
            + self._manual_removal_hint(distro_name),
        )

    @staticmethod
    def _manual_removal_hint(distro_name: str) -> str:
        return (
            f"remove it manually with 'wsl --unregister {distro_name}' "
            f"and by deleting '{_wsl_base_path() / distro_name}'."
        )

    @staticmethod
    def _distro_exists(distro_name: str) -> bool:
        """Pre-flight: is a distro with this name already registered?"""
        try:
            listing = _wsl(["--list", "--quiet"], check=False)
        except (RuntimeError, OSError):
            return False
        return any(line.strip() == distro_name for line in listing.splitlines())

    def start(self, vm: VMRow, ctx: RunContext) -> None:
        # Idempotent by construction (the ABC flags start): running a
        # command boots a stopped distro and is a plain exec on a
        # running one; no guard needed.
        output.info(f"Starting WSL2 distro '{vm.name}'...")
        _wsl(["--distribution", self._distro_name(vm), "--", "echo", "started"])
        output.info(f"WSL2 distro '{vm.name}' started")

    def stop(self, vm: VMRow, ctx: RunContext) -> None:
        # Idempotency guard (the ABC flags stop): `wsl --terminate` on
        # a stopped distro is not reliably a no-op across WSL versions.
        if self.status(vm, ctx) == VMStatus.STOPPED:
            output.detail(f"WSL2 distro '{vm.name}' is already stopped")
            return
        output.info(f"Terminating WSL2 distro '{vm.name}'...")
        _wsl(["--terminate", self._distro_name(vm)])
        output.info(f"WSL2 distro '{vm.name}' terminated")

    @staticmethod
    def _teardown_distro(distro_name: str) -> bool:
        """Unregister the distro and remove its install directory; return whether the distro is shown gone.

        The one place the teardown sequence lives: shared by the delete op
        and the create rollback. Unregistering discards the distro's
        virtual disk. Neither step's status stops the sequence, since the
        rollback's partial states (an install directory without a
        registered distro, or a registered distro whose in-guest setup
        never finished) make either fail harmlessly. Then ``wsl --list``
        decides, and nothing reads a failure as absence: the distro is
        gone only when the listing does not name it and either the
        unregister or the listing exited 0 (an empty one included), or the
        listing reports :data:`_NO_DISTRIBUTIONS`. A distro shown gone whose
        install directory survived (a process outside WSL holds its disk
        image) warns naming the directory.
        """
        unregistered = _run_wsl(["--unregister", distro_name]).returncode == 0
        install_path = _wsl_base_path() / distro_name
        _powershell(
            f"Remove-Item -Recurse -Force -Path {_ps_quote(install_path)} -ErrorAction SilentlyContinue",
            check=False,
        )
        listing = _run_wsl(["--list", "--quiet"])
        if any(line.strip() == distro_name for line in listing.stdout.splitlines()):
            return False
        empty = _NO_DISTRIBUTIONS in listing.stdout or _NO_DISTRIBUTIONS in listing.stderr
        gone = unregistered or listing.returncode == 0 or empty
        if gone and install_path.exists():
            output.warn(
                f"WSL2 distro '{distro_name}' is unregistered, but its directory '{install_path}' could not be "
                "removed, so its disk image may remain there: run 'wsl --shutdown', then delete the directory."
            )
        return gone

    def delete(self, vm: VMRow, ctx: RunContext) -> None:
        output.info(f"Unregistering WSL2 distro '{vm.name}'...")
        distro_name = self._distro_name(vm)
        # Core drops the row only when this returns, so a distro the
        # teardown cannot show gone fails the delete.
        if not self._teardown_distro(distro_name):
            raise StateError(
                f"WSL2 distro '{distro_name}' could not be shown unregistered after 'wsl --unregister'",
                entity_kind="vm",
                entity_name=vm.name,
                hint=f"Run 'agw vm delete {vm.name}' again, or {self._manual_removal_hint(distro_name)}",
            )
        output.info(f"WSL2 distro '{vm.name}' deleted")

    def display_backend_name(self, vm: VMRow) -> str:
        return str(vm.platform_metadata.get("distro_name", vm.name))

    def run_as_root(self, vm: VMRow, ctx: RunContext, script: bytes, deadline: float) -> RawRun:
        """Run ``script`` as root through ``wsl.exe`` (:func:`_run_as_root`).

        ``ctx`` is unused: wsl.exe is local and needs no backend credential.
        """
        return _run_as_root(self._distro_name(vm), script, deadline)

    def native_shell(self, vm: VMRow, ctx: RunContext) -> int:
        """An interactive login shell as the admin: ``wsl.exe --distribution <distro> --user <admin>``."""
        return ssh_process.run_terminal([_WSL, "--distribution", self._distro_name(vm), "--user", vm.admin_username])

    def status(self, vm: VMRow, ctx: RunContext) -> VMStatus:
        distro_name = self._distro_name(vm)
        try:
            listing = _wsl(["--list", "--verbose"], check=False, timeout=_STATUS_TIMEOUT_SECONDS)
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            return VMStatus.UNKNOWN

        for line in listing.strip().splitlines():
            parts = line.split()
            # WSL --list --verbose output: [*] NAME STATE VERSION
            # Filter to find our distro
            name_candidates = [p for p in parts if p == distro_name]
            if not name_candidates:
                continue
            state_str = parts[-2].lower() if len(parts) >= 3 else ""
            if state_str == "running":
                return VMStatus.RUNNING
            if state_str == "stopped":
                return VMStatus.STOPPED
            return VMStatus.UNKNOWN
        return VMStatus.UNKNOWN
