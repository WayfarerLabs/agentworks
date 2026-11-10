"""A stored overlay with a source reference this release refuses: value-free, superseded by explicit replacement."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from agentworks.db import DesiredOverlayRecord, VersionedPayload
from agentworks.doctor import InstanceStateHealthFactType, Status
from agentworks.doctor_state import check_database
from agentworks.instance_specs import (
    VM_ADMIN_REPLACEMENT_REMEDY,
    ExplicitReplacementRequired,
    OverlayDisposition,
    StoredSourceReferenceRefused,
    decode_stored_overlay,
    parse_instance_spec,
    replace_agent_overlay,
)

if TYPE_CHECKING:
    from agentworks.db import Database
    from agentworks.db.instance_state import JsonObject

SECRET = "ghp-stored-secret"

REFUSED: list[JsonObject] = [
    {"dotfiles_source": f"git::https://u:{SECRET}@example.com/dots.git"},
    {"dotfiles_source": f"https://u:{SECRET}@example.com/dots.git"},
    {"dotfiles_source": f"user:{SECRET}@example.com:me/dots.git"},
    {"mise_lockfile": f"git::https://{SECRET}@example.com/locks.git//mise.lock"},
]
IDS = ["credentialed-git", "bare-url", "bare-scp", "lockfile"]


def _stored_agent(db: Database, value: JsonObject) -> VersionedPayload:
    db.insert_vm("vm", site="local", hostname="vm")
    db.insert_agent("a1", "vm", "agt-a1")
    stored = VersionedPayload(1, value)
    db.instance_state.put_desired_overlay("agent", "a1", stored)
    return stored


@pytest.mark.parametrize("value", REFUSED, ids=IDS)
def test_a_refused_stored_source_asks_for_an_explicit_replacement_and_names_only_the_field(
    value: JsonObject,
) -> None:
    record = DesiredOverlayRecord("agent", "a1", VersionedPayload(1, value), "2026-11-10")

    with pytest.raises(StoredSourceReferenceRefused) as refused:
        decode_stored_overlay(record)

    assert isinstance(refused.value, ExplicitReplacementRequired)
    assert next(iter(value)) in str(refused.value)
    assert SECRET not in f"{refused.value} {refused.value.hint}"


@pytest.mark.parametrize("value", REFUSED, ids=IDS)
def test_agent_reinit_clears_or_replaces_a_refused_stored_source_but_a_retain_refuses(
    db: Database, value: JsonObject
) -> None:
    stored = _stored_agent(db, value)

    with pytest.raises(StoredSourceReferenceRefused):
        replace_agent_overlay(db, "a1", None, supplied=False)
    assert db.instance_state.get_desired_overlay("agent", "a1").payload == stored  # type: ignore[union-attr]

    replacement = parse_instance_spec("agent", '{"dotfiles_source":"git::https://example.com/dots.git"}')
    replaced = replace_agent_overlay(db, "a1", replacement, supplied=True)
    assert replaced is not None and replaced.disposition is OverlayDisposition.REPLACED
    assert db.instance_state.get_desired_overlay("agent", "a1").payload == replacement.payload  # type: ignore[union-attr]

    db.instance_state.put_desired_overlay("agent", "a1", stored)
    cleared = replace_agent_overlay(db, "a1", parse_instance_spec("agent", "{}"), supplied=True)
    assert cleared is not None and cleared.disposition is OverlayDisposition.CLEARED
    assert db.instance_state.get_desired_overlay("agent", "a1") is None


def test_a_vm_s_refused_stored_admin_source_gets_the_vm_hint() -> None:
    payload = VersionedPayload(2, {"vm": {}, "admin": REFUSED[0]})

    with pytest.raises(StoredSourceReferenceRefused) as refused:
        decode_stored_overlay(DesiredOverlayRecord("vm", "box", payload, "2026-11-10"))

    assert refused.value.hint == VM_ADMIN_REPLACEMENT_REMEDY
    assert "admin.dotfiles_source" in str(refused.value)
    assert SECRET not in str(refused.value)


def test_doctor_reports_a_refused_stored_source_value_free_with_its_repair(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("agentworks.db.DB_PATH", Path(db._conn.execute("PRAGMA database_list").fetchone()[2]))
    _stored_agent(db, REFUSED[1])

    checks = [
        check
        for check in check_database(None).checks
        if check.instance_state is not None
        and check.instance_state.fact_type is InstanceStateHealthFactType.MIGRATION_PENDING
    ]

    (check,) = checks
    record = db.instance_state.get_desired_overlay("agent", "a1")
    assert record is not None
    with pytest.raises(StoredSourceReferenceRefused) as refused:
        decode_stored_overlay(record)
    assert check.status is Status.WARN
    assert check.hint == refused.value.hint
    assert SECRET not in str(check_database(None).checks)


def test_agent_show_reports_a_refused_stored_source_value_free(
    db: Database,
    make_config,  # noqa: ANN001
) -> None:
    from agentworks.agents.manager import agent_description
    from agentworks.agents.manager.inspect import agent_description_data

    _stored_agent(db, REFUSED[0])

    description = agent_description(db, make_config(), name="a1")

    assert SECRET not in str(agent_description_data(description))
