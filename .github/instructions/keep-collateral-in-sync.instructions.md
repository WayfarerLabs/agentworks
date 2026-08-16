---
description: >-
  A change that outdates its docs, specs, sample config, completions, or guide
  topics updates them in the same change, and keeps guide teaching safe
---
# Keep Collateral in Sync

Behavior here has collateral. When a change outdates any of it, updating it is part of that change
rather than follow-up work. Read the relevant collateral before you start, because it carries
context you need, and check it again before you finish, because by then you are the one who outdated
it.

| Collateral    | Update it when                                                                                      | Where it lives                                                               |
| ------------- | --------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------- |
| Docs          | behavior, usage, or any fact a doc states changes                                                   | `docs/`, `cli/README.md`, the doc nearest the code                           |
| Sample config | a setting is added, renamed, or retired, or its comments go stale                                   | `cli/agentworks/sample-config.toml`                                          |
| Completions   | the command tree, a command's options, or a parameter's completable names change                    | `cli/agentworks/completions/`, whose README covers the hand-maintained parts |
| Guide topics  | a retained concept, workflow, plugin-owned topic, release link, action, or related topic goes stale | the core, subsystem, or plugin package that owns that retained contribution  |

This is the app's collateral, not the development process's. SDD artifacts move under the `sdd`
skill's rules instead, and the `development-process` rule is what tells you to know whether an SDD
governs your work at all.

Sample config carries upkeep beyond the trigger above: its comments and its organization stay
complete, accurate, clear, and easy to use, not merely non-stale.

A retained guide topic goes stale in four ways, not one: its teaching, its relationships, its
examples, and its agent contract. Beyond staying current it carries two constraints. Cover the
ordinary operator path and the next safe action, and signpost the current command instead of
restating command-owned resource, schema, graph, or runtime facts. The onboarding assessment alone
may project its bounded finalized-registry and stored-instance facts. The second is a security
boundary, not a style preference: guide content is instructional, never consent, so it must not
resolve or expose secrets, inspect the workstation, connect to a VM, perform remote work, mutate
state, or imply that rendering authorized any of those. A suggested operation crossing a consent
boundary stays an inert action record carrying its exact scope, expected result, and a useful
refusal alternative.
