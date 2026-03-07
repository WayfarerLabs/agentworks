"""GitHub git host provider -- SSH key management via REST API."""

from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.request

from agentworks.git_hosts.base import GitHostProvider

GITHUB_API = "https://api.github.com"


class GitHubProvider(GitHostProvider):
    """Manages SSH keys on GitHub using gh cli or GITHUB_TOKEN."""

    def verify_auth(self) -> bool:
        token = self._get_token()
        return token is not None

    def auth_hint(self) -> str:
        return "Run 'gh auth login' or set the GITHUB_TOKEN environment variable"

    def register_key(self, vm_name: str, public_key: str) -> str:
        token = self._require_token()
        body = json.dumps({
            "title": f"agentworks-{vm_name}",
            "key": public_key,
        }).encode()

        req = urllib.request.Request(
            f"{GITHUB_API}/user/keys",
            data=body,
            method="POST",
            headers={
                "Authorization": f"token {token}",
                "Content-Type": "application/json",
                "Accept": "application/vnd.github+json",
            },
        )
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read())
            return str(data["id"])

    def test_key_present(self, remote_key_id: str) -> bool:
        token = self._require_token()
        req = urllib.request.Request(
            f"{GITHUB_API}/user/keys/{remote_key_id}",
            headers={
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github+json",
            },
        )
        try:
            with urllib.request.urlopen(req):
                return True
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return False
            raise

    def remove_key(self, remote_key_id: str) -> None:
        token = self._require_token()
        req = urllib.request.Request(
            f"{GITHUB_API}/user/keys/{remote_key_id}",
            method="DELETE",
            headers={
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github+json",
            },
        )
        try:
            with urllib.request.urlopen(req):
                pass
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise

    def _get_token(self) -> str | None:
        # Try GITHUB_TOKEN env var first
        env_token = os.environ.get("GITHUB_TOKEN")
        if env_token:
            return env_token
        # Try gh cli
        try:
            result = subprocess.run(
                ["gh", "auth", "token"],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except FileNotFoundError:
            pass
        return None

    def _require_token(self) -> str:
        token = self._get_token()
        if token is None:
            raise RuntimeError("GitHub authentication not available. " + self.auth_hint())
        return token
