"""Exercise launch redaction through the invocation's real secret boundary."""

from collections.abc import Mapping

import pytest

from agentworks.bootstrap import build_registry, load_request_registry
from agentworks.db import Database
from agentworks.env import EnvEntry, SecretEnvEntry
from agentworks.secrets import SecretTarget
from agentworks.sessions import manager


def register_workload_secret(db: Database, monkeypatch: pytest.MonkeyPatch, *, name: str, value: str) -> None:
    # Keep fake provider/transport operations while using the actual registry,
    # declaration collection, Resolver, and environment source backend.
    db._conn.execute("UPDATE vms SET site='proxmox' WHERE name='vm1'")
    db._conn.commit()
    monkeypatch.setattr("agentworks.bootstrap.build_registry", build_registry)
    monkeypatch.setattr("agentworks.bootstrap.load_request_registry", load_request_registry)
    monkeypatch.setenv("AW_SECRET_" + name.upper().replace("-", "_"), value)
    entry = EnvEntry(SecretEnvEntry(secret=name))
    monkeypatch.setattr(
        manager, "_session_secret_target", lambda *a, **k: SecretTarget(vm={}, session={"TOKEN": entry})
    )

    def compose(*args: object, values: Mapping[str, str], **kwargs: object) -> dict[str, str]:
        return {"TOKEN": values[name]}

    monkeypatch.setattr(manager, "_resolve_session_env", compose)
