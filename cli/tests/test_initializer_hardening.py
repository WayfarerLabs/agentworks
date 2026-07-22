"""Tests for VM hardening applied by the initializer.

Split out of ``test_initializer.py`` (see ``_initializer_support.py`` for
the shared ``Transport`` builder). Covers the sysctl baseline, the fstab
``hidepid`` reconcile (both the ``Transport``-driven wrapper and the pure
line-editor function), and the section role/level shape those two steps
render with.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from ._initializer_support import _make_hardening_target


def test_sysctl_baseline_writes_when_missing() -> None:
    from agentworks.vms.hardening import (
        HARDENING_SYSCTL_CONTENT,
        HARDENING_SYSCTL_PATH,
        _apply_hardening_sysctl,
    )

    target = _make_hardening_target(sysctl_content=None)
    logger = MagicMock()

    _apply_hardening_sysctl(target, logger)

    # write_file was called with the canonical content
    assert any(content == HARDENING_SYSCTL_CONTENT for _, content in target.write_log)
    # install -m 0644 and sysctl --system were run
    assert any("install -m 0644" in cmd and HARDENING_SYSCTL_PATH in cmd for cmd in target.run_log)
    assert any(cmd == "sysctl --system" for cmd in target.run_log)


def test_sysctl_baseline_noop_when_content_matches() -> None:
    from agentworks.vms.hardening import (
        HARDENING_SYSCTL_CONTENT,
        HARDENING_SYSCTL_PATH,
        _apply_hardening_sysctl,
    )

    target = _make_hardening_target(sysctl_content=HARDENING_SYSCTL_CONTENT)
    logger = MagicMock()

    _apply_hardening_sysctl(target, logger)

    # No write, no install, no sysctl reload
    assert target.write_log == []
    assert not any("install -m 0644" in cmd and HARDENING_SYSCTL_PATH in cmd for cmd in target.run_log)
    assert not any(cmd == "sysctl --system" for cmd in target.run_log)


def test_sysctl_baseline_rewrites_when_content_differs() -> None:
    from agentworks.vms.hardening import (
        HARDENING_SYSCTL_CONTENT,
        HARDENING_SYSCTL_PATH,
        _apply_hardening_sysctl,
    )

    target = _make_hardening_target(sysctl_content="# old content\n")
    logger = MagicMock()

    _apply_hardening_sysctl(target, logger)

    # Writes the canonical content and reloads
    assert any(content == HARDENING_SYSCTL_CONTENT for _, content in target.write_log)
    assert any("install -m 0644" in cmd and HARDENING_SYSCTL_PATH in cmd for cmd in target.run_log)
    assert any(cmd == "sysctl --system" for cmd in target.run_log)


def test_fstab_hidepid_appends_when_no_proc_line() -> None:
    """No /proc line in fstab: append one with defaults,hidepid=1."""
    from agentworks.vms.hardening import (
        HARDENING_FSTAB_PATH,
        _apply_hardening_fstab,
    )

    fstab_in = "# fstab\nUUID=root  /  ext4  defaults  0  1\n"
    target = _make_hardening_target(fstab_content=fstab_in)
    logger = MagicMock()

    _apply_hardening_fstab(target, logger)

    assert len(target.write_log) == 1
    _, new_content = target.write_log[0]
    # New /proc line ends up in the file with hidepid=1.
    assert "proc" in new_content and "hidepid=1" in new_content
    # Original lines preserved.
    assert "UUID=root  /  ext4  defaults  0  1" in new_content
    assert any("install -m 0644" in cmd and HARDENING_FSTAB_PATH in cmd for cmd in target.run_log)
    assert any(cmd == "mount -o remount,hidepid=1 /proc" for cmd in target.run_log)


def test_fstab_hidepid_adds_option_when_proc_line_has_no_hidepid() -> None:
    """Existing /proc line with no hidepid= : append hidepid=1 to options."""
    from agentworks.vms.hardening import (
        HARDENING_FSTAB_PATH,
        _apply_hardening_fstab,
    )

    fstab_in = "# fstab\nproc  /proc  proc  defaults  0  0\n"
    target = _make_hardening_target(fstab_content=fstab_in)
    logger = MagicMock()

    _apply_hardening_fstab(target, logger)

    assert len(target.write_log) == 1
    _, new_content = target.write_log[0]
    # The single proc line now has both defaults AND hidepid=1; not parallel lines.
    proc_lines = [ln for ln in new_content.splitlines() if ln.split()[:1] == ["proc"]]
    assert len(proc_lines) == 1
    assert "defaults" in proc_lines[0]
    assert "hidepid=1" in proc_lines[0]
    assert any("install -m 0644" in cmd and HARDENING_FSTAB_PATH in cmd for cmd in target.run_log)
    assert any(cmd == "mount -o remount,hidepid=1 /proc" for cmd in target.run_log)


def test_fstab_hidepid_upgrades_0_to_1() -> None:
    """Existing /proc line with hidepid=0: upgrade to hidepid=1."""
    from agentworks.vms.hardening import (
        HARDENING_FSTAB_PATH,
        _apply_hardening_fstab,
    )

    fstab_in = "proc  /proc  proc  defaults,hidepid=0  0  0\n"
    target = _make_hardening_target(fstab_content=fstab_in)
    logger = MagicMock()

    _apply_hardening_fstab(target, logger)

    assert len(target.write_log) == 1
    _, new_content = target.write_log[0]
    assert "hidepid=1" in new_content
    assert "hidepid=0" not in new_content
    assert any("install -m 0644" in cmd and HARDENING_FSTAB_PATH in cmd for cmd in target.run_log)


def test_fstab_hidepid_noop_when_already_1() -> None:
    """Existing /proc line with hidepid=1: no fstab rewrite, but still remount."""
    from agentworks.vms.hardening import (
        HARDENING_FSTAB_PATH,
        _apply_hardening_fstab,
    )

    fstab_in = "proc  /proc  proc  defaults,hidepid=1  0  0\n"
    target = _make_hardening_target(fstab_content=fstab_in)
    logger = MagicMock()

    _apply_hardening_fstab(target, logger)

    assert target.write_log == []
    assert not any("install -m 0644" in cmd and HARDENING_FSTAB_PATH in cmd for cmd in target.run_log)
    assert any(cmd == "mount -o remount,hidepid=1 /proc" for cmd in target.run_log)


def test_fstab_hidepid_preserves_admin_set_hidepid_2() -> None:
    """Existing /proc line with hidepid=2 (stricter): no fstab edit, remount uses 2."""
    from agentworks.vms.hardening import (
        HARDENING_FSTAB_PATH,
        _apply_hardening_fstab,
    )

    fstab_in = "proc  /proc  proc  defaults,hidepid=2  0  0\n"
    target = _make_hardening_target(fstab_content=fstab_in)
    logger = MagicMock()

    _apply_hardening_fstab(target, logger)

    assert target.write_log == []
    assert not any("install -m 0644" in cmd and HARDENING_FSTAB_PATH in cmd for cmd in target.run_log)
    # Critical: live remount uses hidepid=2 (admin's stricter choice), not 1.
    assert any(cmd == "mount -o remount,hidepid=2 /proc" for cmd in target.run_log)


# -- Section role/level shape (regression guard) -------------------------------


def test_vm_initialization_step_and_subresult_roles(captured_output) -> None:  # noqa: ANN001
    """Pin the fixed indentation shape for a VM Initialization section.

    Regression guard for the over-indent bug: inside a ``section("VM
    Initialization")`` (body at level 1), a primary step must carry the
    BODY role so the handler renders it at the section level (2 spaces),
    while a genuine sub-result subordinate to that step stays DETAIL so it
    nests one notch deeper (4 spaces). ``apply_vm_hardening`` exercises
    both in one section: "Applying sysctl baseline..." and "Ensuring
    hidepid=1 on /proc..." are steps, and "Added /proc entry to
    /etc/fstab" is the fstab step's sub-result.

    The captured level is the ambient section level (1) for both roles;
    the step-vs-subresult distinction rides the ROLE (the handler maps
    DETAIL to level + 1 at render time), which is exactly what the fix
    restored.
    """
    from agentworks import output
    from agentworks.output import Role
    from agentworks.vms.hardening import apply_vm_hardening

    # sysctl file missing -> "Applying sysctl baseline..."; fstab has no
    # /proc line -> "Ensuring hidepid=1 on /proc..." + "Added /proc entry
    # to /etc/fstab" sub-result.
    target = _make_hardening_target(
        sysctl_content=None,
        fstab_content="# fstab\nUUID=root  /  ext4  defaults  0  1\n",
    )
    logger = MagicMock()

    with output.section("VM Initialization"):
        apply_vm_hardening(target, logger)

    # Primary steps: BODY at the section level (renders at 2 spaces).
    assert (Role.BODY, 1, "Applying sysctl baseline...") in captured_output.lines
    assert (Role.BODY, 1, "Ensuring hidepid=1 on /proc...") in captured_output.lines
    # Genuine sub-result: DETAIL, so the handler nests it one notch deeper
    # (4 spaces) under the step above.
    assert (Role.DETAIL, 1, "Added /proc entry to /etc/fstab") in captured_output.lines
    # The step lines are NOT dimmed to DETAIL (the bug this guards against).
    assert not any(
        role is Role.DETAIL and msg == "Ensuring hidepid=1 on /proc..." for role, _level, msg in captured_output.lines
    )


# -- Pure function tests for the fstab editor ----------------------------------


def test_ensure_proc_hidepid_appends_when_no_proc_line() -> None:
    from agentworks.vms.hardening import _ensure_proc_hidepid_in_fstab

    content = "UUID=root  /  ext4  defaults  0  1\n"
    new, action, eff = _ensure_proc_hidepid_in_fstab(content)
    assert action == "appended"
    assert eff == 1
    assert "proc" in new and "hidepid=1" in new


def test_ensure_proc_hidepid_added_option() -> None:
    from agentworks.vms.hardening import _ensure_proc_hidepid_in_fstab

    content = "proc  /proc  proc  defaults  0  0\n"
    new, action, eff = _ensure_proc_hidepid_in_fstab(content)
    assert action == "added-option"
    assert eff == 1
    assert "hidepid=1" in new


def test_ensure_proc_hidepid_upgraded() -> None:
    from agentworks.vms.hardening import _ensure_proc_hidepid_in_fstab

    content = "proc  /proc  proc  defaults,hidepid=0  0  0\n"
    new, action, eff = _ensure_proc_hidepid_in_fstab(content)
    assert action == "upgraded"
    assert eff == 1
    assert "hidepid=1" in new
    assert "hidepid=0" not in new


def test_ensure_proc_hidepid_no_op() -> None:
    from agentworks.vms.hardening import _ensure_proc_hidepid_in_fstab

    content = "proc  /proc  proc  defaults,hidepid=1  0  0\n"
    new, action, eff = _ensure_proc_hidepid_in_fstab(content)
    assert action == "no-op"
    assert eff == 1
    assert new == content


def test_ensure_proc_hidepid_preserves_stricter() -> None:
    from agentworks.vms.hardening import _ensure_proc_hidepid_in_fstab

    content = "proc  /proc  proc  defaults,hidepid=2  0  0\n"
    new, action, eff = _ensure_proc_hidepid_in_fstab(content)
    assert action == "preserved-stricter"
    assert eff == 2
    assert new == content


def test_ensure_proc_hidepid_preserves_trailing_comment() -> None:
    """An existing trailing comment on the /proc line is preserved on edit."""
    from agentworks.vms.hardening import _ensure_proc_hidepid_in_fstab

    content = "proc  /proc  proc  defaults  0  0  # custom proc note\n"
    new, action, _ = _ensure_proc_hidepid_in_fstab(content)
    assert action == "added-option"
    assert "# custom proc note" in new


def test_ensure_proc_hidepid_malformed() -> None:
    """A /proc line without 6 fields is left untouched (caller warns)."""
    from agentworks.vms.hardening import _ensure_proc_hidepid_in_fstab

    content = "proc  /proc  proc\n"
    new, action, eff = _ensure_proc_hidepid_in_fstab(content)
    assert action == "malformed"
    assert new == content
    assert eff == 1
