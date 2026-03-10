"""Azure DevOps git host provider -- SSH key management via az CLI."""

from __future__ import annotations

import json
import shutil
import subprocess

from agentworks.git_hosts.base import GitHostProvider


class AzDOProvider(GitHostProvider):
    """Manages SSH keys on Azure DevOps using the az CLI."""

    def __init__(self, org: str) -> None:
        self._org = org

    def _api_url(self, path: str = "") -> str:
        base = f"https://dev.azure.com/{self._org}/_apis/accounts/self/sshpublickeys"
        if path:
            return f"{base}/{path}?api-version=7.0"
        return f"{base}?api-version=7.0"

    def verify_auth(self) -> bool:
        if not shutil.which("az"):
            return False
        result = subprocess.run(
            ["az", "account", "show"],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0

    def auth_hint(self) -> str:
        if not shutil.which("az"):
            return "Install the Azure CLI (az) from https://learn.microsoft.com/en-us/cli/azure/install-azure-cli"
        return "Run 'az login' to authenticate with Azure AD"

    def register_key(self, vm_name: str, public_key: str) -> str:
        body = json.dumps({
            "displayName": f"agentworks-{vm_name}",
            "publicKeyData": public_key,
            "isUsed": False,
        })
        result = subprocess.run(
            [
                "az", "rest",
                "--method", "POST",
                "--url", self._api_url(),
                "--body", body,
                "--resource", "499b84ac-1321-427f-aa17-267ca6975798",
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip()
            if "already" in stderr.lower() or "409" in stderr:
                raise RuntimeError(
                    f"Azure DevOps rejected the SSH key (may already exist): {stderr}"
                )
            raise RuntimeError(f"Failed to register SSH key with Azure DevOps: {stderr}")

        data = json.loads(result.stdout)
        return str(data["id"])

    def test_key_present(self, remote_key_id: str) -> bool:
        result = subprocess.run(
            [
                "az", "rest",
                "--method", "GET",
                "--url", self._api_url(remote_key_id),
                "--resource", "499b84ac-1321-427f-aa17-267ca6975798",
            ],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0

    def remove_key(self, remote_key_id: str) -> None:
        result = subprocess.run(
            [
                "az", "rest",
                "--method", "DELETE",
                "--url", self._api_url(remote_key_id),
                "--resource", "499b84ac-1321-427f-aa17-267ca6975798",
            ],
            capture_output=True,
            text=True,
        )
        # Ignore 404 (already deleted)
        if result.returncode != 0 and "404" not in result.stderr:
            raise RuntimeError(
                f"Failed to remove SSH key from Azure DevOps: {result.stderr.strip()}"
            )
