"""The WSL2 VM platform: imports Debian distros on Windows."""

from __future__ import annotations

import contextlib
import functools
import json
import os
import platform
import re
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal, cast

from agentworks import output
from agentworks.capabilities.vm_platform.base import (
    CheckpointDescriptor,
    ProvisionRequest,
    ProvisionResult,
    VMPlatform,
)
from agentworks.capabilities.vm_platform.debian_release import (
    code_owned_release_value,
    verify_provisioned_release,
)
from agentworks.capabilities.vm_platform.wsl2_bootstrap import run_wsl2_bootstrap
from agentworks.db import VMStatus
from agentworks.debian import DebianRelease
from agentworks.errors import AgentworksError, ExternalError, StateError
from agentworks.schema import AgwModel
from agentworks.topics import TopicProse
from agentworks.transports import WSL2Transport

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping
    from contextlib import AbstractContextManager

    from agentworks.capabilities.base import RunContext
    from agentworks.config import Config
    from agentworks.db import VMRow
    from agentworks.resources.graph import Readiness
    from agentworks.transports import Transport


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


def _checkpoint_errors[**CheckpointParams, CheckpointResult](
    method: Callable[CheckpointParams, CheckpointResult],
) -> Callable[CheckpointParams, CheckpointResult]:
    """Normalize expected WSL process and filesystem checkpoint failures."""

    @functools.wraps(method)
    def wrapped(*args: CheckpointParams.args, **kwargs: CheckpointParams.kwargs) -> CheckpointResult:
        try:
            return method(*args, **kwargs)
        except AgentworksError:
            raise
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            vm = cast("VMRow", args[1])
            operation = method.__name__.replace("_", " ")
            raise ExternalError(
                f"WSL2 {operation} failed for VM '{vm.name}': {error}",
                entity_kind="vm",
                entity_name=vm.name,
                hint="Correct the Windows filesystem or WSL command failure, then retry.",
            ) from error

    return wrapped


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


def _checkpoint_root() -> Path:
    """Private host-side storage for Agentworks-managed WSL exports."""
    return _local_app_data() / "agentworks" / "checkpoints" / "wsl2"


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
_MANAGED_CHECKPOINT_NAME = re.compile(r"agw-[a-z0-9](?:[a-z0-9-]*[a-z0-9])?")
_SAFE_DISTRO_NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?")
_CHECKPOINT_TIMEOUT_SECONDS = 3600

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


def _wsl(args: list[str], *, check: bool = True, timeout: int = 300) -> str:
    """Run a wsl.exe command and return stdout.

    wsl.exe emits UTF-16LE on redirected output unless WSL_UTF8=1 is
    set; decoding that as UTF-8 leaves a NUL after every ASCII char,
    which silently breaks the name-equality parsers (``status``,
    ``_distro_exists``). Strip the NULs so both encodings parse.
    """
    result = subprocess.run(
        ["wsl", *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout
    )
    if check and result.returncode != 0:
        stderr = result.stderr.replace("\x00", "").strip()
        raise RuntimeError(f"wsl command failed: {stderr}")
    return result.stdout.replace("\x00", "")


def _powershell(script: str, *, check: bool = True, timeout: int = 120) -> str:
    """Run a PowerShell command and return stdout."""
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
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


@contextlib.contextmanager
def _keepalive(distro_name: str) -> Iterator[None]:
    """Anchor a WSL2 distro for the duration of the context.

    Spawns ``wsl --distribution NAME -- sleep infinity`` as a background
    subprocess. While that wsl.exe is attached, Windows' WSL idle timer
    (``vmIdleTimeout`` in .wslconfig, default ~60s) doesn't fire, so the
    distro stays up regardless of whether anything else is talking to it.
    If the distro happens to be stopped on entry, the same subprocess
    boots it.

    This is a pure power-hold: it anchors and (if needed) boots the
    distro, nothing more. Verifying Tailscale connectivity is NOT this
    function's job; the shared paths handle that uniformly across all
    platforms (``_ensure_tailscale`` on ``vm start`` and gate
    auto-start, the gate itself on the generic held-active span), and
    every one of them runs inside this hold, so the anchor covers the
    verification without duplicating it here.

    The hold does no connectivity retry, deliberately. One gate path has
    no reachability wait around it: when ``ensure_active`` finds the VM
    not confirmed-active (tailscale ping failed) but not observed-stopped
    either (``status()`` reports RUNNING anyway, e.g. tailscaled
    mid-reattach), it skips ``auto_start`` and enters this hold and the
    op directly. The earlier WSL2 wait was a safety net there; without
    it, WSL2 surfaces a plain SSHError on that path like every other
    platform. That parity is the point of the uniformity change, not a
    regression: do not re-add a wait here to paper over it.

    On exit: ``terminate()`` the subprocess (TerminateProcess on Windows;
    SIGTERM on POSIX, though this code path is Windows-only in practice),
    wait briefly, then ``kill()`` if it hasn't exited. The distro is then
    free to idle out on Windows' normal schedule.

    The subprocess is also assigned to a Win32 Job Object with
    ``KILL_ON_JOB_CLOSE``, so if this Python process dies in a way that
    skips the ``finally:`` (SIGKILL, console window closed, hard crash),
    the kernel closes the job handle and kills the orphan for us.
    """
    proc = subprocess.Popen(
        ["wsl", "--distribution", distro_name, "--", "sleep", "infinity"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    # Bind the subprocess to a Job Object so a hard-kill of this Python
    # process still tears down the wsl.exe orphan (the OS closes the job
    # handle, which kills every process in the job). Best-effort: if the
    # Win32 calls fail (older Windows / unusual perms / non-Windows), we
    # fall back to terminate-only cleanup and warn so the operator knows
    # the orphan-on-hard-kill risk is live.
    # Popen._handle is the Windows-only process HANDLE; absent from typeshed
    # (and from Popen on POSIX), hence getattr instead of attribute access.
    h_proc: int | None = getattr(proc, "_handle", None)
    h_job: int | None = _create_kill_on_close_job() if h_proc is not None else None
    if h_job is not None and h_proc is not None and not _assign_process_to_job(h_job, int(h_proc)):
        _close_handle(h_job)
        h_job = None
    # Only surface the orphan-risk note on Windows where Job Object SHOULD
    # work but didn't (older Windows / unusual perms / ctypes import failed).
    # On other platforms _kernel32 is always None by design, so the note
    # would just be noise on every keepalive entry.
    if h_job is None and sys.platform == "win32":
        output.detail("(note: Win32 Job Object unavailable; a hard-kill of this command may leave an orphan wsl.exe.)")

    def _close_stderr() -> None:
        # Popen with stderr=PIPE leaves a read-end fd open until the Popen
        # object is GC'd. On the fast-fail path we already read and won't
        # touch it again; on the normal-exit path the subprocess has been
        # waited on so the pipe is at EOF. Either way the fd is dead weight.
        if proc.stderr is not None:
            with contextlib.suppress(OSError):
                proc.stderr.close()

    # Fast-fail check: if wsl.exe couldn't attach (wrong distro name, WSL
    # service hiccup, etc.), `sleep infinity` exits within milliseconds.
    # Without this check the keepalive silently becomes a no-op and the
    # caller hits confusing idle-shutdown timeouts mid-operation.
    try:
        rc = proc.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        rc = None  # still running, which is what we want
    if rc is not None:
        stderr = proc.stderr.read().decode("utf-8", errors="replace").strip() if proc.stderr else ""
        _close_stderr()
        _close_handle(h_job)
        raise RuntimeError(
            f"WSL2 keepalive for distro {distro_name!r} exited immediately (rc={rc})"
            + (f": {stderr}" if stderr else "")
        )
    output.detail(f"Preventing idle-shutdown of WSL2 distro {distro_name!r} for the duration of this command...")
    try:
        yield
    finally:
        # Cleanup is best-effort. If the wsl.exe subprocess has already
        # exited (WSL service reset, distro `wsl --terminate`'d by hand,
        # WSL2 vmIdleTimeout finally fired during a hang), terminate() /
        # kill() raise OSError / ProcessLookupError on POSIX. Suppress so
        # we don't either mask the caller's exception or turn a successful
        # command into a failure on the way out. wait() on an already-
        # reaped Popen just returns the cached returncode, so it doesn't
        # need the same guard.
        with contextlib.suppress(OSError):
            proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(OSError):
                proc.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=5)
        _close_stderr()
        _close_handle(h_job)
        output.detail("Idle-shutdown prevention stopped.")


class Wsl2Config(AgwModel):
    """WSL2 takes no configuration: a WSL distribution is created from the
    host's own WSL install, so there is nothing per-site to point it at.
    The model carries only the tag that selects it, which is what makes an
    unknown key a hard error rather than a silently ignored one."""

    name: Literal["wsl2"]


class WSL2Platform(VMPlatform):
    """Runs VMs as WSL2 Debian distributions on Windows."""

    contract_version: ClassVar[int] = 1
    name: ClassVar[str] = "wsl2"
    description: ClassVar[str] = "WSL2 Debian distributions on Windows"
    config_model: ClassVar[type[Wsl2Config]] = Wsl2Config
    prose: ClassVar[TopicProse | None] = TopicProse(
        title="WSL2",
        overview="""
        WSL2 runs each VM as a Debian distribution on the Windows machine agentworks is
        running on. It takes no configuration beyond selecting it: the built-in `wsl2`
        site is all most hosts need.

        Creation runs the generated bootstrap through private local and distro staging,
        removes both staging files, and returns only after Tailscale reports an IP. A
        bootstrap failure rolls the new distribution back before it propagates.

        It is supported only on Windows, and reports not-ready everywhere else.
        """,
    )

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

        if not shutil.which("wsl"):
            return Readiness.blocked("wsl.exe not installed; run `wsl --install`")
        return Readiness.ready()

    def preflight(self, ctx: RunContext) -> None:
        """``wsl.exe`` must be on PATH (which also implies Windows).
        No config secrets, so the operation sweep's central prediction
        has nothing to check."""
        super().preflight(ctx)
        import shutil

        if not shutil.which("wsl"):
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

                # Configure wsl.conf: default user + systemd
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
                        f"printf '[user]\\ndefault={shlex.quote(admin_username)}"
                        f"\\n\\n[boot]\\nsystemd=true\\n' > /etc/wsl.conf",
                    ]
                )

                # Restart the distro so systemd takes effect
                output.info("Restarting distro...")
                _wsl(["--terminate", vm_name])
                # Run a command to trigger the distro to start with systemd
                _wsl(["--distribution", vm_name, "--user", "root", "--", "bash", "-c", "echo ok"])

                # WSL2's generated script is its primary create-time bootstrap,
                # so it stays inside the same distro rollback span. The manager
                # owns the progress sink and persists the returned IP later.
                native_transport = WSL2Transport(distro_name=distro_name, user=admin_username)
                tailscale_ip = run_wsl2_bootstrap(
                    native_transport,
                    admin_username=admin_username,
                    ssh_public_key=request.ssh_public_key,
                    tailscale_auth_key=request.tailscale_auth_key,
                    hostname=request.hostname,
                    swap_gib=0,
                    progress=request.progress,
                )
                output.detail(f"Tailscale IP: {tailscale_ip}")
                verify_provisioned_release(native_transport, request.debian_release)
            except Exception:
                # WSL2's error convention holds: the RuntimeErrors from
                # _wsl / _powershell propagate unwrapped; the only new
                # obligation is the teardown.
                output.detail(f"Cleaning up the partial WSL2 distro '{vm_name}'...")
                self._cleanup_partial_create(vm_name)
                raise
        except KeyboardInterrupt:
            self._rollback_create_on_interrupt(vm_name)
            raise

        output.detail(f"WSL2 VM '{vm_name}' provisioned.")
        return ProvisionResult(
            native_transport=native_transport,
            platform_metadata={"distro_name": distro_name},
            tailscale_ip=tailscale_ip,
        )

    def _cleanup_partial_create(self, distro_name: str) -> None:
        """Best-effort teardown of the distro / install directory a
        failed ``create`` made (only ever state this create named: the
        pre-flight collision check guarantees the name was free when we
        started).

        Never raises a cleanup failure over the original error; it
        warns with the manual removal command instead. An operator's
        second Ctrl-C (``KeyboardInterrupt``) deliberately escapes so
        :meth:`_rollback_create_on_interrupt` can abandon the cleanup.
        """
        try:
            self._teardown_distro(distro_name)
        except Exception as e:
            output.warn(f"could not clean up the partial WSL2 distro '{distro_name}': {e}")
            output.warn(self._manual_removal_hint(distro_name))

    def _rollback_create_on_interrupt(self, distro_name: str) -> None:
        """Roll back the partially created distro after an operator
        interrupt inside :meth:`create` (the azure precedent:
        ``rollback_create_on_interrupt``, #338).

        A SECOND interrupt during the cleanup abandons it cleanly
        instead of wedging, warning with the exact removal command; it
        is absorbed so the caller re-raises the ORIGINAL interrupt,
        which then reaches ``create_vm``, whose unwind deletes the DB
        row it no longer needs."""
        output.warn(
            f"Interrupted: cleaning up the partial WSL2 distro '{distro_name}', "
            "please wait (Ctrl-C again to abandon it)..."
        )
        try:
            self._cleanup_partial_create(distro_name)
        except KeyboardInterrupt:
            output.warn(
                f"Cleanup abandoned: the WSL2 distro '{distro_name}' may remain; "
                + self._manual_removal_hint(distro_name)
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

    @staticmethod
    def _checkpoint_distro_exists(distro_name: str) -> bool:
        """Strictly prove distro presence across a destructive checkpoint boundary."""

        listing = _wsl(["--list", "--quiet"])
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
    def _teardown_distro(distro_name: str) -> None:
        """The one place the teardown sequence lives: shared by the
        delete op and the create rollback. Unregister the distro (which
        discards its virtual disk), then remove the install directory.
        Both steps are no-ops on absent state (``check=False`` /
        ``SilentlyContinue``), which is what the rollback's partial
        states need: an install directory without a registered distro,
        or a registered distro whose in-guest setup never finished."""
        _wsl(["--unregister", distro_name], check=False)
        install_path = _wsl_base_path() / distro_name
        _powershell(
            f"Remove-Item -Recurse -Force -Path {_ps_quote(install_path)} -ErrorAction SilentlyContinue",
            check=False,
        )

    def delete(self, vm: VMRow, ctx: RunContext) -> None:
        output.info(f"Unregistering WSL2 distro '{vm.name}'...")
        self._teardown_distro(self._distro_name(vm))
        output.info(f"WSL2 distro '{vm.name}' deleted")

    @staticmethod
    def _require_checkpoint_name(name: str, *, vm_name: str) -> str:
        """Validate a checkpoint name crossing a durable/filesystem boundary."""
        if _MANAGED_CHECKPOINT_NAME.fullmatch(name) is None:
            raise StateError(
                f"WSL2 checkpoint name {name!r} is not an Agentworks-managed name",
                entity_kind="vm",
                entity_name=vm_name,
            )
        return name

    def _checkpoint_dir(self, vm: VMRow, *, create: bool) -> Path | None:
        distro_name = self._distro_name(vm)
        if _SAFE_DISTRO_NAME.fullmatch(distro_name) is None:
            raise StateError(
                f"WSL2 distro name {distro_name!r} cannot safely identify checkpoint storage",
                entity_kind="vm",
                entity_name=vm.name,
            )
        base = _local_app_data() / "agentworks"
        path = base
        for child in ("checkpoints", "wsl2", distro_name):
            if path.is_symlink() or (path.exists() and not path.is_dir()):
                raise StateError(
                    f"WSL2 checkpoint storage for VM '{vm.name}' is not a private directory",
                    entity_kind="vm",
                    entity_name=vm.name,
                )
            if not path.exists():
                if not create:
                    return None
                path.mkdir(mode=0o700)
            path = path / child
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise StateError(
                f"WSL2 checkpoint storage for VM '{vm.name}' is not a private directory",
                entity_kind="vm",
                entity_name=vm.name,
            )
        if not path.exists():
            if not create:
                return None
            path.mkdir(mode=0o700)
        return path

    def _checkpoint_paths(self, vm: VMRow, name: str, *, create_dir: bool) -> tuple[Path, Path]:
        name = self._require_checkpoint_name(name, vm_name=vm.name)
        directory = self._checkpoint_dir(vm, create=create_dir)
        if directory is None:
            directory = _checkpoint_root() / self._distro_name(vm)
        return directory / f"{name}.tar", directory / f"{name}.pre-restore.tar"

    def _checkpoint_identifier(self, vm: VMRow, name: str) -> str:
        return f"wsl2:{self._distro_name(vm)}:{name}"

    @staticmethod
    def _complete_export(path: Path) -> bool:
        return not path.is_symlink() and path.is_file() and path.stat().st_size > 0

    def _export_distro(self, vm: VMRow, destination: Path) -> None:
        """Export atomically without replacing an already-proved artifact."""
        if destination.exists() or destination.is_symlink():
            if self._complete_export(destination):
                return
            raise StateError(
                f"WSL2 checkpoint artifact '{destination.name}' is incomplete",
                entity_kind="vm",
                entity_name=vm.name,
            )
        partial = destination.with_name(destination.name + ".partial")
        if partial.is_symlink():
            raise StateError(
                f"WSL2 checkpoint staging path '{partial.name}' is not safe",
                entity_kind="vm",
                entity_name=vm.name,
            )
        if partial.exists():
            partial.unlink()
        _wsl(
            ["--export", self._distro_name(vm), str(partial)],
            timeout=_CHECKPOINT_TIMEOUT_SECONDS,
        )
        if not self._complete_export(partial):
            raise StateError(
                f"WSL2 did not produce a complete export for VM '{vm.name}'",
                entity_kind="vm",
                entity_name=vm.name,
            )
        partial.chmod(0o600)
        try:
            os.link(partial, destination)
        except FileExistsError as e:
            raise StateError(
                f"WSL2 checkpoint artifact '{destination.name}' appeared during export",
                entity_kind="vm",
                entity_name=vm.name,
            ) from e
        partial.unlink()

    def _require_checkpoint_vm_stopped(self, vm: VMRow, ctx: RunContext) -> None:
        if self.status(vm, ctx) is VMStatus.STOPPED:
            return
        raise StateError(
            f"WSL2 distro '{vm.name}' must be stopped for this checkpoint operation",
            entity_kind="vm",
            entity_name=vm.name,
        )

    @_checkpoint_errors
    def create_checkpoint(
        self,
        vm: VMRow,
        name: str,
        ctx: RunContext,
        *,
        operation_id: str,
        resume: bool,
    ) -> CheckpointDescriptor:
        del operation_id, resume
        name = self._require_checkpoint_name(name, vm_name=vm.name)
        self._require_checkpoint_vm_stopped(vm, ctx)
        descriptor = CheckpointDescriptor(name=name, identifier=self._checkpoint_identifier(vm, name))
        checkpoint, _ = self._checkpoint_paths(vm, name, create_dir=True)
        if self._complete_export(checkpoint):
            return descriptor
        self._export_distro(vm, checkpoint)
        if not self._complete_export(checkpoint):
            raise StateError(
                f"WSL2 did not report completed checkpoint '{name}'",
                entity_kind="vm",
                entity_name=vm.name,
            )
        return descriptor

    @_checkpoint_errors
    def list_checkpoints(self, vm: VMRow, ctx: RunContext) -> tuple[CheckpointDescriptor, ...]:
        del ctx
        directory = self._checkpoint_dir(vm, create=False)
        if directory is None:
            return ()
        names: set[str] = set()
        for path in directory.iterdir():
            filename = path.name
            if filename.endswith(".tar.partial"):
                name = filename.removesuffix(".tar.partial")
            elif filename.endswith(".tar") and not filename.endswith(".pre-restore.tar"):
                name = filename.removesuffix(".tar")
            else:
                continue
            if _MANAGED_CHECKPOINT_NAME.fullmatch(name) is None:
                continue
            if path.is_symlink() or not path.is_file():
                raise StateError(
                    f"WSL2 checkpoint path '{filename}' is not a regular managed artifact",
                    entity_kind="vm",
                    entity_name=vm.name,
                )
            names.add(name)
        return tuple(
            CheckpointDescriptor(name=name, identifier=self._checkpoint_identifier(vm, name)) for name in sorted(names)
        )

    @_checkpoint_errors
    def restore_checkpoint(
        self,
        vm: VMRow,
        checkpoint: CheckpointDescriptor,
        ctx: RunContext,
        *,
        operation_id: str,
    ) -> None:
        del operation_id
        name = self._require_checkpoint_name(checkpoint.name, vm_name=vm.name)
        expected = CheckpointDescriptor(name=name, identifier=self._checkpoint_identifier(vm, name))
        if checkpoint != expected or expected not in self.list_checkpoints(vm, ctx):
            raise StateError(
                f"WSL2 checkpoint '{name}' is missing or is not owned by this VM",
                entity_kind="vm",
                entity_name=vm.name,
            )
        artifact, emergency = self._checkpoint_paths(vm, name, create_dir=False)
        if not self._complete_export(artifact):
            raise StateError(
                f"WSL2 checkpoint '{name}' has no complete export to restore",
                entity_kind="vm",
                entity_name=vm.name,
                hint="Delete the incomplete checkpoint and create it again before restoring.",
            )
        distro_name = self._distro_name(vm)
        distro_exists = self._checkpoint_distro_exists(distro_name)
        if distro_exists:
            self._require_checkpoint_vm_stopped(vm, ctx)
            self._export_distro(vm, emergency)
        elif not self._complete_export(emergency):
            raise StateError(
                f"WSL2 distro '{vm.name}' is absent without a recoverable restore intermediate",
                entity_kind="vm",
                entity_name=vm.name,
            )

        install_path = _wsl_base_path() / distro_name
        if distro_exists:
            _wsl(["--unregister", distro_name])
            if self._checkpoint_distro_exists(distro_name):
                raise StateError(
                    f"WSL2 could not unregister distro '{vm.name}' before checkpoint restore",
                    entity_kind="vm",
                    entity_name=vm.name,
                )
        _powershell(
            f"Remove-Item -Recurse -Force -Path {_ps_quote(install_path)} -ErrorAction SilentlyContinue",
            check=False,
        )
        _powershell(f"New-Item -ItemType Directory -Force -Path {_ps_quote(install_path)}")
        _wsl(
            ["--import", distro_name, str(install_path), str(artifact), "--version", "2"],
            timeout=_CHECKPOINT_TIMEOUT_SECONDS,
        )
        _wsl(["--terminate", distro_name], check=False)
        if not self._checkpoint_distro_exists(distro_name) or self.status(vm, ctx) is not VMStatus.STOPPED:
            raise StateError(
                f"WSL2 checkpoint restore did not leave VM '{vm.name}' registered and stopped",
                entity_kind="vm",
                entity_name=vm.name,
            )

    @_checkpoint_errors
    def delete_checkpoint(
        self,
        vm: VMRow,
        checkpoint: CheckpointDescriptor,
        ctx: RunContext,
    ) -> None:
        del ctx
        name = self._require_checkpoint_name(checkpoint.name, vm_name=vm.name)
        expected_identifier = self._checkpoint_identifier(vm, name)
        if checkpoint.identifier != expected_identifier:
            raise StateError(
                f"WSL2 checkpoint '{name}' has a conflicting provider identifier",
                entity_kind="vm",
                entity_name=vm.name,
            )
        artifact, emergency = self._checkpoint_paths(vm, name, create_dir=False)
        for path in (
            artifact,
            emergency,
            artifact.with_name(artifact.name + ".partial"),
            emergency.with_name(emergency.name + ".partial"),
        ):
            if path.is_symlink():
                raise StateError(
                    f"WSL2 checkpoint path '{path.name}' is not safe to delete",
                    entity_kind="vm",
                    entity_name=vm.name,
                )
            if path.exists():
                path.unlink()
        if artifact.exists() or emergency.exists():
            raise StateError(
                f"WSL2 checkpoint '{name}' still exists after deletion",
                entity_kind="vm",
                entity_name=vm.name,
            )

    def display_backend_name(self, vm: VMRow) -> str:
        return str(vm.platform_metadata.get("distro_name", vm.name))

    def native_transport(
        self,
        vm: VMRow,
        ctx: RunContext,
        *,
        config: Config | None = None,
    ) -> Transport | None:
        # ctx is unused: wsl.exe is local and needs no backend credential.
        return WSL2Transport(distro_name=self._distro_name(vm), user=vm.admin_username)

    def status(self, vm: VMRow, ctx: RunContext) -> VMStatus:
        distro_name = self._distro_name(vm)
        try:
            listing = _wsl(["--list", "--verbose"], check=False)
        except RuntimeError:
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
