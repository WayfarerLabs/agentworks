"""Public EC2 startup policy, independently of shared SSH observation."""

import gzip

import pytest
import yaml

from agentworks.plugins.aws import _creation_bootstrap


@pytest.mark.parametrize("admin", ["root", "operator"])
def test_public_cloud_init_intentionally_selects_root_access_without_password_auth(admin):
    payload = _creation_bootstrap.early_user_data(
        admin=admin,
        public_key="ssh-ed25519 public-key",
        hostname="vm-one",
        marker="a" * 32,
    )
    config = yaml.safe_load(gzip.decompress(payload))
    assert config["disable_root"] is (admin != "root")
    assert config["ssh_pwauth"] is False
    assert config["users"][0]["name"] == admin
    assert config["users"][0]["ssh_authorized_keys"] == ["ssh-ed25519 public-key"]
