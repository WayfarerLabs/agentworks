"""R4 system slug: settings encoding, the one-shot first-create prompt
(including non-interactive), the deferred shared-backend nudge with its
suppression flag, and the slug format bounds.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from agentworks.errors import ValidationError
from agentworks.vms import manager as vm_manager
from agentworks.vms.sites import VMSiteDecl

if TYPE_CHECKING:
    from agentworks.db import Database
    from tests.conftest import CapturedOutput


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


def test_non_interactive_never_prompts_and_never_writes(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A later interactive create must still ask, so nothing is written."""
    monkeypatch.setattr("agentworks.output.is_interactive", lambda: False)
    assert vm_manager._resolve_system_slug(db) is None
    assert db.get_setting("system_slug") is None


def test_first_create_prompt_fires_once_on_decline(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    """An empty answer records the declined row (empty value), so the
    prompt never fires again."""
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


def _remote_lima_decl() -> VMSiteDecl:
    return VMSiteDecl(
        name="gpu-box", platform="lima", platform_config={"vm_host": "me@gpu-box"}
    )


def test_nudge_skipped_non_interactive(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("agentworks.output.is_interactive", lambda: False)
    assert vm_manager._nudge_shared_backend_slug(db, _remote_lima_decl()) is None


def test_nudge_skipped_for_single_workstation_site(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("agentworks.output.is_interactive", lambda: True)
    monkeypatch.setattr(
        "agentworks.output.choose",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not nudge")),
    )
    local_lima = VMSiteDecl(name="lima", platform="lima")
    assert vm_manager._nudge_shared_backend_slug(db, local_lima) is None


def test_nudge_never_remind_me_suppresses(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("agentworks.output.is_interactive", lambda: True)
    chooses: list[str] = []

    def _choose(message: str, options: list[str]) -> int:
        chooses.append(message)
        return 2  # never remind me

    monkeypatch.setattr("agentworks.output.choose", _choose)

    assert vm_manager._nudge_shared_backend_slug(db, _remote_lima_decl()) is None
    assert db.get_setting(vm_manager.NUDGE_SUPPRESSED_KEY) is not None
    # Suppressed: no second nudge.
    assert vm_manager._nudge_shared_backend_slug(db, _remote_lima_decl()) is None
    assert len(chooses) == 1


def test_nudge_yes_sets_the_slug(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("agentworks.output.is_interactive", lambda: True)
    monkeypatch.setattr("agentworks.output.choose", lambda *a, **k: 0)
    monkeypatch.setattr("agentworks.output.prompt", lambda label, default=None: "team-a")

    assert vm_manager._nudge_shared_backend_slug(db, _remote_lima_decl()) == "team-a"
    assert db.get_setting("system_slug") == "team-a"


def test_nudge_no_leaves_everything_unset(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """'no' neither sets the slug nor suppresses: the nudge repeats on
    the next shared-backend create."""
    monkeypatch.setattr("agentworks.output.is_interactive", lambda: True)
    monkeypatch.setattr("agentworks.output.choose", lambda *a, **k: 1)

    assert vm_manager._nudge_shared_backend_slug(db, _remote_lima_decl()) is None
    assert db.get_setting("system_slug") is None
    assert db.get_setting(vm_manager.NUDGE_SUPPRESSED_KEY) is None
