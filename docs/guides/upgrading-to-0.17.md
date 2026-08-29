# Upgrading to 0.17

The 0.17 boundary has two upgrade actions. Third-party `harness-integration` authors must move from
contract version 1 to version 2, where merge behavior belongs to the config model the integration
offers. Separately, every VM created before 0.17 needs one successful `agw vm reinit NAME` before
ordinary Agentworks SSH operations will use it. Operators using the shipped `shell`, `claude-code`,
`codex`, or `grok-build` integrations do not need to change integration configuration, but the VM
reinitialization still applies.

**This guide is release-scoped.** It carries both existing VMs and third-party integrations across
the 0.17 boundary. The permanent model and harness author contracts live in
[`cli/agentworks/schema/README.md`](../../cli/agentworks/schema/README.md) and
[`cli/agentworks/capabilities/harness_integration/README.md`](../../cli/agentworks/capabilities/harness_integration/README.md).

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
