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


class ProxmoxAPIError(RuntimeError):
    """A Proxmox API call failed."""


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
            with urllib.request.urlopen(req, context=self._ssl_ctx) as resp:
                resp_body = resp.read().decode()
        except urllib.error.HTTPError as e:
            err_body = e.read().decode() if e.fp else ""
            raise ProxmoxAPIError(
                f"Proxmox API {method} {path} failed ({e.code}): {err_body}"
            ) from e

        if not resp_body:
            return None
        parsed = json.loads(resp_body)
        return parsed.get("data")

    # -- Cluster ---------------------------------------------------------------

    def next_id(self) -> int:
        """Get the next available VMID."""
        result = self._request("GET", "/cluster/nextid")
        return int(result)

    # -- VM operations ---------------------------------------------------------

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
        result = self._request(
            "POST", f"/nodes/{node}/qemu/{template_vmid}/clone", params
        )
        return str(result)

    def configure_vm(self, node: str, vmid: int, **params: Any) -> None:
        """Update VM configuration."""
        self._request("PUT", f"/nodes/{node}/qemu/{vmid}/config", params)

    def resize_disk(
        self, node: str, vmid: int, disk: str, size: str
    ) -> None:
        """Resize a VM disk (e.g. disk='scsi0', size='+20G')."""
        self._request(
            "PUT",
            f"/nodes/{node}/qemu/{vmid}/resize",
            {"disk": disk, "size": size},
        )

    def start_vm(self, node: str, vmid: int) -> str:
        """Start a VM. Returns the task UPID."""
        result = self._request(
            "POST", f"/nodes/{node}/qemu/{vmid}/status/start"
        )
        return str(result)

    def stop_vm(self, node: str, vmid: int) -> str:
        """Stop a VM. Returns the task UPID."""
        result = self._request(
            "POST", f"/nodes/{node}/qemu/{vmid}/status/stop"
        )
        return str(result)

    def delete_vm(self, node: str, vmid: int) -> str:
        """Delete a VM. Returns the task UPID."""
        result = self._request(
            "DELETE", f"/nodes/{node}/qemu/{vmid}"
        )
        return str(result)

    def vm_status(self, node: str, vmid: int) -> dict[str, Any]:
        """Get current VM status."""
        result = self._request(
            "GET", f"/nodes/{node}/qemu/{vmid}/status/current"
        )
        return result  # type: ignore[return-value]

    # -- Tasks -----------------------------------------------------------------

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
            result = self._request(
                "GET", f"/nodes/{node}/tasks/{encoded_upid}/status"
            )
            if result and result.get("status") == "stopped":
                if result.get("exitstatus") != "OK":
                    raise ProxmoxAPIError(
                        f"Task failed: {result.get('exitstatus')}"
                    )
                return
            time.sleep(poll_interval)
        raise ProxmoxAPIError(f"Task timed out after {timeout}s: {upid}")

    # -- Guest agent -----------------------------------------------------------

    def guest_agent_network(
        self, node: str, vmid: int
    ) -> list[dict[str, Any]]:
        """Get network interfaces from the QEMU guest agent."""
        result = self._request(
            "GET", f"/nodes/{node}/qemu/{vmid}/agent/network-get-interfaces"
        )
        if result and "result" in result:
            return result["result"]  # type: ignore[no-any-return]
        return result or []  # type: ignore[return-value]

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
        Returns the result dict with exitcode, out-data, err-data.

        Proxmox 8 requires the command as a JSON array sent with
        Content-Type: application/json.
        """
        cmd_array = [command] + (args or [])

        result = self._request(
            "POST",
            f"/nodes/{node}/qemu/{vmid}/agent/exec",
            {"command": cmd_array},
            json_body=True,
        )
        pid = result.get("pid") if result else None
        if pid is None:
            return None

        # Poll for completion
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = self._request(
                "GET",
                f"/nodes/{node}/qemu/{vmid}/agent/exec-status?pid={pid}",
            )
            if status and status.get("exited"):
                return status  # type: ignore[return-value]
            time.sleep(2)

        return None

    def guest_agent_file_write(
        self, node: str, vmid: int, path: str, content: str
    ) -> None:
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
