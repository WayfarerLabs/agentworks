# Upgrading to 0.17

The 0.17 boundary changes the third-party `harness-integration` author contract from version 1 to
version 2. Merge behavior now belongs to the config model the integration offers. Operators using
the shipped `shell`, `claude-code`, `codex`, or `grok-build` integrations do not need to change
configuration.

**This guide is release-scoped.** It carries third-party integrations across the 0.17 boundary. The
permanent model and harness author contracts live in
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

## No operator-state or CLI migration

This cutover changes only the harness-integration plugin contract and how existing declaration
layers combine. It adds no database migration, desired-payload version change, resource-manifest
key, or CLI syntax. Existing templates and stored instance specs keep their current shape. Operators
using only the shipped integrations have no migration action.
