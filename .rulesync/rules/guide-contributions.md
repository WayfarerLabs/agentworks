---
description: "Keep guide teaching complete, colocated, and safe"
globs: ["**/*"]
---

# Guide Contributions

Code that adds or changes a resource kind, capability implementation, plugin, or documented operator
workflow must add or update the corresponding `agw guide` topic contribution. Treat guide teaching
as part of the change, not as follow-up documentation.

Keep authored topic content with the package that owns the behavior. Update related topics when a
change makes their teaching, relationships, examples, or agent contract stale. Cover the ordinary
operator path, relevant readiness or enablement states, and the next safe action without duplicating
facts that the guide projects from the finalized registry or stored instance rows.

Guide content is instructional and never grants consent. It must not resolve or expose secrets,
inspect the workstation, connect to a VM, perform remote work, mutate state, or imply that rendering
authorized any of those actions. Any suggested operation that crosses a consent boundary must stay
an inert action record with the exact scope, expected result, and a useful refusal alternative.
