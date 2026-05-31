# Resilient Provisioning -- Lockfile

## 2026-03-25

Phase 1 (VM host operations and provisioning resilience) is fully implemented. Phases 2-3 (bootstrap
and init resume/detached execution) are deferred -- init is repeatable via `vm reinit`, so the value
of making it resilient to disconnects is low.

These specs are accurate as of this date but are now locked and will not be updated to reflect
further changes to the implementation.
