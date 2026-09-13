---
description: Select reusable agent context and inspect scoped artifact delivery.
index-order: 45
---

# Agent artifacts

Artifact bundles supply hints, rules, standard skills, and agents (agent personas) without making
producers depend on a particular harness. An owning scope selects bundles; explicitly activated
integration facets decide native placement or defer to a later facet. For the scope/facet model,
activation, environment, and prerequisites, see `agw guide show concept-harness-integrations`.

Start with `agw resource explain artifact-bundle` and `agw resource sample artifact-bundle`. Use the
owning template's `resource explain` output for `artifacts.bundles` and integration activation.
`agw artifacts show --help` lists read-only inspection selectors. Inspection includes the chosen
owner and its actual ancestors, including artifacts already handled upstream, without showing bodies
or fetching content. Recorded application is evidence of a past operation, not current model
context.

<!-- agw:include path="artifacts/README.md" heading="Select an owner and a handler" -->

<!-- agw:include path="artifacts/README.md" heading="A minimal bundle and consumer" -->

<!-- agw:include path="artifacts/README.md" heading="Composition and names" -->

<!-- agw:include path="artifacts/README.md" heading="Sources and refresh" -->

<!-- agw:include path="artifacts/README.md" heading="Native delivery" -->

Native-home overrides for user artifact files must stay beneath that user's `HOME`. For example,
`CLAUDE_CONFIG_DIR`, `CODEX_HOME`, or `GROK_HOME` can select another directory there. A user
artifact plan that would write outside `HOME` is refused before that integration changes settings or
plugins. Session files still use their private directory beneath `HOME`.

Native preflight conservatively refuses relevant restrictions in the configuration files it
inspects. It cannot verify that a later native settings layer reenables an artifact, so such a
combination is unsupported even when the native tool would allow it. Inspect the effective native
policy before changing restrictions; the error does not establish that the tool itself disables the
artifact.

<!-- agw:include path="artifacts/README.md" heading="Inspect before changing state" -->

Before reinitializing or recreating an owner, establish authorization for that operation and inspect
its current declaration and effects. Reinit can install, update, and remove owned files; workspace
recreation replaces a shared owner and needs its normal lifecycle review. If the requested setup is
not authorized, stop after inspection and report which owner needs a change. Reading this guide does
not authorize setup or running imported skill scripts.
