"""VM hardening: sysctl baseline + ``/proc`` hidepid=1.

Extracted from ``vms/initializer.py`` to keep the initializer focused on
provisioning orchestration and the hardening rules self-contained
(constants + pure decision logic + apply functions). Per FRD R4a + R4b
of the direct-target-user-SSH SDD.

Public surface:
- ``apply_vm_hardening(target, logger)``: the top-level entry point
  invoked by ``_phase_b_setup``. Idempotent; non-fatal on failure
  (warns + continues to match the rest of phase B).
- ``_apply_hardening_sysctl`` / ``_apply_hardening_fstab``: each
  hardening step in isolation, kept exposed (with an underscore prefix)
  so unit tests can exercise them without standing up the full phase.
- ``_ensure_proc_hidepid_in_fstab``: the pure parse-and-edit core of
  the fstab decision; no I/O, exhaustively unit-tested.
"""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.ssh import SSHError

if TYPE_CHECKING:
    from agentworks.ssh import SSHLogger
    from agentworks.transports import Transport


HARDENING_SYSCTL_PATH = "/etc/sysctl.d/99-agentworks.conf"
HARDENING_SYSCTL_CONTENT = """\
# Managed by agentworks. Do not edit; this file is rewritten on vm reinit.
# Source: docs/sdd/2026-06-06-direct-user-ssh-access/ R4b.
kernel.dmesg_restrict = 1
kernel.kptr_restrict = 1
kernel.yama.ptrace_scope = 1
fs.protected_hardlinks = 1
fs.protected_symlinks = 1
fs.protected_fifos = 2
fs.protected_regular = 2
kernel.unprivileged_bpf_disabled = 1
"""

HARDENING_FSTAB_PATH = "/etc/fstab"
HARDENING_FSTAB_COMMENT = "# hidepid managed by agentworks"
# Used only when appending a brand-new /proc line (no existing entry).
HARDENING_FSTAB_NEW_LINE = (
    f"proc  /proc  proc  defaults,hidepid=1  0  0  {HARDENING_FSTAB_COMMENT}"
)


def _split_fstab_line(line: str) -> tuple[str, str]:
    """Split a non-empty fstab line into (code_portion, trailing_comment).

    The trailing comment starts at the first '#' (fstab options don't
    contain '#'). The code portion has trailing whitespace stripped.
    """
    idx = line.find("#")
    if idx == -1:
        return line.rstrip(), ""
    return line[:idx].rstrip(), line[idx:]


def _ensure_proc_hidepid_in_fstab(content: str) -> tuple[str, str, int]:
    """Pure: edit fstab content so /proc is mounted with hidepid>=1.

    Returns ``(new_content, action, effective_hidepid)``.

    Actions:
    - ``"no-op"``: existing /proc has hidepid=1 already; content unchanged.
    - ``"appended"``: no /proc line in fstab; appended a new one.
    - ``"added-option"``: existing /proc line; appended hidepid=1 to options.
    - ``"upgraded"``: existing /proc line had hidepid=0; upgraded to 1.
    - ``"preserved-stricter"``: existing /proc has hidepid>=2; content unchanged.
    - ``"malformed"``: found a /proc line that doesn't split cleanly into the
      six expected fstab fields; content unchanged, caller should warn.

    ``effective_hidepid`` is the value /proc should be mounted with after
    this runs (used by the live remount). For ``preserved-stricter`` this is
    the existing stricter value; otherwise 1.
    """
    lines = content.splitlines()
    proc_line_indices: list[int] = []

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        fields = stripped.split()
        # fstab fields: <device> <mount-point> <fstype> <options> <dump> <pass>
        if len(fields) >= 3 and fields[2] == "proc":
            proc_line_indices.append(i)

    if not proc_line_indices:
        lines.append(HARDENING_FSTAB_NEW_LINE)
        return "\n".join(lines) + "\n", "appended", 1

    proc_line_idx = proc_line_indices[0]
    existing_line = lines[proc_line_idx]
    code, trailing_comment = _split_fstab_line(existing_line)
    fields = code.split()
    if len(fields) < 6:
        return content, "malformed", 1

    options = fields[3].split(",")
    hidepid_value: int | None = None
    hidepid_opt_idx: int | None = None
    for j, opt in enumerate(options):
        if opt.startswith("hidepid="):
            try:
                hidepid_value = int(opt.split("=", 1)[1])
                hidepid_opt_idx = j
                break
            except ValueError:
                continue

    if hidepid_value is None:
        options.append("hidepid=1")
        action = "added-option"
        effective = 1
    elif hidepid_value == 0:
        assert hidepid_opt_idx is not None
        options[hidepid_opt_idx] = "hidepid=1"
        action = "upgraded"
        effective = 1
    elif hidepid_value == 1:
        return content, "no-op", 1
    else:
        # hidepid >= 2: stricter than agentworks's default; respect it.
        return content, "preserved-stricter", hidepid_value

    # Rebuild the line: keep the existing trailing comment if any, otherwise
    # add our informational marker.
    new_fields = [fields[0], fields[1], fields[2], ",".join(options), fields[4], fields[5]]
    new_code = "  ".join(new_fields)
    new_line = (
        f"{new_code}  {trailing_comment}"
        if trailing_comment
        else f"{new_code}  {HARDENING_FSTAB_COMMENT}"
    )
    lines[proc_line_idx] = new_line
    return "\n".join(lines) + "\n", action, effective


def apply_vm_hardening(target: Transport, logger: SSHLogger) -> None:
    """Apply VM hardening: sysctl baseline + /proc hidepid=1.

    Idempotent: a second run is a no-op unless the on-disk content differs.
    Called at vm create and re-applied at vm reinit. Non-fatal: failures
    warn and continue (matches the rest of _phase_b_setup).
    """
    logger.step("VM hardening")
    try:
        _apply_hardening_sysctl(target, logger)
    except SSHError as e:
        msg = f"sysctl baseline failed: {e}"
        logger.warning(msg)
        output.warn(msg)
    try:
        _apply_hardening_fstab(target, logger)
    except SSHError as e:
        msg = f"fstab hidepid edit failed: {e}"
        logger.warning(msg)
        output.warn(msg)


def _apply_hardening_sysctl(target: Transport, logger: SSHLogger) -> None:
    """Write the sysctl baseline if content differs from desired; reload if changed."""
    # sudo on the read for consistency with the fstab read below; the file
    # itself is mode 0644 and would be world-readable when present.
    existing = target.run(f"cat {HARDENING_SYSCTL_PATH}", sudo=True, check=False)
    if (
        getattr(existing, "ok", False)
        and getattr(existing, "stdout", "") == HARDENING_SYSCTL_CONTENT
    ):
        output.detail("Sysctl baseline already applied; no change.")
        return

    output.detail("Applying sysctl baseline...")
    mktemp_result = target.run("mktemp --tmpdir agw-sysctl.XXXXXX")
    staging = (getattr(mktemp_result, "stdout", "") or "").strip()
    if not staging:
        raise SSHError("mktemp produced empty path for sysctl staging")
    try:
        target.write_file(staging, HARDENING_SYSCTL_CONTENT)
        target.run(
            f"install -m 0644 -o root -g root {shlex.quote(staging)} {HARDENING_SYSCTL_PATH}",
            sudo=True,
        )
    finally:
        target.run(f"rm -f {shlex.quote(staging)}", check=False)
    target.run("sysctl --system", sudo=True)


def _apply_hardening_fstab(target: Transport, logger: SSHLogger) -> None:
    """Ensure /proc is mounted with ``hidepid>=1`` and live-remount.

    Parses the existing ``/etc/fstab`` /proc line (if any) and edits its
    options conservatively rather than inserting a parallel managed line.
    Preserves admin-set ``hidepid=2`` (stricter than agentworks's default).
    Always live-remounts /proc to the effective value at the end.
    """
    output.detail("Ensuring hidepid=1 on /proc...")

    result = target.run(f"cat {HARDENING_FSTAB_PATH}", sudo=True, check=False)
    if not getattr(result, "ok", False):
        msg = f"could not read {HARDENING_FSTAB_PATH}; skipping hidepid edit"
        logger.warning(msg)
        output.warn(msg)
        return
    current = getattr(result, "stdout", "") or ""

    new_content, action, effective = _ensure_proc_hidepid_in_fstab(current)

    if action == "malformed":
        msg = f"{HARDENING_FSTAB_PATH} /proc line did not parse cleanly; leaving fstab unchanged. Will still remount."
        logger.warning(msg)
        output.warn(msg)
    elif action == "preserved-stricter":
        output.detail(
            f"/proc already mounted with hidepid={effective} (stricter than agentworks default); preserved."
        )
    elif action == "no-op":
        # Already exactly what we want; nothing to log.
        pass
    else:
        # action in {"appended", "added-option", "upgraded"}: write the file.
        action_msg = {
            "appended": "Added /proc entry to /etc/fstab.",
            "added-option": "Added hidepid=1 to /proc options in /etc/fstab.",
            "upgraded": "Upgraded /proc from hidepid=0 to hidepid=1 in /etc/fstab.",
        }[action]
        output.detail(action_msg)
        mktemp_result = target.run("mktemp --tmpdir agw-fstab.XXXXXX")
        staging = (getattr(mktemp_result, "stdout", "") or "").strip()
        if not staging:
            raise SSHError("mktemp produced empty path for fstab staging")
        try:
            target.write_file(staging, new_content)
            target.run(
                f"install -m 0644 -o root -g root {shlex.quote(staging)} {HARDENING_FSTAB_PATH}",
                sudo=True,
            )
        finally:
            target.run(f"rm -f {shlex.quote(staging)}", check=False)

    # Live remount with the effective value (idempotent at the kernel level
    # when options match the existing mount; cheap to call unconditionally).
    target.run(f"mount -o remount,hidepid={effective} /proc", sudo=True)
