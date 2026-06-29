"""Tests that the updated sample-config.toml parses cleanly through the
framework's finalize pass and that the Tailscale auth-key secret
auto-declares from the VM-template requirement.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentworks.bootstrap import build_registry
from agentworks.config import load_config

SAMPLE_CONFIG_PATH = (
    Path(__file__).resolve().parent.parent
    / "agentworks"
    / "sample-config.toml"
)


@pytest.fixture()
def sample_config(tmp_path: Path) -> Path:
    """Copy sample-config.toml into tmp_path with the operator's SSH key
    paths replaced by tmp-path fakes. The sample uses ``~/.ssh/...``
    placeholders that don't exist in CI; the test substitutes locally
    -valid paths.
    """
    pub = tmp_path / "id_ed25519.pub"
    priv = tmp_path / "id_ed25519"
    pub.write_text("ssh-ed25519 fake-test-key")
    priv.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\n")

    src = SAMPLE_CONFIG_PATH.read_text()
    # Replace the operator section's SSH key paths with tmp_path versions.
    src = src.replace(
        "~/.ssh/id_ed25519.pub", str(pub)
    ).replace("~/.ssh/id_ed25519", str(priv))

    cfg = tmp_path / "config.toml"
    cfg.write_text(src)
    return cfg


def test_sample_config_parses_with_phase_1c_field(sample_config: Path) -> None:
    """The sample config -- including the new (commented-out)
    ``tailscale_auth_key`` field in the vm_templates.default section --
    parses cleanly through ``load_config`` and the framework's
    ``build_registry`` finalize pass.
    """
    config = load_config(sample_config, warn_issues=False)
    registry = build_registry(config)

    # The default VM template is published as a Registry entry; the
    # finalize pass auto-declared the default Tailscale secret via the
    # VMTemplate's required_resources emission.
    ts_secret = registry.lookup("secret", "tailscale-auth-key")
    assert ts_secret.origin is not None
    # In a sample config with no [secrets.tailscale-auth-key] block,
    # the auto-declare path produces it.
    assert ts_secret.origin.variant == "auto-declared"
    # First-matching-source rule: VMTemplate.required_resources emits
    # the requirement; the source is the vm_template that published it.
    assert ts_secret.origin.source == ("vm_template", "default")
