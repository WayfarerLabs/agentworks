"""Azure DevOps git host provider -- SSH key management via REST API."""

from __future__ import annotations

import json
import subprocess

from agentworks.git_hosts.base import GitHostProvider


class AzDOProvider(GitHostProvider):
    """Manages SSH keys on Azure DevOps using Azure AD tokens."""

    def __init__(self, org: str) -> None:
        self._org = org

    def verify_auth(self) -> bool:
        try:
            result = subprocess.run(
                ["az", "account", "get-access-token", "--resource", "499b84ac-1321-427f-aa17-267ca6975798"],
                capture_output=True,
                text=True,
            )
            return result.returncode == 0
        except FileNotFoundError:
            return False

    def auth_hint(self) -> str:
        return "Run 'az login' to authenticate with Azure AD"

    def register_key(self, vm_name: str, public_key: str) -> str:
        token = self._get_token()
        import urllib.request

        url = f"https://dev.azure.com/{self._org}/_apis/accounts/self/sshpublickeys?api-version=7.0"
        body = json.dumps({
            "displayName": f"agentworks-{vm_name}",
            "publicKeyData": public_key,
            "isUsed": False,
        }).encode()

        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read())
            return str(data["id"])

    def test_key_present(self, remote_key_id: str) -> bool:
        token = self._get_token()
        import urllib.request

        url = (
            f"https://dev.azure.com/{self._org}/_apis/accounts/self/sshpublickeys/{remote_key_id}"
            f"?api-version=7.0"
        )
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req):
                return True
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return False
            raise

    def remove_key(self, remote_key_id: str) -> None:
        token = self._get_token()
        import urllib.request

        url = (
            f"https://dev.azure.com/{self._org}/_apis/accounts/self/sshpublickeys/{remote_key_id}"
            f"?api-version=7.0"
        )
        req = urllib.request.Request(url, method="DELETE", headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req):
                pass
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise

    def _get_token(self) -> str:
        result = subprocess.run(
            ["az", "account", "get-access-token", "--resource", "499b84ac-1321-427f-aa17-267ca6975798", "--query",
             "accessToken", "-o", "tsv"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Failed to get Azure AD token: {result.stderr.strip()}")
        return result.stdout.strip()
