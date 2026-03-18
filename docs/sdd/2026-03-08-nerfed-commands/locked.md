# Locked -- Superseded

**Date:** 2026-03-17

## Status

This SDD is superseded by [2026-03-17-nerf-tools](../2026-03-17-nerf-tools/).

## Summary

The original nerfed commands design centered on a compiled SUID binary (`nerfrun`) as the
enforcement mechanism. Agent users ran tool-name symlinks pointing to `nerfrun`, which elevated to
the `agentworks` identity via SUID, checked RBAC rules in `rbac.toml`, and executed the scoped
command with the user account's credentials.

After further design review, this approach was set aside in favor of a simpler model:

- Nerf tools are standalone shell scripts generated from a TOML manifest
- Enforcement is at the application layer (AI coding framework permission models)
- No SUID binary, no compiled artifacts, no runtime dependency on the manifest
- Credential access relies on the agent's existing access; scoped injection is a future capability

The SUID/OS-level enforcement approach is not ruled out for future use cases, but it is a separate
mechanism -- not an evolution of the nerf tools model. If introduced, it would be a distinct design.

The features described in this SDD that are not in the new design (bigred, nerf-wcid, audit trail,
RBAC rules) may be revisited as part of the credential injection future work.
