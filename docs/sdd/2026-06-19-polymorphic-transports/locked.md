# Polymorphic transports -- Lockfile

## 2026-06-24

All plan items shipped via PR #130. Two small post-merge follow-ups landed alongside: PR #131
(operation-level tracebacks now land in the per-op `SSHLogger` log instead of the shared
`error.log`) and PR #132 (agent's `~/.agentworks-rc.sh` is written unconditionally to match the
admin pattern). Issue #113 -- SSH ControlMaster on managed Host blocks -- shipped on top of all this
via PR #134; on Linux/macOS it multiplexes the dozens of SSH calls each VM/agent lifecycle op
issues, on Windows it's gated off (OpenSSH bug).

See [plan.md](plan.md) for the full per-phase detail. These specs are accurate as of this date and
are now locked.
