# Tasks -- Lockfile

## 2026-03-25

All artifacts in this feature directory (FRD, HLA, plan) were implemented and verified as of this
date. The implementation matches the specs with the following notable design decisions made during
development:

- The built-in default task template runs a login shell (empty command) rather than a specific tool
  like Claude. Claude is shown as an example in the sample config.
- `task start` was renamed to `task restart` with support for a `restart_command` template field.
- The restricted tmux config loads the user's `~/.tmux.conf` and selectively unbinds dangerous keys
  rather than stripping all keybindings.
- Tmuxinator was repurposed (not removed) as the workspace console mechanism. The VM console remains
  a separate, dynamically-built tmux session.
- SSH logging (SSHLogger) replaced the original InitLogger with a unified, incremental logger.
- Windows SSH requires `-tt` (forced TTY) for reliable non-interactive command execution over
  Tailscale. This is applied per-target via `force_tty` on `SSHTarget`.

These specs are accurate as of this date but are now locked and will not be updated to reflect
further changes to the implementation.
