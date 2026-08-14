# 22. A Single Resource-Declaration Frontend (config.toml Is Settings Only)

Date: 2026-08-05

## Status

Accepted. Supersedes the "Dual-path: deprecate, don't break" stance of
[ADR 0016](0016-yaml-resource-manifests.md) ONLY.

> Amended 2026-08-07 (operator ruling): **`agw resource migrate` is deleted, and Agentworks
> maintains no automated migration tooling.** Every reference to it below reads as history. The
> decision this ADR records is unaffected: the frontend is still single, `config.toml` is still
> settings only, and a resource-declaring section is still a hard error. What changed is the
> remediation the error names. It now carries the rewrite itself, plus `agw resource sample` and
> `agw resource describe-kind` (which print the target shape) and `docs/guides/upgrading-to-0.14.md`
> (which walks it through). The settings-only escape hatch survives for `resource sample --write`
> and `resource edit`'s fallback, so an operator can still author the replacement manifests against
> a config the app would otherwise refuse to load. The migrator's deletability was designed in (a
> separability guard kept it a leaf), which is what let it be removed before release rather than
> maintained until a scheduled expiry. Everything else ADR 0016 decides still stands: the two-layer
> config/resource split, the vocabulary law, resources-reference-capabilities (with the capability
> naming rule and the graduate-when-real clause), the Kubernetes envelope and auto-load, and the
> slash ban. This ADR narrows the resource-declaration frontend from two paths to one; it does not
> reopen the model.
>
> Amended 2026-08-14 (operator ruling): the release-scoped resource-section rewrite and
> settings-only escape hatch have been retired. The old section roots are ordinary unexpected
> top-level keys now. Commands that load config, including doctor and the write forms of resource
> sample and schema, require a valid settings-only file. The permanent migration guide still records
> the section-to-kind mapping, while stdout `resource sample` and `resource describe-kind` remain
> config-independent.

## Context

ADR 0016 accepted two operator publishers into the same resource registry: YAML manifests under
`<config-dir>/resources/` and the legacy resource sections of `config.toml` (`[secrets.*]`,
`[vm_templates.*]`, `[git_credentials.*]`, the legacy flat `[azure]` / `[proxmox]` vm-site sections,
and so on). Keeping both live was a deliberate stance ("not a transitional window"): it forced the
"different publishers, single registry" architecture to be real, and it let operators migrate on
their own schedule with `agw resource migrate`.

That stance has done its job, and the cost of keeping it has come due:

1. **The registry architecture is proven.** Bundled built-ins and system plugins (ADR 0021) publish
   into the registry through the same mechanism operators use. The dual TOML path is no longer what
   keeps the abstraction honest; the plugin origins do.
2. **Two frontends is a standing tax.** Every resource kind carried two decode surfaces (a flat TOML
   loader and a YAML decoder) that had to stay behavior-identical, and every reader had to learn
   both spellings plus the unwritten rule about which applied where. The follow-on work that models
   each kind's spec once ([ADR 0023](0023-declared-schemas-and-the-kind-descriptor.md)) would have
   had to model it twice with two frontends.
3. **The deprecation runway has shipped.** Declaring resources in `config.toml` was marked
   deprecated for removal at load time in 0.13.0 (PR #315, the aggregated load-time warning), and
   the tagged-capability-config pre-support landed in the same release (PR #349). The FRD's
   one-released- warning-version requirement is satisfied.

## Decision

**The resource-declaration frontend is single: YAML manifests are the only way an operator declares
a resource. `config.toml` is settings only.** A `config.toml` that still carries a
resource-declaring section is a hard `ConfigError` at load, naming the offending sections and
pointing at `agw resource migrate` (and, for the legacy flat sites, noting that `[azure]` /
`[proxmox]` migrate as `vm-site`). The warning became an error; the section no longer loads.

This is the change from ADR 0016's dual-path section and nothing more. `config.toml` remains the
home for settings (operator identity, paths, defaults, the secret source chain, `[plugins].system`),
and those sections load exactly as before. The `[secret_backends.*]` no-op sections keep their
existing deprecation warning: they were never resource declarations, so FR1 does not sweep them into
the hard error.

`agw resource migrate` remains the escape hatch and the recommended path. It still reads the legacy
TOML directly and writes YAML manifests, backup-first, with rollback on a verification mismatch; the
migrator's operator-facing contract is unchanged. Because a normal load now rejects a config that
still declares resources, the migrator (and `agw resource sample --write` and `agw resource edit`'s
fallback) load with resources skipped (the settings-only escape hatch), so an operator can migrate a
config the app would otherwise refuse to load.

## Consequences

- **One spelling to learn, one decode surface to maintain.** Operators declare resources as YAML
  manifests, full stop. Internally the manifest decoders now own their per-kind validation directly
  rather than routing through the TOML loaders; the relocated TOML reader survives only as the
  migrator's private, frozen oracle for verifying a migration. (This corrects ADR 0016's
  Consequences claim that "the manifest decoders call the TOML loaders, so the two sources cannot
  drift"; there is no longer a TOML source in the load path to drift from, and the decoders validate
  on their own. See the amendment note on that bullet.)
- **Breaking change, guided.** An operator whose `config.toml` still declares resources gets a hard
  error on the next command instead of a silent load-with-warning. The error names the sections and
  the exact remediation (`agw resource migrate --all`, or per kind), and the migrator can still run
  against the offending config via the settings-only load. The upgrade note lives in
  `docs/guides/upgrading-to-0.14.md`; the removal commit carries a `feat(config)!:` marker with a
  `BREAKING CHANGE` footer so release-please surfaces it.
- **The runway is spent.** With the TOML resource path gone, the aggregated TOML-section deprecation
  warning and its `--no-deprecations` channel entry retire. The tagged-capability-config shape
  deprecation (PR #349) and the settings-side deprecations are unaffected and remain on the channel.
- **Follow-on modeling is unblocked.** With a single frontend, each kind's spec is modeled once
  rather than reconciled between a TOML loader and a YAML decoder. That is
  [ADR 0023](0023-declared-schemas-and-the-kind-descriptor.md), which this ADR is the precondition
  for.

## Relationship to ADR 0016

ADR 0016 is amended (not rewritten) with a "Superseded by ADR 0022" pointer on its dual-path status
note, and its Consequences bullet about the manifest decoders calling the TOML loaders is corrected
in place, since that implementation claim is now false. ADR 0016 remains the record of the
config/resource model, the vocabulary law, resources-reference-capabilities, the envelope,
auto-load, and the slash ban; this ADR changes only how many publishers an operator has (one, YAML).
