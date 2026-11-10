"""VM hardening: sysctl baseline + ``/proc`` hidepid=1.

Extracted from ``vms/initializer.py`` to keep the initializer focused on
provisioning orchestration and the hardening rules self-contained
(constants + pure decision logic + apply functions).

Public surface:
- ``apply_vm_hardening(files, log)``: the top-level entry point
  invoked by ``_phase_b_setup``. Idempotent; non-fatal on failure
  (warns + continues to match the rest of phase B).
- ``_apply_hardening_sysctl`` / ``_apply_hardening_fstab``: each
  hardening step in isolation, kept exposed (with an underscore prefix)
  so unit tests can exercise them without standing up the full phase.
- ``_ensure_proc_hidepid_in_fstab``: the pure parse-and-edit core of
  the fstab decision; no I/O, exhaustively unit-tested.

Both files are read through the file program and published with
``Files.update``, so an unchanged file is never rewritten and a file
another writer changed since the read is left as it is.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks import output
from agentworks.execution.requests import Command
from agentworks.vms.initializer.steps import STEP_FAILURES, update_and_apply, warn, warn_step

if TYPE_CHECKING:
    from agentworks.execution.files import Files
    from agentworks.vms.initializer.steps import InitLog


HARDENING_SYSCTL_PATH = "/etc/sysctl.d/99-agentworks.conf"
HARDENING_SYSCTL_CONTENT = """\
# Managed by agentworks. Do not edit; this file is rewritten on vm reinit.
# Defense-in-depth baseline for shared multi-user VMs: restrict kernel log
# and pointer exposure, confine ptrace to descendants, enable the protected
# symlink/hardlink/fifo/regular-file guards against symlink and fifo attacks
# in world-writable dirs, and disable unprivileged BPF.
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
HARDENING_FSTAB_NEW_LINE = f"proc  /proc  proc  defaults,hidepid=1  0  0  {HARDENING_FSTAB_COMMENT}"


def _split_fstab_line(line: str) -> tuple[str, str]:
    """Split a non-empty fstab line into (code_portion, trailing_comment).

    The trailing comment starts at the first '#' (fstab options don't
    contain '#'). The code portion has trailing whitespace stripped.
    """
    idx = line.find("#")
    if idx == -1:
        return line.rstrip(), ""
    return line[:idx].rstrip(), line[idx:]


_HIDEPID_LEVELS = {
    "0": 0,
    "1": 1,
    "2": 2,
    "4": 4,
    "off": 0,
    "noaccess": 1,
    "invisible": 2,
    "ptraceable": 4,
}
"""The kernel's canonical spellings of ``hidepid`` (``proc(5)``), by number and by name, with each level."""


def _ensure_proc_hidepid_in_fstab(content: str) -> tuple[str, str, str]:
    """Pure: edit fstab content so /proc is mounted with hidepid>=1.

    Returns ``(new_content, action, effective_hidepid)``.

    Actions:
    - ``"no-op"``: existing /proc has hidepid=1 (or ``noaccess``) already;
      content unchanged.
    - ``"appended"``: no /proc line in fstab; appended a new one.
    - ``"added-option"``: existing /proc line; appended hidepid=1 to options.
    - ``"upgraded"``: existing /proc line had hidepid=0 (or ``off``);
      upgraded to 1.
    - ``"preserved-stricter"``: existing /proc has a stricter hidepid;
      content unchanged.
    - ``"preserved-unrecognized"``: existing /proc has a ``hidepid=`` value
      this code does not recognize; content unchanged, caller reports it.
    - ``"malformed"``: found a /proc line that doesn't split cleanly into the
      six expected fstab fields; content unchanged, caller should warn.

    ``effective_hidepid`` is the value /proc should be mounted with after
    this runs (used by the live remount): the existing value as written when
    it is kept, otherwise ``"1"``.
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
        return "\n".join(lines) + "\n", "appended", "1"

    proc_line_idx = proc_line_indices[0]
    existing_line = lines[proc_line_idx]
    code, trailing_comment = _split_fstab_line(existing_line)
    fields = code.split()
    if len(fields) < 6:
        return content, "malformed", "1"

    options = fields[3].split(",")
    # The last occurrence is the one mount applies.
    hidepid_opt_idx = next((j for j in reversed(range(len(options))) if options[j].startswith("hidepid=")), None)

    if hidepid_opt_idx is None:
        options.append("hidepid=1")
        action = "added-option"
    else:
        value = options[hidepid_opt_idx].split("=", 1)[1]
        level = _HIDEPID_LEVELS.get(value)
        if level is None:
            return content, "preserved-unrecognized", value
        if level == 1:
            return content, "no-op", value
        if level > 1:
            # Stricter than agentworks's default; respect it.
            return content, "preserved-stricter", value
        options[hidepid_opt_idx] = "hidepid=1"
        action = "upgraded"

    # Rebuild the line: keep the existing trailing comment if any, otherwise
    # add our informational marker.
    new_fields = [fields[0], fields[1], fields[2], ",".join(options), fields[4], fields[5]]
    new_code = "  ".join(new_fields)
    new_line = f"{new_code}  {trailing_comment}" if trailing_comment else f"{new_code}  {HARDENING_FSTAB_COMMENT}"
    lines[proc_line_idx] = new_line
    return "\n".join(lines) + "\n", action, "1"


def apply_vm_hardening(files: Files, log: InitLog) -> None:
    """Apply VM hardening: sysctl baseline + /proc hidepid=1.

    Idempotent: a second run is a no-op unless the on-disk content differs.
    Called at vm create and re-applied at vm reinit. Non-fatal: failures
    warn and continue (matches the rest of _phase_b_setup).
    """
    log.step("VM hardening")
    try:
        _apply_hardening_sysctl(files, log)
    except STEP_FAILURES as error:
        warn_step(log, "sysctl baseline failed", error)
    try:
        _apply_hardening_fstab(files, log)
    except STEP_FAILURES as error:
        warn_step(log, "fstab hidepid edit failed", error)


def _apply_hardening_sysctl(files: Files, log: InitLog) -> None:
    """Write the sysctl baseline if content differs from desired, then load that file with ``sysctl -p``.

    Loading only this file means another package's ``sysctl.d`` entry cannot
    fail the load. A load that fails puts the prior baseline back, so the next
    run retries. Every key is present on the kernels Agentworks supports
    (Debian's, WSL2's, which builds in Yama and the BPF syscall); a kernel
    lacking one fails the load, which warns on each reinit rather than
    dropping the key silently.
    """
    current = files.read(HARDENING_SYSCTL_PATH, elevated=True)
    desired = HARDENING_SYSCTL_CONTENT.encode()
    if current == desired:
        output.info("Sysctl baseline already applied; no change.")
        return
    output.info("Applying sysctl baseline...")
    update_and_apply(
        files,
        log,
        HARDENING_SYSCTL_PATH,
        desired,
        observed=current,
        mode=0o644,
        parent_mode=0o755,
        apply=lambda: files.access.run(
            Command(("sysctl", "-p", HARDENING_SYSCTL_PATH), elevated=True, deadline=files.deadline), check=True
        ),
    )


def _apply_hardening_fstab(files: Files, log: InitLog) -> None:
    """Ensure /proc is mounted with ``hidepid>=1`` and live-remount.

    Parses the existing ``/etc/fstab`` /proc line (if any) and edits its
    options conservatively rather than inserting a parallel managed line.
    Preserves admin-set ``hidepid=2`` (stricter than agentworks's default).
    An edit keeps the file's owner, mode and attributes. Always
    live-remounts /proc to the effective value at the end.
    """
    output.info("Ensuring hidepid=1 on /proc...")

    current = files.read(HARDENING_FSTAB_PATH, elevated=True)
    if current is None:
        msg = f"could not read {HARDENING_FSTAB_PATH}; skipping hidepid edit"
        warn(log, msg)
        return

    # surrogateescape keeps any byte that is not UTF-8 exactly as it was.
    new_content, action, effective = _ensure_proc_hidepid_in_fstab(current.decode("utf-8", "surrogateescape"))

    if action == "malformed":
        msg = f"{HARDENING_FSTAB_PATH} /proc line did not parse cleanly; leaving fstab unchanged. Will still remount."
        warn(log, msg)
    elif action == "preserved-stricter":
        output.detail(f"/proc already mounted with hidepid={effective} (stricter than agentworks default); preserved.")
    elif action == "preserved-unrecognized":
        output.detail(f"/proc has hidepid={effective} in {HARDENING_FSTAB_PATH}, a value Agentworks leaves as it is.")
    elif action != "no-op":
        # action in {"appended", "added-option", "upgraded"}: the content changed.
        output.detail(
            {
                "appended": "Added /proc entry to /etc/fstab",
                "added-option": "Added hidepid=1 to /proc options in /etc/fstab",
                "upgraded": "Upgraded /proc from hidepid=0 to hidepid=1 in /etc/fstab",
            }[action]
        )
    # Every action but an edit returns the content unchanged, which writes nothing.
    files.update(
        HARDENING_FSTAB_PATH,
        new_content.encode("utf-8", "surrogateescape"),
        observed=current,
        mode=0o644,
        parent_mode=0o755,
        preserve_metadata=True,
        elevated=True,
    )

    # Live remount with the effective value (idempotent at the kernel level
    # when options match the existing mount; cheap to call unconditionally).
    files.access.run(
        Command(("mount", "-o", f"remount,hidepid={effective}", "/proc"), elevated=True, deadline=files.deadline),
        check=True,
    )
