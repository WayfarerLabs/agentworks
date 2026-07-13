"""System slug: settings encoding, the one-shot first-create prompt
(including non-interactive), and the slug format bounds. A blank answer
is a VALID answer -- it records the declined row and the prompt never
fires again (the former shared-backend nudge that re-asked decliners
was removed by maintainer ruling).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from agentworks.errors import ValidationError
from agentworks.vms import manager as vm_manager

if TYPE_CHECKING:
    from agentworks.db import Database


def test_settings_encoding_absent_empty_value(db: Database) -> None:
    """None = never written; empty string = written-but-declined."""
    assert db.get_setting("system_slug") is None
    db.set_setting("system_slug", "")
    assert db.get_setting("system_slug") == ""
    db.set_setting("system_slug", "team-a")
    assert db.get_setting("system_slug") == "team-a"


def test_validate_slug_bounds() -> None:
    vm_manager.validate_slug("abc")
    vm_manager.validate_slug("a" * 20)
    vm_manager.validate_slug("team-a1")
    for bad in ("ab", "a" * 21, "-abc", "abc-", "Team", "a_b_c", ""):
        with pytest.raises(ValidationError, match="invalid system slug"):
            vm_manager.validate_slug(bad)


def test_slug_prompt_matches_the_frd_text() -> None:
    """Pin the operator-facing prompt wording (drift is user-visible)."""
    assert vm_manager._SLUG_PROMPT == (
        "A system slug uniquely identifies this agentworks installation. "
        "It is used to namespace VMs and other resources so this install "
        "does not collide with others that share the same cloud account, "
        "Proxmox cluster, or Windows/Mac user. Leave blank if this "
        "install is the only one using its sites' backends. [system slug]"
    )


def test_non_interactive_never_prompts_and_never_writes(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A later interactive create must still ask, so nothing is written."""
    monkeypatch.setattr("agentworks.output.is_interactive", lambda: False)
    assert vm_manager._resolve_system_slug(db) is None
    assert db.get_setting("system_slug") is None


def test_blank_answer_is_final(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty answer is a valid one ("no slug"): it records the
    declined row, so the prompt never fires again -- not on the next
    create, not via any nudge."""
    monkeypatch.setattr("agentworks.output.is_interactive", lambda: True)
    prompts: list[str] = []

    def _prompt(label: str, default: str | None = None) -> str:
        prompts.append(label)
        return ""

    monkeypatch.setattr("agentworks.output.prompt", _prompt)

    assert vm_manager._resolve_system_slug(db) is None
    assert db.get_setting("system_slug") == ""
    assert len(prompts) == 1

    assert vm_manager._resolve_system_slug(db) is None
    assert len(prompts) == 1  # declined row short-circuits


def test_first_create_prompt_stores_valid_slug(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("agentworks.output.is_interactive", lambda: True)
    monkeypatch.setattr("agentworks.output.prompt", lambda label, default=None: "team-a")

    assert vm_manager._resolve_system_slug(db) == "team-a"
    assert db.get_setting("system_slug") == "team-a"


def test_invalid_answer_aborts_without_writing(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invalid input aborts the create before any state mutation; the
    next create asks again."""
    monkeypatch.setattr("agentworks.output.is_interactive", lambda: True)
    monkeypatch.setattr("agentworks.output.prompt", lambda label, default=None: "-bad-")

    with pytest.raises(ValidationError):
        vm_manager._resolve_system_slug(db)
    assert db.get_setting("system_slug") is None


def test_no_prompt_of_any_kind_after_decline(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression this file exists to prevent: a declined install
    must see NO slug prompt of any kind on later creates (the nudge
    call site is gone from create_vm entirely; _resolve_system_slug is
    the only remaining asker, and the declined row short-circuits it
    before any prompt call)."""
    db.set_setting("system_slug", "")  # declined on an earlier create
    monkeypatch.setattr("agentworks.output.is_interactive", lambda: True)
    monkeypatch.setattr(
        "agentworks.output.prompt",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("prompted after a declined slug")
        ),
    )
    assert vm_manager._resolve_system_slug(db) is None
