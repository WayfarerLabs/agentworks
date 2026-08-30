# Upgrading to 0.17

The 0.17 boundary has three upgrade actions. Third-party `harness-integration` authors must move
from contract version 1 to version 2, where merge behavior belongs to the config model the
integration offers. Every VM created before 0.17 needs one successful `agw vm reinit NAME` before
ordinary Agentworks SSH operations will use it. Every declared `git-credential` must also move to
the new provider-owned structured `source`, followed by reinitialization of each affected admin or
agent user. Operators using the shipped `shell`, `claude-code`, `codex`, or `grok-build`
integrations do not need to change integration configuration.

**This guide is release-scoped.** It carries existing VMs, third-party harness integrations, and Git
credential declarations across the 0.17 boundary. The permanent contracts live in
[`cli/agentworks/schema/README.md`](../../cli/agentworks/schema/README.md),
[`cli/agentworks/capabilities/harness_integration/README.md`](../../cli/agentworks/capabilities/harness_integration/README.md),
and
[`cli/agentworks/capabilities/git_credential/README.md`](../../cli/agentworks/capabilities/git_credential/README.md).

## Third-party harness integrations must declare contract version 2

Agentworks matches capability contract versions exactly. A harness integration that still declares
`contract_version = 1` is refused at plugin registration with a version-mismatch diagnostic. There
is no version-1 compatibility adapter because it would preserve two merge-policy authorities.

Change every third-party harness integration class to declare:

```python
from typing import ClassVar

contract_version: ClassVar[int] = 2
```

All shipped harness integrations already declare version 2.

## Remove imperative config merging

`HarnessIntegration.merge_config` and the package-level `merged_config` helper are gone. Remove any
override, helper import, and callback-specific test. The framework now combines repeated
declarations through the model the registered integration offers via `config_for()` (normally its
`config_model`).

Version 1's default callback performed a shallow child-wins merge: each incoming top-level key
replaced that key's complete prior value. Only an integration's `merge_config` override changed that
default; shipped `shell.required_commands` and `codex.writable_dirs` append-deduplicated. Merely
changing `contract_version` to 2 can therefore change behavior: an unannotated list now
append-deduplicates, and an unannotated nested object now recursively merges. Compare every field
with its version-1 callback and mark complete values `REPLACE` wherever child replacement must
remain.

The model defaults are:

- objects and mappings recursively merge by key;
- lists append unequal atomic items in stable order and deduplicate equal items; and
- scalars replace with the incoming value.

Declare only behavior that differs from those defaults. A list that should replace, for example an
argument vector, uses field metadata:

```python
from typing import Annotated

from pydantic import Field

from agentworks.schema import MergeStrategy

extra_args: Annotated[list[str], MergeStrategy.REPLACE] = Field(default_factory=list)
```

A mapping-shaped model that should replace everywhere can declare:

```python
merge_strategy: ClassVar[MergeStrategy] = MergeStrategy.REPLACE
```

A containing field override takes precedence over the selected model policy. `REPLACE` applies to
the whole value, so incoming `{}` or `[]` clears the prior object or list. List items themselves
remain atomic; version 2 has no identity or recursive list-item merge protocol.

The shipped policy examples are useful reference points: `shell.required_commands` and
`codex.writable_dirs` append-deduplicate, while the `extra_args` fields on `claude-code`, `codex`,
and `grok-build` replace.

## Make the offered config model merge-conformant

Plugin registration runs `merge_contract_error()` over the complete reachable model returned by
`config_for()` before seating any implementation. In addition to ordinary capability model
conformance, version 2 requires:

- no `validation_alias` on a participating model field, including in nested models, mapping values,
  and below replacement boundaries;
- an exact `str` key annotation for every mapping that merges by key; and
- a closed, structurally comparable JSON element domain for every append-deduplicated list.

Serialization-only aliases remain valid. Use the field name for validation, and keep
`serialization_alias` if output needs a different spelling. If a mapping or list intentionally
accepts a broader Python domain, mark that complete node `REPLACE` instead of asking the recursive
merger to interpret it.

Final typed validation remains the error boundary for authored values. The merger does not run model
validators, apply defaults, coerce values, or filter malformed input.

## No config or CLI migration for merge behavior

The merge-policy cutover changes only the harness-integration plugin contract and how existing
declaration layers combine. It adds no database migration, desired-payload version change,
resource-manifest key, or CLI syntax. Existing templates and stored instance specs keep their
current shape. Operators using only the shipped integrations have no merge-related migration action.

## Reinitialize existing VMs once to establish SSH evidence

Agentworks 0.17 begins recording the configured SSH identity only after a lifecycle operation proves
that identity was written to the VM. It does not infer or synthesize this evidence for VMs created
by an earlier release. Those VMs therefore start with SSH identity evidence not recorded, and
ordinary canonical SSH commands refuse them until a successful reinitialization establishes it.

After upgrading, while the VM still accepts the configured key, run:

```bash
agw vm reinit NAME
```

One successful reinitialization records the evidence required by later ordinary SSH commands. If the
configured key no longer reaches the VM, try `agw vm shell NAME --platform` where the selected
platform supports a recovery transport. Restore the configured public key on the VM, confirm the
configured public and private paths identify the same key, and then rerun `agw vm reinit NAME`.
Platform recovery can still depend on the configured key for some providers. If it cannot connect,
use the provider's native recovery tooling or recreate the VM.

## Rewrite every Git credential source

The released `provider.token` field, its scalar shorthand, and omission default are gone. Each
shipped provider now requires its own structured `provider.source`:

```yaml
# Before
provider:
  name: github
  token: github-pat

# After, same secret-backed behavior
provider:
  name: github
  source:
    mode: secret
    secret: github-pat
```

If the old credential omitted `token`, use an explicit secret source and omit only its inner
reference:

```yaml
provider:
  name: github
  source:
    mode: secret
```

That inner omission retains the `git-token-<credential-name>` default. Azure DevOps uses the same
secret shape beside its required `org`.

The new runtime identity choices are:

```yaml
# GitHub
provider:
  name: github
  source: { mode: gh-cli }

# Azure DevOps
provider:
  name: azdo
  org: example-org
  source: { mode: az-cli }
```

CLI sources use the target admin or agent user's active CLI identity. Agentworks does not install or
authenticate `gh` or `az`. During enabled credential runup, GitHub checks
`gh auth status --active --hostname github.com` and Azure checks `az account show`; either check may
warn without blocking helper installation. GitHub invokes `gh auth token --hostname github.com` at
Git runtime; Azure DevOps uses `az account get-access-token` with resource
`499b84ac-1321-427f-aa17-267ca6975798`, query `accessToken`, and TSV output. The Azure identity must
also have access to the configured organization and repository. The target user owns and may change
the active CLI identity; verify it after authentication changes.

## Cut over the CLI and resource directory together

The 0.16 CLI does not understand `source`, and the 0.17 CLI does not accept the released `token`
shape. Treat the CLI and the complete resource directory as one versioned unit:

1. Stop concurrent Agentworks commands on the workstation.
2. Back up `config.toml` and the complete resources directory at operator-selected locations.
3. Prepare the rewritten resource directory without changing the active copy.
4. Upgrade the CLI and atomically replace the complete active resource directory.
5. Run `agw doctor` and `agw resource list --kind git-credential --include-disabled`.

For the recommended `uv` installation and the default resource path, one concrete cutover is:

```console
$ cp -a ~/.config/agentworks ~/.config/agentworks.pre-0.17
$ cp -a ~/.config/agentworks/resources ~/.config/agentworks-resources.0.17
# Edit every git-credential in ~/.config/agentworks-resources.0.17, then stop other agw commands.
$ uv tool install --upgrade 'agentworks-cli>=0.17,<0.18'
$ mv ~/.config/agentworks/resources ~/.config/agentworks-resources.0.16
$ mv ~/.config/agentworks-resources.0.17 ~/.config/agentworks/resources
$ agw doctor
$ agw resource list --kind git-credential --include-disabled
```

Choose unused backup and staging paths before starting. Operators with a non-default `resource_dir`
should substitute that complete directory; do not move individual manifests into the active tree.

A short validation outage between steps 4 and 5 is expected if those two replacements cannot be
truly atomic. Do not run provisioning or reinitialization while the CLI and active resources are on
opposite sides of the contract.

Before the first successful 0.17 reinit, rollback means restoring both the prior CLI and the
complete prior resource directory. After a successful 0.17 reinit, do not use a downgrade to manage
or repair that user's Git credentials. Fix forward with the new CLI and reinitialize again.

For the example above, pre-reinit rollback is the inverse paired cutover:

```console
# Stop other agw commands first.
$ mv ~/.config/agentworks/resources ~/.config/agentworks-resources.failed-0.17
$ mv ~/.config/agentworks-resources.0.16 ~/.config/agentworks/resources
$ uv tool install --force 'agentworks-cli>=0.16,<0.17'
$ agw doctor
```

The full `~/.config/agentworks.pre-0.17` copy is the recovery backup if more than the resource
directory was changed. Keep it until the canary and affected-user reinitializations succeed.

## Reinitialize every affected Git user

Run `agw vm reinit VM` for affected VM administrators and `agw agent reinit AGENT` for affected
managed agents. Initialization now rebuilds the complete Agentworks-owned Git credential state even
when the desired list is empty. It removes the released Agentworks-owned helper, scope include, and
credential file, when its exact legacy Agentworks helper registration proves ownership, before
installing the new private generation, or leaves a clean empty state.

The removed `agw vm add-git-credential` command has no replacement. Declare credentials on the
appropriate admin or agent template and reinitialize. A generic operator-managed
`credential.helper=store` setting is preserved. Agentworks removes `~/.git-credentials` without
reading it only when `!~/.agentworks-git-cred-helper.sh` was registered at the start of that
reconciliation; otherwise a file or directory at that generic path is left untouched.

If initialization warns about CLI readiness or a CLI-backed helper fails later, authenticate or
repair that target user's CLI identity and retry Git. Reinitialize only after changing a manifest or
generated helper.

## Update third-party Git credential providers

Provider contract version 2 is removed without an adapter. A version-3 provider owns its complete
configuration model and declared references, implements `credential_scopes()` and
`credential_material(ctx)`, and may override the side-effect-free `validate_inputs(ctx)` hook.
Static scopes are generic HTTPS host/path segments; the later operation returns either final
`StoredCredential` material or a first-party `ManagedHelper`. Provider inputs do not determine which
output shape is allowed. See
[`cli/agentworks/capabilities/git_credential/README.md`](../../cli/agentworks/capabilities/git_credential/README.md)
for the permanent author contract.
