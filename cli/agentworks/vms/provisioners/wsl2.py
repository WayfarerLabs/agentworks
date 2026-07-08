"""WSL2 provisioner -- imports Debian distros on Windows."""

from __future__ import annotations

import contextlib
import json
import os
import platform
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.db import VMStatus
from agentworks.transports import WSL2Transport, transport, wait_for_reconnect
from agentworks.vms.base import ProvisionResult, VMProvisioner

if TYPE_CHECKING:
    from collections.abc import Iterator
    from contextlib import AbstractContextManager

    from agentworks.config import Config
    from agentworks.db import VMRow
    from agentworks.transports import Transport


# -- Win32 job-object machinery for orphan-proof subprocess cleanup ----------
#
# Without this, an `agw` process that gets hard-killed (SIGKILL, console
# window closed, Python crash) leaves its `wsl.exe sleep infinity` subprocess
# orphaned and still anchoring the WSL2 distro -- defeating the user's
# expectation that idle-shutdown resumes after the command dies. Windows'
# job-object KILL_ON_JOB_CLOSE flag exists exactly for this case: when the
# last handle to the job closes (which the OS guarantees on process death,
# however the death happens), all processes assigned to the job are killed
# by the kernel.
#
# Wired up lazily on Windows; on other platforms _kernel32 stays None and
# every helper short-circuits to a no-op. Failures during ctypes setup are
# swallowed so that an unusual Windows configuration doesn't break WSL2
# provisioning -- we fall back to terminate-only cleanup, accepting the
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
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
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
_DOCKER_MANIFESTS_URL = "https://registry-1.docker.io/v2/library/debian/manifests/bookworm"
_DOCKER_BLOBS_URL = "https://registry-1.docker.io/v2/library/debian/blobs"

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
    """Run a wsl.exe command and return stdout."""
    result = subprocess.run(
        ["wsl", *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"wsl command failed: {result.stderr.strip()}")
    return result.stdout


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


def _download_debian_rootfs(tarball_path: Path) -> None:
    """Download the official Debian rootfs from Docker Hub OCI registry.

    Pulls the rootfs layer from the official debian:bookworm image without
    requiring Docker to be installed. The layer is a tar.gz that works
    directly with ``wsl --import``.
    """
    # 1. Get anonymous pull token
    output.detail("Authenticating with Docker Hub...")
    with urllib.request.urlopen(_DOCKER_AUTH_URL) as resp:
        token = json.loads(resp.read())["token"]

    # 2. Fetch image manifest to find the rootfs layer digest.
    #    debian:bookworm is multi-arch, so we first get the manifest list
    #    and resolve the platform-specific manifest for the host architecture.
    output.detail("Fetching Debian bookworm image manifest...")
    auth_header = {"Authorization": f"Bearer {token}"}

    req = urllib.request.Request(
        _DOCKER_MANIFESTS_URL,
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
            raise RuntimeError(f"No {arch}/linux manifest found for debian:bookworm")
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

    # 3. Download the rootfs layer with progress
    blob_url = f"{_DOCKER_BLOBS_URL}/{digest}"
    req = urllib.request.Request(blob_url, headers=auth_header)
    p = output.progress("Downloading Debian rootfs", total=total_bytes or None)

    with _blob_opener.open(req) as resp, tarball_path.open("wb") as f:
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


@contextlib.contextmanager
def _keepalive(vm: VMRow, config: Config | None) -> Iterator[None]:
    """Anchor a WSL2 distro for the duration of the context.

    Spawns ``wsl --distribution NAME -- sleep infinity`` as a background
    subprocess. While that wsl.exe is attached, Windows' WSL idle timer
    (``vmIdleTimeout`` in .wslconfig, default ~60s) doesn't fire, so the
    distro stays up regardless of whether anything else is talking to it.
    If the distro happens to be stopped on entry, the same subprocess
    boots it.

    When the VM has already joined Tailscale (``vm.tailscale_host`` is
    set) AND we have a config to build an SSH target from, we wait for
    Tailscale SSH to be reachable before yielding -- a stopped distro
    needs a few seconds for tailscaled to reattach to the tailnet after
    boot, and callers expect a ready VM.

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
        ["wsl", "--distribution", vm.name, "--", "sleep", "infinity"],
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
        output.detail(
            "(note: Win32 Job Object unavailable; a hard-kill of this command may leave an orphan wsl.exe.)"
        )

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
        stderr = (proc.stderr.read().decode("utf-8", errors="replace").strip() if proc.stderr else "")
        _close_stderr()
        _close_handle(h_job)
        raise RuntimeError(
            f"WSL2 keepalive for distro {vm.name!r} exited immediately (rc={rc})"
            + (f": {stderr}" if stderr else "")
        )
    output.detail(f"Preventing idle-shutdown of WSL2 distro {vm.name!r} for the duration of this command...")
    try:
        if vm.tailscale_host and config is not None:
            target = transport(vm, config)
            wait_for_reconnect(target)
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


class WSL2Provisioner(VMProvisioner):
    """Provisions WSL2 Debian distributions on Windows."""

    def vm_active(
        self, vm: VMRow, *, config: Config | None = None
    ) -> AbstractContextManager[None]:
        return _keepalive(vm, config)

    def create(
        self,
        vm_name: str,
        config: Config,
        *,
        swap: int = 4,
        admin_username: str = "agentworks",
    ) -> ProvisionResult:
        output.info(f"Provisioning WSL2 VM '{vm_name}'...")

        install_path = _wsl_base_path() / vm_name
        _powershell(f"New-Item -ItemType Directory -Force -Path {_ps_quote(install_path)}")

        # Download Debian rootfs if not cached
        cache_dir = _cache_dir()
        tarball = cache_dir / f"debian-bookworm-{_oci_arch()}-rootfs.tar.gz"
        _powershell(f"New-Item -ItemType Directory -Force -Path {_ps_quote(cache_dir)}")

        if not tarball.exists():
            _download_debian_rootfs(tarball)
        else:
            output.detail("Using cached Debian rootfs.")

        # Import and configure the distro
        output.detail("Importing rootfs into WSL2...")
        _wsl(["--import", vm_name, str(install_path), str(tarball)])

        # Strip Docker-image minimization hooks before we run any apt-get.
        # The official debian:bookworm Docker rootfs ships /usr/sbin/policy-rc.d
        # that returns 101 to refuse all service starts during image build;
        # without removing it, apt-installed daemons (e.g. tailscaled) never
        # start, leaving us with an "installed but inert" service.
        output.detail("Removing Docker minimization hooks...")
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
        output.detail("Installing base packages...")
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
                " && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq -o Dpkg::Options::=--force-confnew"
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
            output.detail(f"Setting up {swap} GiB swap file...")
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
        output.detail(f"Creating user '{admin_username}'...")
        _wsl(["--distribution", vm_name, "--user", "root", "--", "useradd", "-m", "-s", "/bin/bash", admin_username])
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
        output.detail("Enabling systemd...")
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
        output.detail("Restarting distro...")
        _wsl(["--terminate", vm_name])
        # Run a command to trigger the distro to start with systemd
        _wsl(["--distribution", vm_name, "--user", "root", "--", "bash", "-c", "echo ok"])

        output.detail(f"WSL2 VM '{vm_name}' provisioned.")
        return ProvisionResult(
            provisioner_transport=WSL2Transport(distro_name=vm_name, user=admin_username),
            wsl_distro_name=vm_name,
        )

    def start(self, vm: VMRow) -> None:
        output.info(f"Starting WSL2 distro '{vm.name}'...")
        _wsl(["--distribution", vm.name, "--", "echo", "started"])
        output.info(f"WSL2 distro '{vm.name}' started")

    def stop(self, vm: VMRow) -> None:
        output.info(f"Terminating WSL2 distro '{vm.name}'...")
        _wsl(["--terminate", vm.name])
        output.info(f"WSL2 distro '{vm.name}' terminated")

    def delete(self, vm: VMRow) -> None:
        output.info(f"Unregistering WSL2 distro '{vm.name}'...")
        _wsl(["--unregister", vm.name], check=False)
        # Clean up install directory
        install_path = _wsl_base_path() / vm.name
        _powershell(
            f"Remove-Item -Recurse -Force -Path {_ps_quote(install_path)} -ErrorAction SilentlyContinue",
            check=False,
        )
        output.info(f"WSL2 distro '{vm.name}' deleted")

    def provisioner_transport(
        self, vm: VMRow, *, config: object | None = None,
    ) -> Transport:
        return WSL2Transport(distro_name=vm.name, user=vm.admin_username)

    def status(self, vm: VMRow) -> VMStatus:
        try:
            output = _wsl(["--list", "--verbose"], check=False)
        except RuntimeError:
            return VMStatus.UNKNOWN

        for line in output.strip().splitlines():
            parts = line.split()
            # WSL --list --verbose output: [*] NAME STATE VERSION
            # Filter to find our distro
            name_candidates = [p for p in parts if p == vm.name]
            if not name_candidates:
                continue
            state_str = parts[-2].lower() if len(parts) >= 3 else ""
            if state_str == "running":
                return VMStatus.RUNNING
            if state_str == "stopped":
                return VMStatus.STOPPED
            return VMStatus.UNKNOWN
        return VMStatus.UNKNOWN
