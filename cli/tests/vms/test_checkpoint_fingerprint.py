"""Managed-checkpoint desired-state fingerprint boundaries."""

from __future__ import annotations

from agentworks.apt import AptSourceEntry
from agentworks.bootstrap import load_request_registry
from agentworks.debian import DebianRelease
from agentworks.vms.manager.checkpoint_fingerprint import (
    _resource_spec,
    checkpoint_desired_state_fingerprint,
)


def test_fingerprint_excludes_runtime_observations_but_includes_provider_identity(
    db,  # noqa: ANN001
    make_config,  # noqa: ANN001
) -> None:
    config = make_config()
    db.insert_vm("box", site="proxmox", hostname="box")
    vm = db.get_vm("box")
    assert vm is not None
    registry = load_request_registry(config, live_database=db)

    baseline = checkpoint_desired_state_fingerprint(
        db,
        config,
        registry,
        vm,
        capture_release=DebianRelease.BOOKWORM,
    )
    db.update_vm_tailscale("box", "100.64.0.8")
    db.update_vm_last_seen("box")
    observed = checkpoint_desired_state_fingerprint(
        db,
        config,
        registry,
        vm,
        capture_release=DebianRelease.BOOKWORM,
    )
    assert observed == baseline

    db.update_vm_platform_metadata("box", {"vmid": "100", "node": "pve1"})
    changed_identity = checkpoint_desired_state_fingerprint(
        db,
        config,
        registry,
        vm,
        capture_release=DebianRelease.BOOKWORM,
    )
    assert changed_identity != baseline


def test_release_mapped_apt_source_uses_immutable_capture_release() -> None:
    source = AptSourceEntry(
        name="mapped",
        key_url="https://example.test/key.gpg",
        key_path="/etc/apt/keyrings/example.gpg",
        source=None,
        sources={
            DebianRelease.BOOKWORM: "deb https://example.test/debian bookworm main",
            DebianRelease.TRIXIE: "deb https://example.test/debian trixie main",
        },
        source_file="example.list",
    )

    bookworm = _resource_spec(source, DebianRelease.BOOKWORM)
    trixie = _resource_spec(source, DebianRelease.TRIXIE)

    assert isinstance(bookworm, dict)
    assert isinstance(trixie, dict)
    assert bookworm["source"] == "deb https://example.test/debian bookworm main"
    assert trixie["source"] == "deb https://example.test/debian trixie main"
    assert "sources" not in bookworm
    assert "sources" not in trixie
