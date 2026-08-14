---
description:
  "A change that outdates its docs, specs, sample config, completions, or guide topics updates them
  in the same change"
---

# Keep Collateral in Sync

Behavior here has collateral. When a change outdates any of it, updating it is part of that change
rather than follow-up work. Read the relevant rows before you start, because they carry context you
need, and check them again before you finish, because by then you are the one who outdated them.

| Collateral    | Update it when                                                                                                         | Where it lives                                                               |
| ------------- | ---------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------- |
| Docs          | behavior, usage, or any fact a doc states changes                                                                      | `docs/`, `cli/README.md`, the doc nearest the code                           |
| SDD artifacts | work falls under an active SDD: plan boxes, and any FRD, HLA, plan, or LLD the implementation moved past               | `docs/sdd/<effort>/`                                                         |
| Sample config | a setting is added, renamed, or retired, or its comments go stale                                                      | `cli/agentworks/sample-config.toml`                                          |
| Completions   | the command tree, a command's options, or a parameter's completable names change                                       | `cli/agentworks/completions/`, whose README covers the hand-maintained parts |
| Guide topics  | a resource kind, capability, plugin, or documented operator workflow changes, or a related topic's teaching goes stale | the `agw guide` contribution beside the package that owns the behavior       |

When you are not the effort's lead, flag a lead-owned SDD artifact rather than editing it (principle
9).

Guide topics carry two constraints beyond staying current. Cover the ordinary operator path, the
relevant readiness or enablement states, and the next safe action, without restating facts the guide
already projects from the finalized registry or stored instance rows. And guide content is
instructional, never consent: it must not resolve or expose secrets, inspect the workstation,
connect to a VM, perform remote work, mutate state, or imply that rendering authorized any of those.
A suggested operation crossing a consent boundary stays an inert action record carrying its exact
scope, expected result, and a useful refusal alternative.
