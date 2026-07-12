"""System slug: settings encoding, the one-shot first-create prompt
(including non-interactive), the deferred shared-backend nudge with its
suppression flag, and the slug format bounds.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from agentworks.db import NUDGE_SUPPRESSED_KEY
from agentworks.errors import ValidationError
from agentworks.vms import manager as vm_manager
from agentworks.vms.sites import VMSiteDecl

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
    assert vm_manager._resolve_system_slug(db) == (None, False)
    assert db.get_setting("system_slug") is None


def test_first_create_prompt_fires_once_on_decline(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty answer records the declined row (empty value), so the
    prompt never fires again -- and asked_now is True exactly once so
    the caller can skip the same-create nudge."""
    monkeypatch.setattr("agentworks.output.is_interactive", lambda: True)
    prompts: list[str] = []

    def _prompt(label: str, default: str | None = None) -> str:
        prompts.append(label)
        return ""

    monkeypatch.setattr("agentworks.output.prompt", _prompt)

    assert vm_manager._resolve_system_slug(db) == (None, True)
    assert db.get_setting("system_slug") == ""
    assert len(prompts) == 1

    assert vm_manager._resolve_system_slug(db) == (None, False)
    assert len(prompts) == 1  # declined row short-circuits


def test_first_create_prompt_stores_valid_slug(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("agentworks.output.is_interactive", lambda: True)
    monkeypatch.setattr("agentworks.output.prompt", lambda label, default=None: "team-a")

    assert vm_manager._resolve_system_slug(db) == ("team-a", True)
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


def _prompt_script(answers: list[str]):
    """A prompt stub yielding scripted answers in order."""
    seen: list[str] = []

    def _prompt(label: str, default: str | None = None) -> str:
        seen.append(label)
        return answers[len(seen) - 1]

    return _prompt, seen


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
        "agentworks.output.prompt",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not nudge")),
    )
    local_lima = VMSiteDecl(name="lima", platform="lima")
    assert vm_manager._nudge_shared_backend_slug(db, local_lima) is None


def test_nudge_never_remind_me_suppresses(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("agentworks.output.is_interactive", lambda: True)
    prompt, seen = _prompt_script(["never-remind-me"])
    monkeypatch.setattr("agentworks.output.prompt", prompt)

    assert vm_manager._nudge_shared_backend_slug(db, _remote_lima_decl()) is None
    assert db.get_setting(NUDGE_SUPPRESSED_KEY) is not None
    # Suppressed: no second nudge.
    assert vm_manager._nudge_shared_backend_slug(db, _remote_lima_decl()) is None
    assert len(seen) == 1


def test_nudge_default_yes_on_enter_then_sets_the_slug(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The [Y/n/never-remind-me] shape: Enter accepts, then the
    full slug prompt runs."""
    monkeypatch.setattr("agentworks.output.is_interactive", lambda: True)
    prompt, seen = _prompt_script(["", "team-a"])
    monkeypatch.setattr("agentworks.output.prompt", prompt)

    assert vm_manager._nudge_shared_backend_slug(db, _remote_lima_decl()) == "team-a"
    assert db.get_setting("system_slug") == "team-a"
    assert "[Y/n/never-remind-me]" in seen[0]


def test_nudge_accepted_then_blank_slug_writes_nothing(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Enter at the nudge, then Enter at the full prompt: nothing is
    written (no slug, no declined row, no suppression), so both the
    first-create prompt and the nudge fire again next time."""
    monkeypatch.setattr("agentworks.output.is_interactive", lambda: True)
    prompt, _seen = _prompt_script(["", ""])
    monkeypatch.setattr("agentworks.output.prompt", prompt)

    assert vm_manager._nudge_shared_backend_slug(db, _remote_lima_decl()) is None
    assert db.get_setting("system_slug") is None
    assert db.get_setting(NUDGE_SUPPRESSED_KEY) is None


def test_nudge_no_leaves_everything_unset(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """'n' neither sets the slug nor suppresses: the nudge repeats on
    the next shared-backend create."""
    monkeypatch.setattr("agentworks.output.is_interactive", lambda: True)
    prompt, _seen = _prompt_script(["n"])
    monkeypatch.setattr("agentworks.output.prompt", prompt)

    assert vm_manager._nudge_shared_backend_slug(db, _remote_lima_decl()) is None
    assert db.get_setting("system_slug") is None
    assert db.get_setting(NUDGE_SUPPRESSED_KEY) is None
