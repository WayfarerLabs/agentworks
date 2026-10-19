"""Complete host access declarations coexist with unchanged legacy Lima routes."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import yaml
from pydantic import ValidationError

from agentworks.capabilities.vm_platform.lima import LimaConfig, LimaPlatform, LimaSshPlacement
from agentworks.capabilities.vm_platform.ssh_host import SSHPlatformHostAccessConfig


def _access() -> dict[str, object]:
    return dict(resource_key="shared-host", endpoint="literal.example", user="worker", target_os="darwin")


@pytest.mark.parametrize("access", [None, _access()])
@pytest.mark.parametrize("ssh_available", [False, True])
def test_loaded_placement_keeps_legacy_destination_and_readiness(monkeypatch, access, ssh_available):
    import agentworks.ssh

    placement: dict[str, object] = dict(mode="ssh", host="me@box")
    if access is not None:
        placement["access"] = access
    raw = yaml.safe_load(yaml.safe_dump(dict(name="lima", placement=placement)))
    platform = LimaPlatform("site-one", raw)
    assert platform.is_remote and platform._remote_host == "me@box"
    assert isinstance(platform.config.placement, LimaSshPlacement)
    declared = platform.config.placement.access
    assert (declared is None) is (access is None)
    if declared is not None:
        assert declared.endpoint == "literal.example" and declared.user == "worker"
        assert declared.port == 22 and declared.python_path is None
    probes = []

    def which(command):
        probes.append(command)
        return "/synthetic/ssh" if ssh_available else None

    monkeypatch.setattr("shutil.which", which)
    readiness = LimaPlatform.not_ready(raw)
    assert readiness.is_ready is ssh_available
    assert probes == ["ssh"]
    calls = []

    def run(target, command, **options):
        calls.append((target, command, options))
        return SimpleNamespace(stdout="legacy-result")

    monkeypatch.setattr(agentworks.ssh, "run", run)
    assert platform._run_lima("limactl list", timeout=5) == "legacy-result"
    target, command, options = calls[0]
    assert target.host == "me@box" and target.user is None and target.login_shell
    assert command == "limactl list" and options["timeout"] == 5


@pytest.mark.parametrize("field", ["resource_key", "endpoint", "user", "target_os"])
def test_access_requires_complete_declaration(field):
    access = _access()
    del access[field]
    with pytest.raises(ValidationError):
        LimaConfig.model_validate(dict(name="lima", placement=dict(mode="ssh", host="me@box", access=access)))


@pytest.mark.parametrize(
    "change",
    [
        {"unknown": True},
        {"resource_key": ""},
        {"resource_key": "a" * 256},
        {"resource_key": "host\nkey"},
        {"resource_key": "\ud800"},
        {"endpoint": ""},
        {"endpoint": 12},
        {"user": ""},
        {"user": 12},
        {"port": "22"},
        {"port": True},
        {"port": 0},
        {"port": 65536},
        {"target_os": "windows"},
        {"target_os": None},
        {"python_path": "relative/python"},
        {"python_path": "/bad\0path"},
        {"python_path": "/bad\ud800path"},
        {"python_path": 12},
    ],
)
def test_access_rejects_unknown_malformed_and_coerced_values(change):
    with pytest.raises(ValidationError):
        LimaConfig.model_validate(
            dict(name="lima", placement=dict(mode="ssh", host="me@box", access=_access() | change))
        )


def test_access_frozen_and_local_placement_has_no_access_arm():
    access = SSHPlatformHostAccessConfig.model_validate(_access() | {"python_path": "/opt/python/bin/python3"})
    with pytest.raises(ValidationError):
        access.user = "other"
    with pytest.raises(ValidationError):
        LimaConfig.model_validate(dict(name="lima", placement=dict(mode="local", access=_access())))
    assert LimaPlatform("local-site", dict(name="lima")).is_remote is False
