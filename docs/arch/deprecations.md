# Compatibility and deprecation

When a transition preserves released input temporarily, keep the compatibility adapter at the
boundary that owns the input, use the canonical form in new examples, and give the adapter an
explicit warning window, removal release, and tracking issue. A compatibility decision for one
consumer does not grant aliases to other consumers.

## Runtime notices and inspection

Use `output.deprecation(message, key=...)` for an invocation-owned notice. A stable feature key
deduplicates repeated checks within the CLI request, including preflight followed by launch. The
request warning scope resets at CLI entry. Calls without a key keep their ordinary emission
behavior. `--no-deprecations` suppresses ambient notices without suppressing correctness or
readiness failures.

Keep notices value-free: identify the deprecated input, replacement, and removal release without
printing operator command text, resolved values, or credentials. Leave declarations intact; runtime
compatibility does not rewrite desired configuration. The owning subsystem supplies its own
inspection facts rather than putting unrelated notices in `Config.deprecation_issues`, which belongs
to settings loading.

`agw doctor` reports deprecation health independently of ambient suppression. Inspect effective
declarations, including stored overrides where applicable, without executing commands, resolving
secrets, contacting VMs, or migrating state. Do not mark unavailable inspection coverage healthy.

## Removal

Remove an adapter in a deliberate reviewed release change after its announced warning window has
actually shipped. Do not change parsing based on the wall clock. The removal change retires the
producer, its compatibility-only tests and current teaching together; generic warning and
suppression mechanisms remain for other transitions. Release notes name the input and replacement so
existing configurations can be updated.

The tracking issue records the owner, supported warning releases, target removal release, and
verification criteria. If the window has not shipped or the schedule changes, update that record and
the warning before removal. A scheduled issue records work; it does not authorize it.

## Active compatibility windows

| Input                                    | Owner and scope                                                                                 | Canonical input                                | Warning releases      | Planned removal                                                             |
| ---------------------------------------- | ----------------------------------------------------------------------------------------------- | ---------------------------------------------- | --------------------- | --------------------------------------------------------------------------- |
| `{{session_name}}`, `{{workspace_name}}` | Shell `command` / `resume_command`, and winning session-scope plaintext env across integrations | `${agw:session_name}`, `${agw:workspace_name}` | CLI 0.20.x and 0.21.x | 0.22.0, [issue #878](https://github.com/WayfarerLabs/agentworks/issues/878) |

Compatibility aliases and canonical references share a single rendering pass. Inserted values remain
data even if they resemble either syntax. Env compatibility follows the winning declaration's
session scope, including inherited and stored session overrides; other env scopes and native
integration fields do not accept the brace aliases. Secrets and discarded entries are not scanned.
The full contract is in [runtime context](runtime-context.md).
