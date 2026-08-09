# High-level architecture: installer resource plugins

- Status: Independent artifact review clean, pending roadmap review
- Date: 2026-08-08
- Inputs: the [FRD](./frd.md), the revised [inventory](./inventory.md), and implementation on `main`
  at `615aa0da`

## Goals and boundary

The architecture moves 16 existing built-in manifest rows into two opt-in, manifest-only system
plugins. It also completes the generic resource enablement contract required to make that provider
change explicit and safe.

Core continues to own apt and install-command models, validation, dependency extraction, selection,
and remote execution. Snap, mise, dotfiles, tmuxinator, Claude setup, and initializer orchestration
are unchanged. The plugins contribute manifests and guide content only. No initializer capability,
callback, consumer gate, execution seat, raw-field gate, or default change is introduced.

## Decisions

### D1. Add a settings-only resource disable policy

`config.toml` gains this settings surface:

```toml
[resource_policy]
disabled = ["apt-package/gh", "user-install-command/nvm"]
```

Each entry is a canonical `kind/name` selector. The policy disables an app-shipped provider, which
means a built-in or system-plugin row. It never disables an operator declaration. That distinction
makes the sanctioned replacement flow coherent: the selector disables the shipped provider while a
same-name operator declaration becomes active.

Every selector must match an app-shipped provider. Unknown selectors and selectors that identify
only an operator row are configuration errors. A typo or inapplicable safety policy must not be
accepted as inert configuration.

The table is named `resource_policy`, not `resources`, because `resources` was the retired TOML
declaration namespace.

### D2. Collect a complete provider-claim ledger

Every plugin manifest publishes strongly whether its plugin is enabled or disabled. Plugin opt-in
still marks rows disabled at finalize, but it does not weaken their name claim.

`Registry.add` records a provider claim instead of resolving a pair against one occupied slot. Each
canonical selector therefore retains the complete claim set before any provider is chosen. An exact
republish with the same origin and value collapses idempotently. A changed value from the same
provider, multiple operator providers, or multiple app-shipped providers is an attributed hard
error.

The ledger also admits synthesized-default claims, which retain only the explicitly documented
kind-specific override contract. They are not app-shipped providers and cannot satisfy an explicit
disable selector.

### D3. Apply every enablement source to provider claims

Plugin opt-in and explicit resource policy are evaluated against the complete provider claims, not
only the eventual active row. A claim may be disabled by more than one source. Every applicable mark
is retained in deterministic source order and carries a stable source identifier, reason, and exact
remediation. Binary `Enablement.disabled` is derived from a non-empty mark tuple.

Applying marks before resolution ensures a displaced provider retains both the explicit policy mark
and a plugin opt-in mark when both apply. No projection reconstructs a reason from a row's origin.

### D4. Resolve complete claim sets once

After all publishers and enablement sources have contributed, one order-independent pass resolves
each selector:

1. Without an exact selector in `resource_policy.disabled`, registry construction fails and names
   both origins when exactly one operator and one app-shipped provider claim the name.
2. With the selector, exactly one operator claim becomes active and exactly one app-shipped claim is
   retained as the displaced provider.
3. A selector with multiple claims in either provider class is ambiguous and fails even when an
   explicit disable exists. The policy never chooses among multiple providers.
4. A selector with only an app-shipped claim retains that row with its complete disable marks. A
   selector with only an operator claim is active and cannot match explicit disable policy.
5. Synthesized-default replacement follows only its kind's separately declared override contract.

Plugin non-opt-in alone never authorizes replacement. This is essential because non-opt-in is a
plugin availability choice, while replacement is an explicit decision to change the provider of a
stable resource name.

The frozen registry retains each sanctioned substitution:

- selector and active operator origin
- displaced shipped origin
- every mark on the displaced claim, including the explicit policy mark that authorized replacement

The registry policy is config-independent and injected before publication. It tracks which explicit
selectors matched a surviving or displaced app-shipped claim. After resolution, unmatched selectors
fail as configuration errors. This validates typos and inapplicable policy without making
publication order observable.

### D5. Reject enabled resource edges to disabled targets at finalize

Once references and disable marks are known, finalize rejects every resource-to-resource edge whose
source is enabled and target is disabled. The error names:

- the enabled referrer and declaration location when available
- the disabled target
- every disable cause
- the exact actions to re-enable the shipped target, stop declaring the reference, or declare an
  operator provider under the same name

A disabled source is inert and does not activate or validate the availability of its dependencies.
This lets an entirely disabled plugin cohort contain internal references without breaking unrelated
configuration. Missing targets remain ordinary missing-reference errors.

Settings references keep wave 2's presence-not-availability contract. They are excluded from this
edge invariant, while `doctor` reports when a present settings target is disabled.

### D6. Use existing plugin manifests as the only contribution mechanism

The installed plugin index gains `apt` and `install-command`:

- `apt` packages the existing five `apt-source` and five `apt-package` rows.
- `install-command` packages the existing six `user-install-command` rows.

The rows move without name or spec changes. Their manifest files use the shared package loader and
the existing system-plugin origin. The original three built-in files stop publishing those rows.

Neither plugin advertises a capability. The unchanged core consumers read enabled rows from the same
finalized registry graph they use today. Existing plugin opt-in marks provide the precise R3 error
when a selected moved row belongs to a disabled plugin.

### D7. Keep conceptual teaching visible and filter resource-derived topics

Each plugin contributes a conceptual `plugin/<name>/...` guide topic explaining ownership,
enablement, selection, verification, replacement, and the next safe action. Conceptual topics remain
visible while the plugin is disabled because they teach the remediation.

Resource-derived guide names follow the resource's stored enablement and disappear from normal guide
output when disabled. Resource lists and resource completion already use the normal filtered view.
Guide names must use the same graph truth rather than enumerate every row blindly.

No new completion mechanism is needed. Bash, Zsh, and PowerShell continue to consume
`agw resource list --names-only` and `agw guide --names-only`.

### D8. Verify shipped package data and permanent documentation

Tests build and inspect the wheel to prove that both plugins' YAML and guide Markdown ship and are
readable through `importlib.resources`. Editable-source success is not sufficient evidence.

The behavior changes and permanent docs land together. The sample config, plugin inventory, resource
guide, idempotency guide, CLI README, built-in manifest README, ADR 0021, and 0.14 upgrade guide all
describe the two plugin opt-ins and explicit replacement flow. Release Please owns versions and the
changelog.

## Registry construction topology

```text
config settings
  -> parse resource_policy.disabled
  -> construct config-independent registry policy
  -> collect built-in, operator, synthesized, capability, and all plugin claims
  -> validate complete claim sets and collapse exact idempotent republication
  -> apply every enablement source to every provider claim
  -> resolve claims and retain sanctioned substitutions
  -> validate that every policy selector matched an app-shipped provider
  -> extract references and reject enabled source -> disabled target
  -> freeze graph, disable provenance, and substitutions
  -> expose filtered list/completion/guide views plus describe/doctor provenance
```

All steps through graph freeze are local and side-effect free. A disabled dependency, bad selector,
or provider collision fails before any VM or user mutation.

## Error contract

Three errors remain distinct:

1. **Owning plugin disabled.** An enabled resource references a moved row whose plugin is not in
   `[plugins].system`. The error names the moved selector, owning plugin, referrer, and a valid TOML
   replacement snippet containing the complete preserved list, for example:

   ```toml
   [plugins]
   system = ["onepassword", "claude", "apt"]
   ```

   The same error also names the two complete alternatives: remove the reference, or add the exact
   `resource_policy.disabled` selector and declare an operator provider under the same name. An
   operator declaration without that selector would be a C4 collision and is never offered as a
   valid remediation.

2. **Resource explicitly disabled.** An enabled resource references a selector disabled by
   `resource_policy.disabled`. The error names both ends and tells the operator to remove the
   selector, remove the reference, or declare an operator provider under the same name.
3. **Provider collision.** An operator and app-shipped provider claim one name without explicit
   authorization. The error names both origins and tells the operator to rename the operator row or
   add the exact disable selector and keep the name.

Describe and doctor are not error-message substitutes. They render the same retained marks and
substitution records for later diagnosis.

## Testing boundaries

Unit and integration tests cover:

- settings parsing, selector normalization, unknown and inapplicable selectors
- complete claim sets, exact idempotent republication, collision order symmetry, plugin-off
  collisions, ambiguous multi-provider errors, explicit replacement, and displaced provenance
- multiple disable causes without first-source loss
- enabled-source to disabled-target errors, disabled-source inertness, and settings-reference
  exceptions
- list, include-disabled, describe, doctor, resource completion, and guide filtering
- exact manifest parity for all 16 moved rows and unchanged core execution-path tests
- both plugins' enabled, disabled, collision, and disable-and-redeclare behavior
- package installation and wheel inspection for manifests and guide content
- sample-config and installed-plugin roster drift tests
- exact R3 TOML snippets for empty and already-populated plugin lists
- plugin-disabled errors whose disable-and-redeclare alternative includes both the exact policy
  selector and a same-name operator declaration

The integration gate drives the real CLI in an isolated home and validates enabled create and
idempotent reinit on an available live VM backend. No test expands scope into moving core runners.

## Rejected alternatives

- **Move generic apt or install-command execution into plugins:** rejected because operator rows use
  the same core consumers and the operator explicitly limited this effort to declared resources.
- **Move snap, mise, dotfiles, tmuxinator, or Claude setup:** rejected as out of scope. Claude also
  lacks the future harness-integration user facet required for a clean move.
- **Add an initializer capability or callbacks on `Plugin`:** rejected because manifest-only plugins
  need no execution extension point.
- **Derive raw-field consumer gates or change defaults:** rejected because no raw configuration
  surface moves.
- **Publish disabled plugin rows weakly:** rejected because it silently changes providers and loses
  substitution provenance.
- **Treat plugin non-opt-in as replacement authorization:** rejected because enabling a provider and
  replacing a stable name are different operator decisions.
- **Reject dependencies of disabled sources:** rejected because disabled resource cohorts are inert.
- **Hide conceptual plugin guide topics while disabled:** rejected because that removes the teaching
  required to enable the plugin.
