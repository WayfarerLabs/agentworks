"""Restoring persisted settings cannot restore an earlier managed trust policy."""

from __future__ import annotations

import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
import tomli_w

from agentworks.config import load_config
from agentworks.errors import StateError
from agentworks.execution.carriers.ssh.trust import (
    ManagedSSHTrust,
    SSHTrustFiles,
    TrustBlockedError,
    import_trust,
    refresh_trust,
    resolve_trust,
    trust_status,
)

pytestmark = pytest.mark.windows


def _bytes(directory: Path) -> dict[Path, bytes]:
    return {path.relative_to(directory): path.read_bytes() for path in directory.rglob("*") if path.is_file()}


def _reopen(config: Path, backup: Path, bundle: ManagedSSHTrust, generation: str, blocked: bool) -> None:
    script = """
import importlib.abc
import sys
from pathlib import Path

class RefuseLegacy(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == name or fullname.startswith(name + ".") for name in (
            "agentworks.ssh", "agentworks.ssh_config", "agentworks.ssh_identity",
            "agentworks.transports", "agentworks.runners", "agentworks.db",
        )):
            raise AssertionError("rollback reopening imported " + fullname)

sys.meta_path.insert(0, RefuseLegacy())
from agentworks.config import load_config
from agentworks.execution.carriers.ssh.trust import (
    ManagedSSHTrust, TrustBlockedError, resolve_trust, trust_status,
)
config, backup, directory = (Path(value) for value in sys.argv[1:4])
assert config.read_bytes() == backup.read_bytes()
old = load_config(backup, warn_issues=False)
restored = load_config(config, warn_issues=False)
assert restored.operator == old.operator
assert restored.operator.ssh is None
bundle = ManagedSSHTrust(directory)
status = trust_status(bundle)
assert status.generation == sys.argv[4]
assert status.blocked == (sys.argv[5] == "True")
assert status.authority == "fixture complete-policy maintainer"
if status.blocked:
    try:
        resolve_trust(bundle)
    except TrustBlockedError:
        pass
    else:
        raise AssertionError("restored configuration re-enabled blocked policy")
else:
    selected = resolve_trust(bundle)
    assert tuple(path.read_bytes() for path in selected.known_hosts) == tuple(
        path.read_bytes() for path in status.sources.known_hosts
    )
    assert selected.revoked_host_keys is not None
    assert status.sources.revoked_host_keys is not None
    assert selected.revoked_host_keys.read_bytes() == status.sources.revoked_host_keys.read_bytes()
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(config), str(backup), str(bundle.directory), generation, str(blocked)],
        capture_output=True,
        text=True,
        timeout=20,
        cwd=Path(__file__).resolve().parents[4],
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("failed_refresh", [False, True], ids=["newer-generation", "blocked-generation"])
def test_persisted_config_restoration_retains_current_complete_policy(tmp_path: Path, failed_refresh: bool) -> None:
    root = tmp_path.resolve()
    legacy = root / "legacy"
    legacy.mkdir()
    for name, content in (
        ("identity", b"fixture private identity\r\n"),
        ("identity.pub", b"fixture public identity\n"),
        ("extra.pub", b"fixture additional identity\n"),
        ("config", b"Host manual-alias\n    HostName fixture.invalid\n"),
        ("known-hosts", b"# original policy\r\n|1|hashed|record key\r\n[alias]:2222 key"),
        ("authorities", b"@cert-authority *.example key\n@revoked host key\n"),
        ("revoked.krl", b"SSHKRL\n\0\0\xff\x80\r\n"),
    ):
        (legacy / name).write_bytes(content)
    original_files = _bytes(legacy)
    operator = {
        "ssh_private_key": str(legacy / "identity"),
        "ssh_public_key": str(legacy / "identity.pub"),
        "ssh_config": str(legacy / "config"),
        "ssh_config_dir": False,
        "ssh_host_prefix": "retained--",
        "ssh_agent_host_prefix": "retained-agent--",
        "extra_ssh_public_keys": [str(legacy / "extra.pub")],
        "ssh_allow_cidrs": ["192.0.2.0/24"],
    }
    config = root / "config.toml"
    original = b"# operator-owned copy\r\n" + tomli_w.dumps({"operator": operator}).encode()
    config.write_bytes(original)
    backup = root / "before.toml"
    backup.write_bytes(original)
    old = load_config(config, warn_issues=False)
    assert old.operator.ssh is None
    assert old.config_issues == ()
    assert config.read_bytes() == original
    directory = root / "bundle"
    additive = original + tomli_w.dumps({"operator": {"ssh": {"trust_store": str(directory)}}}).encode()
    config.write_bytes(additive)
    loaded = load_config(config, warn_issues=False)
    assert replace(loaded.operator, ssh=None) == old.operator
    assert loaded.operator.ssh is not None
    assert loaded.operator.ssh.identity_file == old.operator.ssh_private_key
    assert config.read_bytes() == additive
    assert not directory.exists()

    sources = SSHTrustFiles((legacy / "known-hosts", legacy / "authorities"), legacy / "revoked.krl")
    bundle = import_trust(loaded.operator.ssh.trust_store, sources=sources, authority="fixture import maintainer")
    initial = trust_status(bundle)
    # Opaque retained-key/KRL fixtures prove custody, not OpenSSH interpretation or enrollment.
    learned = root / "learned-known-hosts"
    learned.write_bytes(b"[new-target]:2222 ssh-ed25519 learned-fixture-key\n")
    revoked = root / "current.krl"
    revoked.write_bytes(original_files[Path("revoked.krl")] + b"\0new-revocation\xff")
    complete = SSHTrustFiles((*sources.known_hosts, learned), revoked)
    current = refresh_trust(
        bundle, sources=complete, authority="fixture complete-policy maintainer", expected_generation=initial.generation
    )
    assert current.generation is not None and current.generation != initial.generation
    selected = resolve_trust(bundle)
    assert tuple(path.read_bytes() for path in selected.known_hosts) == tuple(
        path.read_bytes() for path in complete.known_hosts
    )
    assert selected.revoked_host_keys is not None
    assert selected.revoked_host_keys.read_bytes() == revoked.read_bytes()
    if failed_refresh:
        generations = {path for path in directory.iterdir() if path.is_dir()}
        with pytest.raises(StateError):
            refresh_trust(
                bundle,
                sources=SSHTrustFiles(complete.known_hosts, root / "missing.krl"),
                authority="fixture incomplete update",
                expected_generation=current.generation,
            )
        assert trust_status(bundle) == replace(current, blocked=True)
        partial_generations = {path for path in directory.iterdir() if path.is_dir()} - generations
        assert any(
            tuple((partial / path.name).read_bytes() for path in selected.known_hosts)
            == tuple(path.read_bytes() for path in complete.known_hosts)
            for partial in partial_generations
        )
    retained = _bytes(directory)
    config.write_bytes(backup.read_bytes())
    assert load_config(config, warn_issues=False).operator == old.operator
    _reopen(config, backup, bundle, current.generation, failed_refresh)
    assert config.read_bytes() == original
    assert _bytes(directory) == retained
    assert _bytes(legacy) == original_files
    # A stale writer cannot use restored configuration to publish the initial policy.
    with pytest.raises(StateError):
        refresh_trust(bundle, sources=sources, authority="stale fixture writer", expected_generation=initial.generation)
    assert _bytes(directory) == retained

    config.write_bytes(additive)
    settings = load_config(config, warn_issues=False).operator.ssh
    assert settings is not None
    reopened = ManagedSSHTrust(settings.trust_store)
    if failed_refresh:
        with pytest.raises(TrustBlockedError):
            resolve_trust(reopened)
        repaired = refresh_trust(
            reopened,
            sources=selected,
            authority=current.authority,
            expected_generation=current.generation,
        )
        assert repaired.generation != current.generation and not repaired.blocked
    else:
        assert trust_status(reopened) == current
    final = resolve_trust(reopened)
    assert tuple(path.read_bytes() for path in final.known_hosts) == tuple(
        path.read_bytes() for path in complete.known_hosts
    )
    assert final.revoked_host_keys is not None
    assert final.revoked_host_keys.read_bytes() == revoked.read_bytes()
    assert all(
        (directory / path).read_bytes() == content for path, content in retained.items() if path != Path("state.json")
    )
    assert _bytes(legacy) == original_files
    assert backup.read_bytes() == original
    assert config.read_bytes() == additive
