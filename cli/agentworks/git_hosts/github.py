"""GitHub git host provider -- SSH key management via gh CLI."""

from __future__ import annotations

import json
import shutil
import subprocess

from agentworks.git_hosts.base import GitHostProvider


class GitHubProvider(GitHostProvider):
    """Manages SSH keys on GitHub using the gh CLI."""

    def verify_auth(self) -> bool:
        if not shutil.which("gh"):
            return False
        result = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0

    def auth_hint(self) -> str:
        if not shutil.which("gh"):
            return "Install the GitHub CLI (gh) from https://cli.github.com"
        return (
            "Run 'gh auth login' and then "
            "'gh auth refresh -s admin:public_key' for SSH key management"
        )

    def register_key(self, vm_name: str, public_key: str) -> str:
        result = subprocess.run(
            [
                "gh", "api", "user/keys",
                "-f", f"title=agentworks-{vm_name}",
                "-f", f"key={public_key}",
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip()
            if "422" in stderr or "already" in stderr.lower():
                raise RuntimeError(
                    f"GitHub rejected the SSH key (may already exist): {stderr}"
                )
            raise RuntimeError(f"Failed to register SSH key with GitHub: {stderr}")

        data = json.loads(result.stdout)
        return str(data["id"])

    def test_key_present(self, remote_key_id: str) -> bool:
        result = subprocess.run(
            ["gh", "api", f"user/keys/{remote_key_id}"],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0

    def remove_key(self, remote_key_id: str) -> None:
        result = subprocess.run(
            ["gh", "api", "-X", "DELETE", f"user/keys/{remote_key_id}"],
            capture_output=True,
            text=True,
        )
        # Ignore 404 (already deleted)
        if result.returncode != 0 and "404" not in result.stderr:
            raise RuntimeError(
                f"Failed to remove SSH key from GitHub: {result.stderr.strip()}"
            )
