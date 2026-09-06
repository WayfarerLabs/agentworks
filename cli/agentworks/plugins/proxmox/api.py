"""Thin REST client for the Proxmox VE API.

Uses stdlib urllib.request -- no external dependencies. Authentication
is via PVEAPIToken (token ID + secret), not session cookies.
"""

from __future__ import annotations

import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from agentworks.errors import ProvisioningError


class ProxmoxAPIError(ProvisioningError):
    """A Proxmox API call failed.

    ``code`` carries the HTTP status when the failure was an HTTP error
    (``None`` for a non-HTTP failure), so callers can distinguish an
    auth rejection (401/403) from other failures without parsing the
    message. Used by the platform's ``runup`` token check."""

    code: int | None = None


_QGA_BOOLEAN_FIELDS = ("exited", "out-truncated", "err-truncated")
_QGA_COMPLETION_FIELDS = {
    "exitcode",
    "signal",
    "out-data",
    "err-data",
    "out-truncated",
    "err-truncated",
}


class _InvalidQGAExecStatus(ValueError):
    """A QGA exec-status response violates the provider contract."""


def _validate_qga_exec_status(status: dict[str, object]) -> dict[str, object]:
    """Normalize exact wire booleans and validate one QGA status shape."""
    normalized = dict(status)
    for field in _QGA_BOOLEAN_FIELDS:
        if field not in normalized:
            continue
        value = normalized[field]
        if type(value) is bool:
            continue
        if type(value) is int and value in (0, 1):
            normalized[field] = bool(value)
            continue
        raise _InvalidQGAExecStatus(f"Proxmox guest-agent exec-status returned an invalid {field} field")

    exited = normalized.get("exited")
    if type(exited) is not bool:
        raise _InvalidQGAExecStatus("Proxmox guest-agent exec-status returned an invalid exited field")
    if not exited:
        if _QGA_COMPLETION_FIELDS.intersection(normalized):
            raise _InvalidQGAExecStatus("Proxmox guest-agent exec-status reported completion data before exit")
        return normalized

    has_exitcode = "exitcode" in normalized
    has_signal = "signal" in normalized
    if has_exitcode == has_signal:
        raise _InvalidQGAExecStatus("Proxmox guest-agent exec-status returned an invalid exit status")
    if has_exitcode:
        exitcode = normalized["exitcode"]
        if type(exitcode) is not int or exitcode < 0:
            raise _InvalidQGAExecStatus("Proxmox guest-agent exec-status returned an invalid exit status")
    else:
        signal = normalized["signal"]
        if type(signal) is not int or signal <= 0:
            raise _InvalidQGAExecStatus("Proxmox guest-agent exec-status returned an invalid exit status")

    stdout = normalized.get("out-data", "")
    stderr = normalized.get("err-data", "")
    if not isinstance(stdout, str) or not isinstance(stderr, str):
        raise _InvalidQGAExecStatus("Proxmox guest-agent exec-status returned invalid output fields")

    out_truncated = normalized.get("out-truncated", False)
    err_truncated = normalized.get("err-truncated", False)
    if type(out_truncated) is not bool or type(err_truncated) is not bool:
        raise _InvalidQGAExecStatus("Proxmox guest-agent exec-status returned invalid truncation fields")
    if out_truncated or err_truncated:
        raise _InvalidQGAExecStatus("Proxmox guest-agent exec-status reported truncated output")
    return normalized


class ProxmoxAPI:
    """Minimal Proxmox VE REST client."""

    def __init__(
        self,
        api_url: str,
        token_id: str,
        token_secret: str,
        verify_ssl: bool = True,
    ) -> None:
        # Strip trailing slash for consistent URL building
        self._base = api_url.rstrip("/") + "/api2/json"
        self._auth = f"PVEAPIToken={token_id}={token_secret}"
        self._ssl_ctx: ssl.SSLContext | None = None
        if not verify_ssl:
            self._ssl_ctx = ssl.create_default_context()
            self._ssl_ctx.check_hostname = False
            self._ssl_ctx.verify_mode = ssl.CERT_NONE

    # -- Low-level request -----------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        data: dict[str, Any] | None = None,
        *,
        json_body: bool = False,
        timeout: float | None = None,
    ) -> Any:
        """Send an API request and return the parsed JSON ``data`` field.

        Args:
            json_body: If True, send data as JSON (required for guest agent
                       endpoints). Otherwise use form-urlencoded.
        """
        url = f"{self._base}{path}"

        body: bytes | None = None
        content_type = "application/x-www-form-urlencoded"
        if data is not None:
            if json_body:
                body = json.dumps(data).encode()
                content_type = "application/json"
            else:
                body = urllib.parse.urlencode(data).encode()

        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("Authorization", self._auth)
        if body is not None:
            req.add_header("Content-Type", content_type)

        try:
            response = (
                urllib.request.urlopen(req, context=self._ssl_ctx)
                if timeout is None
                else urllib.request.urlopen(req, context=self._ssl_ctx, timeout=timeout)
            )
            with response as resp:
                resp_body = resp.read().decode()
        except urllib.error.HTTPError as e:
            err_body = e.read().decode() if e.fp else ""
            err = ProxmoxAPIError(f"Proxmox API {method} {path} failed ({e.code}): {err_body}")
            err.code = e.code
            raise err from e
        except (OSError, UnicodeError) as e:
            raise ProxmoxAPIError(f"Proxmox API {method} {path} failed: {e}") from e

        if not resp_body:
            return None
        try:
            parsed = json.loads(resp_body)
        except json.JSONDecodeError as e:
            raise ProxmoxAPIError(f"Proxmox API {method} {path} returned invalid JSON") from e
        if not isinstance(parsed, dict) or "data" not in parsed:
            raise ProxmoxAPIError(f"Proxmox API {method} {path} returned a malformed response")
        return parsed["data"]

    # -- Cluster ---------------------------------------------------------------

    def next_id(self) -> int:
        """Get the next available VMID."""
        result = self._request("GET", "/cluster/nextid")
        return int(result)

    # -- VM operations ---------------------------------------------------------

    def list_vms(self, node: str) -> list[dict[str, Any]]:
        """List the VMs on a node (GET /nodes/{node}/qemu)."""
        result = self._request("GET", f"/nodes/{node}/qemu")
        return result if isinstance(result, list) else []

    def clone_vm(
        self,
        node: str,
        template_vmid: int,
        newid: int,
        name: str,
        *,
        storage: str | None = None,
        pool: str | None = None,
        full: bool = True,
    ) -> str:
        """Clone a VM template. Returns the task UPID."""
        params: dict[str, Any] = {
            "newid": newid,
            "name": name,
            "full": int(full),
        }
        if storage:
            params["storage"] = storage
        if pool:
            params["pool"] = pool
        result = self._request("POST", f"/nodes/{node}/qemu/{template_vmid}/clone", params)
        return str(result)

    def configure_vm(self, node: str, vmid: int, **params: Any) -> None:
        """Update VM configuration."""
        self._request("PUT", f"/nodes/{node}/qemu/{vmid}/config", params)

    def resize_disk(self, node: str, vmid: int, disk: str, size: str) -> None:
        """Resize a VM disk (e.g. disk='scsi0', size='+20G')."""
        self._request(
            "PUT",
            f"/nodes/{node}/qemu/{vmid}/resize",
            {"disk": disk, "size": size},
        )

    def start_vm(self, node: str, vmid: int) -> str:
        """Start a VM. Returns the task UPID."""
        result = self._request("POST", f"/nodes/{node}/qemu/{vmid}/status/start")
        return str(result)

    def stop_vm(self, node: str, vmid: int) -> str:
        """Stop a VM. Returns the task UPID."""
        result = self._request("POST", f"/nodes/{node}/qemu/{vmid}/status/stop")
        return str(result)

    def delete_vm(self, node: str, vmid: int) -> str:
        """Delete a VM. Returns the task UPID."""
        result = self._request("DELETE", f"/nodes/{node}/qemu/{vmid}")
        return str(result)

    def vm_status(self, node: str, vmid: int, *, timeout: float | None = None) -> dict[str, Any]:
        """Get current VM status."""
        result = self._request("GET", f"/nodes/{node}/qemu/{vmid}/status/current", timeout=timeout)
        return result  # type: ignore[no-any-return]

    # -- Tasks -----------------------------------------------------------------

    def stop_task(self, node: str, upid: str) -> None:
        """Stop a running task (DELETE /nodes/{node}/tasks/{upid}).

        Used by create's rollback to cancel an in-flight clone so the
        target VMID unlocks in seconds instead of after the full clone."""
        encoded_upid = urllib.parse.quote(upid, safe="")
        self._request("DELETE", f"/nodes/{node}/tasks/{encoded_upid}")

    def wait_for_task(
        self,
        node: str,
        upid: str,
        *,
        timeout: int = 300,
        poll_interval: float = 2.0,
    ) -> None:
        """Poll a task until it completes or times out."""
        encoded_upid = urllib.parse.quote(upid, safe="")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = self._request("GET", f"/nodes/{node}/tasks/{encoded_upid}/status")
            if result and result.get("status") == "stopped":
                if result.get("exitstatus") != "OK":
                    raise ProxmoxAPIError(f"Task failed: {result.get('exitstatus')}")
                return
            time.sleep(poll_interval)
        raise ProxmoxAPIError(f"Task timed out after {timeout}s: {upid}")

    # -- Guest agent -----------------------------------------------------------

    def guest_agent_network(self, node: str, vmid: int) -> list[dict[str, Any]]:
        """Get network interfaces from the QEMU guest agent."""
        result = self._request("GET", f"/nodes/{node}/qemu/{vmid}/agent/network-get-interfaces")
        if result and "result" in result:
            data = result["result"]
            if isinstance(data, list):
                return data
            raise ProxmoxAPIError(f"unexpected network-get-interfaces shape: {type(data).__name__}")
        return []

    def guest_agent_exec_wait(
        self,
        node: str,
        vmid: int,
        command: str,
        args: list[str] | None = None,
        *,
        timeout: int = 60,
    ) -> dict[str, Any] | None:
        """Run a command via the guest agent and wait for completion.

        Uses exec then polls exec-status until finished or timeout.
        Returns a validated completion status, or ``None`` at the deadline.

        Proxmox 8 requires the command as a JSON array sent with
        Content-Type: application/json.
        """
        deadline = time.monotonic() + timeout
        pid = self.guest_agent_exec(
            node,
            vmid,
            command=[command, *(args or [])],
            timeout=max(deadline - time.monotonic(), 0.001),
        )
        while time.monotonic() < deadline:
            status = self.guest_agent_exec_status(
                node,
                vmid,
                pid=pid,
                timeout=max(deadline - time.monotonic(), 0.001),
            )
            if status.get("exited") is True:
                return status
            time.sleep(min(2, max(deadline - time.monotonic(), 0)))

        return None

    def guest_agent_exec(
        self,
        node: str,
        vmid: int,
        *,
        command: list[str],
        input_data: str | None = None,
        timeout: float | None = None,
    ) -> int:
        """Dispatch one QGA command and return its provider PID."""
        payload: dict[str, Any] = {"command": command}
        if input_data is not None:
            payload["input-data"] = input_data
        result = self._request(
            "POST",
            f"/nodes/{node}/qemu/{vmid}/agent/exec",
            payload,
            json_body=True,
            timeout=timeout,
        )
        if not isinstance(result, dict) or type(result.get("pid")) is not int:
            raise ProxmoxAPIError("Proxmox guest-agent exec returned a malformed response")
        return int(result["pid"])

    def guest_agent_exec_status(
        self,
        node: str,
        vmid: int,
        *,
        pid: int,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Read one QGA command's current status."""
        result = self._request(
            "GET",
            f"/nodes/{node}/qemu/{vmid}/agent/exec-status?pid={pid}",
            timeout=timeout,
        )
        if not isinstance(result, dict):
            raise ProxmoxAPIError("Proxmox guest-agent exec-status returned a malformed response")
        try:
            return _validate_qga_exec_status(result)
        except _InvalidQGAExecStatus as error:
            raise ProxmoxAPIError(str(error)) from None

    def guest_agent_file_write(self, node: str, vmid: int, path: str, content: str) -> None:
        """Write a file inside the VM via the guest agent.

        Sends raw content and lets Proxmox handle base64 encoding
        for the guest agent.
        """
        self._request(
            "POST",
            f"/nodes/{node}/qemu/{vmid}/agent/file-write",
            {"file": path, "content": content},
            json_body=True,
        )
