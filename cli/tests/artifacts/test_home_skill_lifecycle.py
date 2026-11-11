"""Session-owned home packages retain the ordinary lifecycle and other sessions."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from agentworks.artifacts.declarations import RuleArtifactSpec
from agentworks.capabilities.harness_integration import HarnessStart, ShellIntegration
from agentworks.errors import StateError
from agentworks.harness_setup.state import read_native_setup
from agentworks.plugins.codex.artifacts import session_artifacts
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.sessions import manager
from tests.artifacts.test_session_lifecycle import lifecycle as lifecycle

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="local Linux guest file operations")


@pytest.fixture
def home_lifecycle(lifecycle, monkeypatch):
    def start(integration, *args, **kwargs):
        context = integration._session_binding.artifact_context
        plan = session_artifacts(context, extra_args=[], enabled_workarounds=["session-home-skills"])
        return HarnessStart("", artifacts=plan.application)

    monkeypatch.setattr(ShellIntegration, "start", start)
    return lifecycle


def test_restart_stages_disjoint_home_packages_before_teardown(home_lifecycle, monkeypatch):
    current = home_lifecycle
    current.restart()
    before = current.db.get_session("s1")
    old = current.files()
    assert old and all("/.agents/skills/" in str(path) for path in old)
    observed = []

    def stop(*args, **kwargs):
        files = current.files()
        assert len(files) == 2 * len(old) and all(path.exists() for path in files)
        assert set(old) < set(files)
        assert current.db.get_session("s1").run_id == before.run_id
        observed.append(True)

    monkeypatch.setattr("agentworks.sessions.manager._lifecycle._teardown_session", stop)
    current.restart()
    assert observed == [True]
    assert current.db.get_session("s1").run_id != before.run_id
    assert all(not path.exists() for path in old)
    assert current.files() and all(path.exists() for path in current.files())


def test_partial_home_publication_retains_old_runtime_and_retry_cleans_owned_prefix(home_lifecycle, monkeypatch):
    from agentworks.native_files import NativeFiles

    current = home_lifecycle
    current.config.artifact_bundles["team"] = current.config.artifact_bundles["team"].model_copy(
        update={"rules": {"policy": RuleArtifactSpec(text="fixture rule")}}
    )
    current.restart()
    old = current.files()
    before = current.db.get_session("s1")
    publish = NativeFiles.publish
    count = 0

    def fail(self, *args, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            raise StateError("fixture failed write")
        return publish(self, *args, **kwargs)

    current.events.clear()
    monkeypatch.setattr(NativeFiles, "publish", fail)
    with pytest.raises(StateError):
        current.restart()
    partial = current.files()
    assert set(old) < set(partial) and all(path.exists() for path in partial)
    assert current.db.get_session("s1").run_id == before.run_id
    assert "kill" not in current.events
    monkeypatch.setattr(NativeFiles, "publish", publish)
    current.restart()
    assert all(not path.exists() for path in partial)
    assert len(current.files()) == len(old)
    assert all(path.exists() for path in current.files())


def test_deleting_one_same_user_session_preserves_the_other_home_packages(home_lifecycle, monkeypatch):
    current = home_lifecycle
    current.restart()
    first = current.files()
    current.db.insert_agent_grant("a1", "ws1", "explicit")
    monkeypatch.setattr("agentworks.agents.manager._assert_agent_ssh_works", lambda *a, **k: None)
    monkeypatch.setattr(manager, "_teardown_session", lambda *a, **k: None)
    manager.create_session(
        current.db, current.config, name="s2", workspace="ws1", agent="a1", interaction=TtyInteractionPolicy.REFUSE
    )
    second = tuple(
        Path(file.path)
        for record in read_native_setup(current.db, "session", "s2").records
        for file in record.artifact_files
    )
    assert second and not set(first).intersection(second)
    manager.delete_session(current.db, current.config, name="s2", yes=True, interaction=TtyInteractionPolicy.REFUSE)
    assert all(not path.exists() for path in second)
    assert all(path.exists() for path in first)


def test_modified_home_package_preserves_owned_entrypoint_and_cleanup_evidence(home_lifecycle):
    from agentworks.artifacts.session import cleanup_session_artifacts

    current = home_lifecycle
    current.restart()
    paths = current.files()
    paths[0].write_bytes(b"operator modification")
    with pytest.raises(StateError):
        cleanup_session_artifacts(current.db, current.db.get_session("s1"), current.target)
    assert paths[0].read_bytes() == b"operator modification"
    record = read_native_setup(current.db, "session", "s1").records[0]
    assert str(paths[0]) in {file.path for file in record.artifact_files}
