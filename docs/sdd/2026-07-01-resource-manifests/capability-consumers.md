# Companion: capability consumers and schema shapes, by cardinality

Status: SUGGESTION (2026-07-07). A prototype of what consuming a capability should look like across
every current and planned capability, written as feedback input for the plugin-system SDD (draft,
`docs/sdd/2026-07-06-plugin-system/`) and future work. The first three rows describe shipped
behavior; the rest are proposals and bind nothing. ADR 0016 carries the rules that ARE decided
(resources reference capabilities many-to-one; `provider` + `provider_config` on dedicated
capability-instance kinds; dedicated kinds only for real domain nouns).

## The observation this doc is built on

With the exposed-resource layer gone, every "consume a capability" site reduces to two questions:

1. **Cardinality**: does this consumer name ONE capability or MANY?
2. **What rides the selection**: nothing, a capability-owned config blob, or (for secrets) a
   per-consumer adjustment against a selection made elsewhere?

Three schema shapes cover every case (the first has two hosting forms):

| Shape                 | When                                    | Grammar                                                |
| --------------------- | --------------------------------------- | ------------------------------------------------------ |
| **Reference + blob**  | one capability, dedicated kind          | `provider: <name>` + optional `provider_config: {...}` |
|                       | one capability, inline in a consumer    | `<domain>: <name>` + optional `<domain>_config: {...}` |
| **Map keyed by name** | many capabilities, order-free           | `<field>: { <capability>: <blob-or-shorthand>, ... }`  |
| **Ordered name list** | many capabilities, order IS the meaning | `<field> = ["a", "b"]`; config lives elsewhere         |

The inline form drops the `provider` envelope: when a template field selects exactly one capability,
the FIELD is the selector -- it's not a "harness provider", it's simply the template's harness -- so
`harness: claude-code` plus a sibling `harness_config: {...}` (validated by the selected harness)
says everything the nested form said with one less layer. The `provider` / `provider_config`
spelling survives only where a dedicated kind's whole identity is "a configured capability instance"
(git-credential, vm-platform), where the generic field name is the honest one.

The map-vs-list split is the cardinality wrinkle: a keyed map gives uniqueness by construction,
per-key config with no envelope ceremony, and -- decisive for templates -- **per-key merge under
template inheritance** (a child overrides ONE feature's config without restating the set). A list
earns its place only when order is the semantic payload (the chain), and then it carries names only:
interleaving config into an ordered list couples two concerns and makes inheritance merges ambiguous
(replace? append? splice?).

A second observation, visible in the secrets rows: **selection and configuration can live in
different places.** Features co-locate them (the template both enables and configures). Secrets
split them: the chain (config) selects; per-secret maps only adjust. Both are legitimate -- the
split form is what "ambient capability, per-consumer tuning" looks like.

### The map form's limit: one entry per capability

A map key appears once, so the map form cannot express two mappings to the SAME capability -- a
secret findable under two env vars, a hypothetical mount feature activated twice with different
paths. Not a hard requirement today, but likely a reality eventually. Three extension paths, in
preference order:

1. **Capability-owned value multiplicity** (no schema change): the map's value vocabulary already
   belongs to the capability, so a capability that wants multiple lookups accepts a list value --
   `env-var: [NPM_TOKEN, NODE_AUTH_TOKEN]` ("try in order"). This is the right answer for
   adjust-style consumers: lookup multiplicity is intra-capability semantics, and the loop contract
   (one mapping per secret/backend pair) is untouched.
2. **Named-instance map** (the docker-compose shape): keys become operator-chosen names and the
   capability moves into the value --
   `features: { scratch: {feature: mount, path: /scratch}, cache: {feature: mount, path: /cache} }`.
   Duplicates allowed, and the two properties that made the plain map win for templates survive:
   per-key inheritance merge, and a stable identity for a child to override and for errors/status to
   name.
3. **Anonymous entry list** (the GitHub-Actions-steps shape):
   `features: [{feature: mount, path: /scratch}, {feature: mount, path: /cache}]`. The simplest
   duplicate-enabler, but a template context pays for it: no per-key inheritance merge (a child
   replaces the whole list) and no stable identity to override or report against. Fits an ordered,
   non-inherited consumer if one ever appears.

Accepting map-or-list polymorphically on one field is possible but buys two spellings of the common
case; the cleaner path is to keep the plain map as the default and graduate a specific consumer
deliberately when same-capability multiplicity becomes real for it.

## The table

| #   | Capability                          | Consuming resource / config         | Cardinality           | Shape                         |
| --- | ----------------------------------- | ----------------------------------- | --------------------- | ----------------------------- |
| 1   | `secret-backend`                    | `[secret_config].backends` (config) | many, ordered         | ordered name list             |
| 2   | `secret-backend`                    | `secret.spec.backend_mappings`      | many, adjusts ambient | map keyed by name             |
| 3   | `git-credential-provider`           | `git-credential.spec`               | one                   | reference + blob              |
| 4   | `vm-provider` (planned)             | `vm-platform.spec` (dedicated kind) | one                   | reference + blob              |
| 5   | `harness` (planned)                 | `session-template.spec.harness`     | one                   | reference + blob, inline      |
| 6   | `feature` (planned, per level)      | `<level>-template.spec.features`    | many, order-free      | map keyed by name             |
| 7   | plugin (trust unit, not capability) | `[plugins]` (config)                | many, order-free      | name list + namespaced tables |

## Samples, row by row

### 1. The chain: many, ordered -- names only

```toml
[secret_config]
backends = ["env-var", "onepassword", "prompt"]
```

Order is the meaning (resolution precedence), so this is the one list. It carries names only:
backend-level configuration (a future onepassword service-account token) is backend-scoped and lives
with the capability, not interleaved into the chain (FRD R8's backend-scoped-config-then-graduate
story).

### 2. `backend_mappings`: many, adjusting an ambient selection

```yaml
apiVersion: agentworks/v1
kind: secret
metadata:
  name: npm-token
  description: npm registry token
spec:
  backend_mappings:
    env-var: NPM_TOKEN # string shorthand: identifier override
    onepassword: # capability-owned addressing, full form
      vault: Work
      item: npm
      field: token
    prompt: false # opt-out shorthand
```

Map keyed by capability name; the VALUE vocabulary is capability-owned (env-var reads a string,
onepassword reads a structured address) plus two generic shorthands the loop owns: `false` (opt out)
and key-absent (backend default convention / soft-skip). The `false` shorthand exists here and
nowhere else because this is the one adjust-an-ambient-selection consumer: everything in the chain
applies unless a secret opts out. Opt-IN consumers (features) don't need it -- absence is the
opt-out.

### 3. git-credential: one capability, dedicated kind

```yaml
apiVersion: agentworks/v1
kind: git-credential
metadata:
  name: ado
spec:
  provider: azdo
  token: git-token-ado # kind-owned: every credential has one
  provider_config:
    org: my-org # capability-owned
```

The canonical single-reference shape (rule 1), hosted by a dedicated kind because a credential is a
real domain noun (templates reference credentials by name; the token secret hangs off it).

### 4. vm-platform: one capability, dedicated kind (planned)

```yaml
apiVersion: agentworks/v1
kind: vm-platform
metadata:
  name: azure-prod
  description: Production subscription, East US
spec:
  provider: azure
  provider_config:
    subscription: 1234-...
    resource_group: agw-prod
    region: eastus
```

Same shape as row 3. The dedicated kind is justified by the instance-identity test, not by the
pattern: many consumers name the platform (`vm-template.spec.platform: azure-prod`,
`agw vm create --platform azure-prod`, DB provenance), and "create a VM HERE" wants multiple named
heres per provider without carrying connection config on the create command. Note the consumers
reference the PLATFORM (a resource) by bare name -- resource-to-resource references don't use this
doc's shapes at all.

### 5. harness: one capability, inline in the template (planned)

```yaml
apiVersion: agentworks/v1
kind: session-template
metadata:
  name: claude
spec:
  harness: claude-code
  harness_config:
    permissionMode: auto
    marketplaces:
      - https://github.com/WayfarerLabs/nerftools#v4.0.0
    plugins:
      - nerftools-default@nerftools
```

The inline form of reference+blob (rule 2): the template is the only consumer (no dedicated
`harness` kind). No config needed? Just the selector:

```yaml
spec:
  harness: shell
```

Omitted entirely -> `shell`, preserving today's behavior; `harness_config` without `harness` is an
error (a blob with no owner). The two inherit as a pair under template inheritance: a child that
overrides `harness` gets a fresh (empty) `harness_config` rather than the parent's blob, which would
be addressed to the wrong capability. The `shell` harness's `harness_config` is where `command` /
`restart_command` / `required_commands` land, which keeps those fields' owner honest (they were
always harness-owned; the core just didn't have the word).

### 6. features: many, order-free -- the map earns its keep (planned)

```yaml
apiVersion: agentworks/v1
kind: agent-template
metadata:
  name: dd-agent
spec:
  inherits: [default]
  features:
    az-cli: {} # enabled, no config
    passport-agent: # enabled, capability-owned config
      ca: wayfarer-prod
      validity_days: 7
```

Map keyed by capability name, value = the capability-owned blob directly (`{}` for none). No
`provider:` key and no `provider_config:` envelope -- the map key IS the selection and the value IS
the blob. This is the map form the intro argues for (uniqueness by construction, per-key inheritance
merge, order-free); a child can override just `passport-agent.validity_days` or disable
inheritance-supplied `az-cli` (see below) without restating the set. Order-free is honest here
because activation order is the framework's job (same-level dependency topological sort, plugin SDD
R8).

Open sub-question for the plugin SDD: whether inheritance needs a disable shorthand (`az-cli: false`
-- borrowed from row 2's vocabulary -- to drop a parent-enabled feature). Opt-in-plus-inheritance
quietly recreates the ambient-selection situation within a template lineage, so the mapping
precedent likely transfers; flagging rather than deciding.

Dependencies stay capability-name-to-capability-name and are declared BY the feature, not in this
schema (the collapse ruling): `passport-agent requires passport-vm` is code-side metadata, validated
against the lineage at create time. Instance pairing, when a real case lands, is a reference inside
the blob (`broker: default` naming another feature's configured instance), not a schema shape.

### 7. plugins: config-level selection of trust units (planned)

```toml
[plugins]
enable = ["claude-code", "passport"]

[plugins.passport]
ca_dir = "~/.config/agentworks/passport"
```

Included for completeness because it LOOKS like row 6 and isn't: a plugin is a trust/distribution
unit, not a capability, and enabling one activates nothing (activation is rows 5-6). The shape is
config-idiomatic TOML -- a name list (order-free; a map of tables would also work and the plugin SDD
should pick) plus one namespaced table per plugin for plugin-level settings. Capability-level config
still lives at the reference sites above; the namespaced table is for plugin-wide concerns (the
passport CA directory, not per-agent validity).

## Secrets in capability config

Capability config will sometimes need secrets (an AWS vm-provider's client secret, a 1Password
service-account token). These are ordinary secret REFERENCES -- a bare secret name in a config
field, never a value -- and the existing machinery covers them end to end, in both flavors:

- **Defaulted, overridable** (the `git-token-<name>` / `tailscale-auth-key` precedent): a field
  (e.g. `aws_client_secret`) defaults to a well-known secret name (`aws-client-secret`). The
  operator may explicitly define the field to point it at an alternate secret instead.
  Auto-declaration means that the referenced secret (whether defaulted or overridden) Just Works
  with zero secret ceremony: the reference materializes a `secret` row at finalize with a
  synthesized description (`(auto) the client secret for vm-platform/aws-prod`), reachability is
  validated, doctor predicts resolution per row, and the default chain's prompt makes it resolvable
  out of the box.
- **Required, explicit**: same mechanism minus the default; the loader errors if the field is
  absent.

One extension is needed, because the blob is opaque: the framework cannot know that
`provider_config.client_secret` holds a secret name. The split of responsibilities matters:

- **The capability contributes schema knowledge only** ("field `client_secret` of my blob is a
  secret name, defaulting to `aws-client-secret`") -- via the validation invocation described in the
  next section. It declares no references: it has no config of its own, and the secret name is
  per-consumer (two AWS platforms can name different secrets).
- **The consuming resource declares the reference**, with ITSELF as the source -- exactly how
  `git-credential/ado` emits its `token` reference today while the `azdo` capability declares
  nothing. That attribution is what makes every downstream surface useful: the auto-declared
  description reads `(auto) the client secret for vm-platform/aws-prod` (not "for the aws provider"
  across all platforms), `Referenced by:` on the secret lists the platforms individually, and the
  needed-secrets walk reaches only the secrets of the platform actually in play.

The general rule: **whoever hosts the config that names the secret emits the reference.** (If
capability-scoped config ever materializes -- a backend-wide connection token -- the capability
resource would host that config and correctly become the source for that secret.) Everything
downstream (auto-declare, reachability, doctor, `backend_mappings` customization of the secret
itself) is stock.

Resolution happens at the consuming command's composition root, never at registry build (the
registry never resolves values). The hook exists today:
`compute_needed_secrets(..., extra_decls=...)` is exactly how tailscale keys and git-credential
tokens -- secrets needed by machinery rather than by env tables -- join a command's single resolve
pass. Capability-config secrets ride the same path: `vm create` against an AWS platform adds the
platform's secret decls to its resolve set.

## Capability config validation

The contract (for the plugin SDD to build): capabilities are invoked during validation of the
consuming resource -- they validate their own config block and return the resource references to
associate with the consuming resource (the one that owns the config block). Today's code is simpler
than the contract because nothing yet needs it: the sole config-bearing capability field (azdo's
`org`, a plain string) is validated in the git-credential kind's shared loader, and references are
hand-coded on the resource; no capability exposes a validate API, and no shipped blob contains a
resource reference. The capability RUNTIME APIs do ship (`SecretBackend`'s resolution methods,
`GitCredentialProvider.credential_lines`), each invoked by the framework at a well-defined moment --
so the contract is additive: one more method, one more moment, on API surfaces that already exist. A
further enhancement could be a schema-specification mechanism where capabilities register their
schemas, allowing the core engine to validate and generate resource references without invoking the
capability -- and, as a nice side effect, naturally documenting the capability config schema (e.g.
rendered by `agw resource describe <capability-kind>/<name>`). For that to work, the schema would
need to be able to describe fields as resource references to specific kinds (secrets as well as
other resources), and to include usage information to populate on those references.

## The rules, restated for the plugin SDD

1. One capability, dedicated kind (instance-identity test passes: vm-platform, git-credential):
   `provider` + `provider_config`.
2. One capability, inline in a consumer (the test fails: harness): the field is the selector
   (`harness: <name>`) with a sibling `<field>_config` blob; the pair inherits as a unit.
3. Many capabilities, order-free: a map keyed by capability name, value = capability-owned blob,
   `{}` for none. Templates get per-key inheritance merge for free.
4. Many capabilities, ordered: a list of bare names; config never rides an ordered list.
5. Adjust-an-ambient-selection consumers (today: only `backend_mappings`) add the `false` opt-out
   shorthand to the map shape (rule 3); pure opt-in consumers don't need it.
6. Value shorthands are per-domain sugar (`env-var: NPM_TOKEN` as a mapping value), always
   equivalent to a spelled-out form.
7. The map allows one entry per capability; extend deliberately when same-capability multiplicity
   becomes real (see "The map form's limit").
8. Secret-name fields inside capability config are ordinary secret references (see "Secrets in
   capability config").
9. Capability config: the contract is invoking the capability to validate its blob and return the
   references it implies (not yet needed by shipped code); a further schema-registration enhancement
   could let the core engine do both without invocation (see "Capability config validation").
