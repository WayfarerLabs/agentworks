# Transport Take 2: Trust Low-Level Design (Slice 1)

- Status: Draft for review. Nothing here is implemented.
- Date: 2026-11-01
- Architecture: [hla.md](./hla.md) D9 (the VM-bound host-key store); requirements: R8
- Plan: [plan.md](./plan.md), slice 1, delivered in PR A with slice 2
- Source baseline: `51b6fec09` (the design PR's head, stacked on `main`)

## Scope

This LLD settles the slice 1 details D9 leaves open:

- the store's paths and alias;
- the option sets for each trust mode;
- the import algorithm;
- how the legacy VM-target factories bind to the store;
- the create, delete and rekey hooks;
- the managed SSH entries;
- the command, errors, tests and collateral.

Re-establishment (D9's recovery hook) is slice 4's, so this slice only leaves room for it.

## Module layout

| Module                                | Contents                                                                                                                       |
| ------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| `agentworks/execution/trust.py`       | `TrustMode`, store paths and alias, the option sets, record lookup, reset and removal, `TrustMissing`. No legacy imports (R10) |
| `agentworks/execution/known_hosts.py` | Parsing a `known_hosts` file and matching its host patterns, including hashed entries and markers. Pure functions              |
| `agentworks/vms/manager/host_keys.py` | The `import_host_keys` service: resolve VMs from the database, call the parser, write store files, report per VM               |
| `agentworks/cli/commands/vm.py`       | The `import-host-keys` command, a thin Typer wrapper over the service                                                          |

Legacy modules call `execution.trust` and never the reverse, as D9 and R10 allow. Each legacy module
this slice edits joins the static legacy-import check (D14) only when slice 2 migrates it; slice 1
adds `trust.py` and `known_hosts.py` to the package guard, which already walks
`agentworks.execution`.

## Store paths and alias

- **Directory.** `CONFIG_DIR / "known_hosts"` (`CONFIG_DIR` is `config/validation.py:26`), created
  0700 on first write. Each file is written 0600.
- **File name.** `vm-<name>`, never the bare name. VM names match `NAME_RE` (`naming.py:22`), which
  admits Windows reserved device names such as `con` and `nul`. Windows reserves those with any
  extension too, so a prefix is the safe fix.
- **Alias.** `HostKeyAlias=agentworks-vm-<name>`. It satisfies the alias rule the proof already
  enforces (`[A-Za-z0-9_][A-Za-z0-9_.:-]*`, `execution/carriers/ssh/connection.py:61`). It is
  deliberately not derived from the configurable `ssh_host_prefix`, so changing that prefix never
  orphans trust.
- **Record format.** Ordinary `known_hosts` lines, `<alias> <keytype> <base64>`, in plain form
  (`HashKnownHosts=no`), one line per key type.
- **Path quoting.** Option values use the proof's `_literal_path` rules
  (`execution/carriers/ssh/connection.py:19`): an absolute path, forward slashes on Windows, and a
  refusal of `%`, `$`, `"`, backslash and control characters. Paths are double-quoted when they
  contain spaces. A home directory that breaks those rules is a typed `ConfigError` at option-build
  time, naming the path.

## Trust modes and options

`trust.ssh_options(mode, vm_name) -> tuple[str, ...]` returns `-o` arguments. Every mode adds
`GlobalKnownHostsFile=none`, `UpdateHostKeys=no` and `CheckHostIP=no`.

| Mode         | `UserKnownHostsFile`             | `HostKeyAlias`         | `StrictHostKeyChecking` | Precondition                    |
| ------------ | -------------------------------- | ---------------------- | ----------------------- | ------------------------------- |
| `VM`         | the store file                   | `agentworks-vm-<name>` | `yes`                   | the file holds at least one key |
| `ENROLL`     | the store file                   | `agentworks-vm-<name>` | `accept-new`            | the file is absent or empty     |
| `UNVERIFIED` | `/dev/null`, or `NUL` on Windows | none                   | `no`                    | none                            |

- **`VM` without a record** raises `TrustMissing` before any process is spawned. It never falls back
  to first use.
- **`ENROLL`** writes the key only to the store, so after the first connection the same transport
  behaves as `VM`. `accept-new` refuses a key that differs from a recorded one, so a later
  connection in the same create cannot replace it.
- **`UNVERIFIED`** is used only for cloud provisioning in this slice. Slice 4 adds the lost-record
  key read.

## Import

`agw vm import-host-keys (--all | VM...) [--known-hosts PATH]`, per D9:

1. **Select VMs.** Named VMs must exist (`NotFoundError` otherwise). `--all` takes every VM in the
   database. A VM without a recorded tailnet address is reported and skipped.
2. **Read the source** without modifying it. The default is `~/.ssh/known_hosts`. A missing default
   file means nothing to import; a missing named file is a `ConfigError`.
3. **Match.** For each line, the host field is a comma-separated pattern list. A VM matches when its
   tailnet address matches a pattern under OpenSSH's rules:
   - `*` and `?` wildcards;
   - a `!` prefix negating, so any negated match excludes the line;
   - `[host]:22` is the same as `host`, and any other port never matches;
   - `|1|salt|hash`, where the hash is HMAC-SHA1 keyed by the base64 salt over the address, computed
     with `hmac` and `hashlib`, needing no SSH library. Comment and blank lines are skipped. A
     malformed line is counted and reported, never fatal.
4. **Markers.** A line marked `@cert-authority` or `@revoked` whose patterns match a VM is reported,
   and nothing is imported for that VM.
5. **Write.** Matching keys become store lines under the alias, de-duplicated by key type and key.
   - When the VM has no store file, the file is written atomically: a temporary file in the
     directory, `fsync`, then rename.
   - When it already holds exactly those keys, nothing changes.
   - When it holds a different key, the VM is reported as a conflict and left alone. An existing
     record is never replaced.
6. **Report.** One line per VM, from a closed set of outcomes: `imported (N keys)`,
   `already current`, `no matching entry`, `trust marker present`, `conflicts with the stored key`,
   `no tailnet address`. The command exits non-zero when any named VM ended in conflict or with a
   marker.
7. **Refresh the managed SSH entries** (`sync_ssh_config`) when anything was imported, so the
   operator's own `ssh` starts verifying against the store.

The service takes a `known_hosts` path and a list of VM rows. It returns typed outcomes, and the
command renders them, per the CLI conventions' service-layer rule.

## Binding the legacy VM targets

`SSHTransport` (`transports/ssh.py:115`) gains one optional field, `trust: TrustBinding | None`.
`TrustBinding` is a frozen dataclass holding the mode and the VM name. A single method,
`_host_key_options()`, replaces the hard-coded `StrictHostKeyChecking=accept-new` in all six
builders:

- `_ssh_base_args` (186);
- `_interactive` (357);
- `call_streaming` (440-447);
- the rebuilt `SSHTarget` used for `input_text` (257-265, through `ssh.py:336`);
- `_scp_base_args` (105, for `copy_to` and `copy_from`).

With `trust=None` it keeps today's `accept-new`. That is the host behavior, which Remote Lima's host
connections (`transports/remote_lima.py`) and `capabilities/vm_platform/lima.py` keep. `SSHTarget`
(`ssh.py`) gains the same field for the `input_text` path.

Call sites:

| Site                                                                                                         | Binding                 |
| ------------------------------------------------------------------------------------------------------------ | ----------------------- |
| `transport_for_user`, `transport()`, `agent_transport()` (`transports/__init__.py:58-118`)                   | `VM`                    |
| Phase A's builder (`vms/initializer/driver.py:404`), which only create runs (`vms/manager/lifecycle.py:627`) | `ENROLL`                |
| Backup's scp (`vms/backup.py:464`), from its transport                                                       | the transport's binding |
| Workspace copy's ssh (`workspaces/manager/copy.py:116`)                                                      | the transport's binding |
| The port forward (`vms/manager/tailscale.py:154`)                                                            | `VM`                    |
| Cloud create-time transports (`aws/platform.py:572`, `azure/platform.py:642`, `gcp/platform.py:410`)         | `UNVERIFIED`            |
| Cloud native transports (`aws/platform.py:718`, `azure/platform.py:824`, `gcp/platform.py:591`)              | `VM`                    |

The backup, copy and forward sites build argv by hand today. They take the options from
`trust.ssh_options` (directly, or through the transport they already hold), so no site re-implements
them.

## Lifecycle hooks

- **Create.** Right after `db.insert_vm` commits (`vms/manager/lifecycle.py:421-436`) and before
  provisioning, `trust.reset(vm)` removes any stale file, so a reused name never inherits a key. The
  create's unwind node removes the file on failure. Phase A's first tailnet connection then runs in
  `ENROLL`.
- **Delete.** After `platform.delete` succeeds, beside the per-VM log cleanup
  (`vms/manager/power.py:318-324`) and before `db.delete_vm` (336), `trust.remove(vm)`. Logout and
  session teardown (279-301) still have the store.
- **Rekey and address changes.** Nothing changes: the alias, not the address, keys the record.
- **Stop and start.** Nothing changes: the key persists on the guest (incident row I9).

## Managed SSH entries

`_format_entry` (`ssh_config.py:247`) gains an optional trust block. Its four callers pass the VM
name, so admin and agent entries for a VM with a store record get:

```text
  HostKeyAlias agentworks-vm-<name>
  UserKnownHostsFile "<store file>"
  StrictHostKeyChecking yes
```

Entries for a VM without a record are unchanged until slice 8 makes the block unconditional. Paths
use the module's existing `_to_ssh_path` form.

## Errors

`execution/errors.py` gains `TrustMissing(StateError)` with `entity_kind="vm"`, naming the VM and
the hint `Run 'agw vm import-host-keys <vm>'`. Slice 4 adds `agw vm recover` to the hint.
`TrustMismatch` lands with the slice 2 core, which reads OpenSSH's diagnostic (HLA D4).

## Tests

Each test guards behavior, per the development principles and the no-prose-policing rule:

- **`known_hosts.py`:** plain, hashed, wildcard, negated, bracketed-port and comma-list patterns;
  markers; malformed lines.
- **Import:**
  - the source file is byte-identical afterwards;
  - an existing different key is never replaced;
  - a marker blocks the VM;
  - `--all` and named VMs behave as specified;
  - a missing named source is a `ConfigError`.
- **Options:** each mode's argv; `VM` with no record raises `TrustMissing` and spawns nothing;
  Windows `NUL` and path forms (`@pytest.mark.windows`).
- **Binding:** each factory and call site in the table produces its binding's options; host targets
  keep `accept-new`; Enroll is reachable only from create.
- **Lifecycle:** create resets and unwind removes; delete removes only after `platform.delete`
  succeeds.
- **Managed entries:** the trust block appears only once a record exists.
- **Real OpenSSH:** extend the proof's installed-client test
  (`tests/execution/carriers/ssh/test_connection.py:187`) so `ssh -G` accepts each mode's options.

## Collateral

- `cli/command-reference.md`: the VM command table and a section for `import-host-keys`.
- Completions: add `("vm.import-host-keys", "names"): "vms"` and the `--known-hosts` path to
  `DYNAMIC_COMPLETIONS` (`completions/spec.py:220-248`).
- Guide topics: the virtual-machines and troubleshooting topics name the import for a `TrustMissing`
  failure.
- `docs/guides/upgrading-to-0.20.md`, assuming PR A ships in the next release: the one-time import,
  what fails until it runs, and that the operator's own `ssh` keeps working meanwhile.
- `cli/agentworks/execution/README.md`: what the package now provides in production.
- The commit carries a `BREAKING CHANGE:` footer for the import requirement (`CONTRIBUTING.md:69`).

## Live evidence (slice 1 cells)

From the plan:

- create;
- the operator's own `ssh` to a managed entry;
- import of an existing VM;
- an address change after rekey.

These run against Remote Lima from macOS and Linux, local Lima from macOS, WSL2, and Proxmox, AWS,
Azure and GCP from at least one workstation each, Windows among them. They include an encrypted key
served by the agent.
