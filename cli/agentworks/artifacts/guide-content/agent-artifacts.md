---
description: Select reusable agent context and inspect scoped artifact delivery.
index-order: 45
---

# Agent artifacts

Artifact bundles supply hints, rules, standard skills, and agents (agent personas) without making
producers depend on a particular harness. An owning scope selects bundles; explicitly activated
integration facets decide native placement or defer to a later facet.

Start with `agw resource explain artifact-bundle` and `agw resource sample artifact-bundle`. Use the
owning template's `resource explain` output for `artifacts.bundles` and integration activation.
`agw artifacts show --help` lists read-only inspection selectors. Inspection includes the chosen
owner and its actual ancestors, including artifacts already handled upstream, without showing bodies
or fetching content. Recorded application is evidence of a past operation, not current model
context.

<!-- agw:include path="artifacts/README.md" heading="Select an owner and a handler" -->

<!-- agw:include path="artifacts/README.md" heading="Sources and refresh" -->

<!-- agw:include path="artifacts/README.md" heading="Native delivery" -->

Before reinitializing or recreating an owner, establish authorization for that operation and inspect
its current declaration and effects. Reinit can install, update, and remove owned files; workspace
recreation replaces a shared owner and needs its normal lifecycle review. If the requested setup is
not authorized, stop after inspection and report which owner needs a change. Reading this guide does
not authorize setup or running imported skill scripts.
