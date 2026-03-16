# Resilient Provisioning -- Implementation Plan

**Status:** Draft **Branch:** `feat/resilient-provisioning`

---

## Problem

Long-running provisioning operations (limactl create/start, bootstrap script, Phase B init) run
synchronously over SSH. If the workstation's network connection drops mid-operation, the SSH session
dies and the operation may be left in an unknown state. The user gets a cryptic error and has to
manually figure out what happened.

This is especially painful on flaky networks where the workstation -> VM host connection is
unstable. The operations themselves may still be running fine on the remote host, but the
workstation has lost its window into them.

## Approach

Wrap long-running remote operations in `nohup` with output redirected to a file. The workstation
launches the operation, then polls for completion by checking process status and tailing the output
file. If the connection drops, the operation continues on the remote host. On reconnect, the
workstation picks up where it left off.

`nohup` is available on every Unix system with no dependencies, making it safe to use even during
bootstrap (before tmux or other tools are installed).

### Key Design Decisions

- **Fire-and-poll, not stream**: the workstation does not stream stdout over SSH. Instead it
  periodically reads the output file. This means a dropped connection has zero impact on the
  running operation.
- **PID file for resume**: the remote process writes its PID to a known file. The workstation
  checks `kill -0 $pid` to see if it's still running.
- **Structured output unchanged**: the bootstrap script's `##STEP##`/`##SUCCESS##` markers work
  fine in an output file. The workstation parses them the same way.
- **Timeout with progress**: if no new output appears for N seconds, warn the user rather than
  killing the operation. The remote process has its own timeout.

### Remote Execution Wrapper

A helper function that encapsulates the nohup + poll pattern:

```
def run_detached(target, command, output_file, pid_file, *, poll_interval=5, timeout=600):
    """Run a command detached on a remote host, polling for completion.

    1. Start: nohup bash -c '<command>' > <output_file> 2>&1 & echo $! > <pid_file>
    2. Poll: check kill -0 $(cat <pid_file>); tail output_file for new content
    3. Complete: read final output, return exit code from a status file
    4. Resume: if pid_file exists and process is running, skip start and resume polling
    """
```

This helper is used by all three phases. The caller provides the command and file paths, the helper
handles the nohup/poll/resume logic.

---

## Phase 1: VM Host Operations (limactl)

Make limactl create and start resilient to workstation -> VM host connection drops.

### 1.1 Detached Execution Helper

- [ ] Create `cli/agentworks/remote_exec.py` with `run_detached()` function
  - Start command via nohup with output and PID files
  - Poll loop: check PID alive, tail new output, print to console
  - Resume: detect existing PID file, skip start if process still running
  - Return: final output, exit code (from status file written by wrapper script)
  - Handle cleanup of remote temp files on completion
- [ ] Write a wrapper shell script template that:
  - Writes PID to pid_file
  - Runs the command
  - Writes exit code to a status file on completion
- [ ] Write tests with mock ExecTarget

**Definition of done:** `run_detached()` launches a command, polls for output, and returns results.
Simulated SSH drop (stop polling, resume) picks up where it left off.

### 1.2 Wire into Lima Provisioner

- [ ] Update `_run_lima()` for remote mode to use `run_detached()` instead of `ssh_run()`
  - Output file: `/tmp/agentworks-lima-<vm_name>.out`
  - PID file: `/tmp/agentworks-lima-<vm_name>.pid`
- [ ] Handle resume: if `vm create` is re-run and the limactl process is still running, resume
  polling instead of starting a new create
- [ ] Handle stale state: if the process finished but we never saw the result, read the output
  file and status file to determine success/failure
- [ ] Local mode unchanged (local subprocess doesn't have the SSH drop problem)

**Definition of done:** `limactl create` and `limactl start` survive workstation SSH drops. Re-running
`vm create` while limactl is still running resumes instead of failing.

### 1.3 Update vm create Flow

- [ ] Update `create_vm()` in `manager.py` to handle resume scenario
  - If DB record exists with provisioning_status "in_progress", attempt resume
  - If DB record exists with provisioning_status "pending", check for stale detached process
- [ ] Update error messages for detached execution context

**Definition of done:** Interrupted `vm create` can be re-run to resume. Clear messaging about
what is happening.

---

## Phase 2: Bootstrap Script

Make the bootstrap script (Phase A) resilient to both workstation -> VM host and VM host -> VM
connection drops.

### 2.1 Detached Bootstrap Execution

- [ ] Update `_phase_a_bootstrap()` to use `run_detached()` for the bootstrap script
  - The bootstrap script already runs as a single bash script on the VM
  - Wrap in nohup so it survives SSH drops from VM host to VM
  - Output file: `/tmp/agentworks-bootstrap-<vm_name>.out`
  - PID file: `/tmp/agentworks-bootstrap-<vm_name>.pid`
- [ ] Parse bootstrap output from the output file (same `##STEP##` markers)
- [ ] Handle resume: if bootstrap is still running, resume polling

**Definition of done:** Bootstrap script survives SSH drops. Output is captured and parsed the
same way.

---

## Phase 3: Phase B Initialization

Make Phase B init (over Tailscale SSH) resilient to connection drops.

### 3.1 Detached Init Steps

- [ ] Evaluate which Phase B steps benefit from detached execution
  - apt-get install (long-running, benefits from nohup)
  - Install commands (variable duration)
  - Short steps (shell config, git credentials) probably fine as-is
- [ ] Wrap long-running Phase B steps in `run_detached()` where beneficial
- [ ] Handle resume for partially-completed Phase B

**Definition of done:** Long-running init steps survive Tailscale SSH drops. `vm reinit` can
resume a partially-completed initialization.

---

## Phase 4: Documentation and Cleanup

### 4.1 Documentation

- [ ] Update CLI README with resilient provisioning behavior
- [ ] Document resume behavior (re-running vm create picks up where it left off)

### 4.2 Final Validation

- [ ] Full test suite passes
- [ ] Manual test: kill SSH connection during limactl create, reconnect, vm create resumes
- [ ] Manual test: kill SSH connection during bootstrap, reconnect, vm create resumes
