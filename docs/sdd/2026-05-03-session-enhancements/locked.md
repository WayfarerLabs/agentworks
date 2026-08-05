# Session enhancements: locked

**Locked:** 2026-08-05

This effort is complete. Sessions persist a tmux server PID and VM boot ID, derive one live
`SessionStatus` (`OK`, `STOPPED`, `BROKEN`, or `UNKNOWN`), repair incomplete legacy rows on access,
and permit PID-based force termination only for a same-boot live process whose tmux socket is
unreachable. The completed plan remains the immutable implementation record.

The status model later evolved from the original shared-default-server distinction: current admin
and agent sessions both use per-session sockets. Old admin rows without a socket are directed
through `session resume` for migration. Transport failures and failed boot-ID reads resolve to
`UNKNOWN`, preserving the rule that missing evidence must not authorize destructive action.

The concise permanent maintainer contract is `docs/guides/session-status.md`, linked from the
sessions section of `cli/README.md`. Current implementation and regression anchors are named there.
Nothing in this directory is required to maintain the current status behavior.
