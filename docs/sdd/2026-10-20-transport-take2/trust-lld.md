# Transport Take 2: Trust Low-Level Design (Slice 1)

- Status: Implemented in slice 1 (PR A); live evidence pending.
- Date: 2026-11-01
- Architecture: [hla.md](./hla.md) D9 (the VM-bound host-key store); requirements: R8
- Plan: [plan.md](./plan.md), slice 1, delivered in PR A with slice 2
- Source baseline: `51b6fec09` (the design PR's head, stacked on `main`)

## Scope

This LLD settles the slice 1 details D9 leaves open:

- the store's paths and alias;
- each trust mode's options;
- the import procedure;
- how the legacy VM-target code binds to the store;
- enrollment, delete and the other lifecycle hooks;
- the managed SSH entries;
- errors, tests and collateral.

Re-establishment through a platform's key read is slice 4's. `trust.py`'s atomic record write is the
primitive slice 4 reuses for it.

## Module layout

| Module                                | Contents                                                                                               |
| ------------------------------------- | ------------------------------------------------------------------------------------------------------ |
| `agentworks/execution/trust.py`       | `TrustMode`, `TrustBinding`, store paths and alias, the option sets, record read and write, enrollment |
| `agentworks/execution/errors.py`      | New: `TrustMissing`; slice 2 adds the rest of D13's errors                                             |
| `agentworks/vms/manager/host_keys.py` | The import service: select VMs, run `ssh-keygen -F`, write records, return typed outcomes              |
| `agentworks/cli/commands/vm.py`       | `import-host-keys`, a thin Typer wrapper over the service                                              |

`trust.py` and `errors.py` import no legacy module (R10), and the existing package guard covers them
as it walks `agentworks.execution`. Legacy modules call them, never the reverse. Slice 2 builds its
VM policy's trust options from `trust.ssh_options`. Slice 2 also deletes the proof carrier, whose
copy of these options (`execution/carriers/ssh/connection.py:105-157` at `fd1256b1f`) is the model
here, so only one copy survives.

## The client

Slice 1 keeps the legacy code's client. D1's resolver and its OpenSSH 8.5 floor arrive in slice 2,
where its VM policy's `-F none` makes them load-bearing. The option sets below therefore avoid
`none` values: before 8.5 a `none` is read as a file name relative to the working directory, so a
file planted there could add a trusted key, which R8 forbids. They name the null device instead,
which every version accepts, and that holds for scp, whose own `ssh` reads the same options.
`ssh-keygen`, which import needs, is the one beside the resolved client and never one on `PATH`, the
rule `ssh-add` follows too (`client.tool_beside_ssh`), so import needs a supported client; a record
is only useful to a client that can connect.

## Store paths and alias

- **Directory.** `CONFIG_DIR / "known_hosts"` (`CONFIG_DIR` is `config/validation.py:26`),
  mode 0700. `trust.begin_enrollment` and the record writer create it, because OpenSSH does not
  create a custom `UserKnownHostsFile`'s parent directory: under `accept-new` a failed write only
  warns.
- **File name.** `vm-<name>`. The store checks each name against its own path- and option-safe
  pattern rather than `NAME_RE` (`naming.py:22`), because persisted names are never re-validated and
  a stricter check would strand an old VM. Names admit Windows reserved device names such as `con`
  and `nul`, and Windows reserves those with any extension too, so a prefix is the safe fix. HLA D9
  is amended to match.
- **Alias.** `HostKeyAlias=agentworks-vm-<name>`. It satisfies the alias rule the proof enforces
  (`execution/carriers/ssh/connection.py:64` at `fd1256b1f`). It is deliberately not derived from
  the configurable `ssh_host_prefix`, so changing that prefix never orphans trust.
- **Record format.** Plain `known_hosts` lines, `<alias> <keytype> <base64>`, one per key. A record
  the import writes is mode 0600; the file `ssh` creates at enrollment takes the umask, which the
  0700 directory makes harmless. Writes go through a temporary file in the directory and a rename,
  reusing `ssh_config.py`'s `_atomic_write` (`ssh_config.py:271`) moved to a shared helper rather
  than adding a third copy.
- **Path quoting.** Option values follow the proof's `_literal_path` rules
  (`execution/carriers/ssh/connection.py:19` at `fd1256b1f`): an absolute path, forward slashes on
  Windows, and a refusal of `%`, `$`, `"`, backslash and control characters. The path is always
  double-quoted. A home directory that breaks those rules is a `ConfigError` naming the path.

## Trust modes and options

`trust.ssh_options(binding) -> tuple[str, ...]` returns `-o` arguments. Every mode pins
`GlobalKnownHostsFile` to the null device (`/dev/null`, or `NUL` on Windows), `UpdateHostKeys=no`,
`HashKnownHosts=no` and `CheckHostIP=no`. They are passed as `-o`, which takes precedence over any
configuration file, so the system's configuration (Debian ships `HashKnownHosts yes`) cannot change
them while legacy code still reads configuration. Other operator configuration, such as a
`KnownHostsCommand`, stays the operator's own trusted input on these legacy paths until slice 2's VM
policy ignores configuration.

| Mode         | `UserKnownHostsFile`           | `HostKeyAlias`         | `StrictHostKeyChecking` |
| ------------ | ------------------------------ | ---------------------- | ----------------------- |
| `VM`         | the store file                 | `agentworks-vm-<name>` | `yes`                   |
| `ENROLL`     | the store file                 | `agentworks-vm-<name>` | `accept-new`            |
| `UNVERIFIED` | the create's provisioning file | none                   | `accept-new`            |

- **`VM`** reads the store when options are built, which is at spawn, not when the transport is
  constructed. With no record it raises and spawns nothing; it never falls back to first use. The
  legacy builder raises `TrustMissing` itself, which is not an `SSHError`. A missing record is a
  state the operator must fix, not an unreachable VM, so no best-effort `except SSHError` handler
  swallows it: otherwise `agent delete` and `workspace delete` would delete their rows and orphan
  the VM-side account and workspace, and `wait_for_reconnect`'s `False` would send `vm start` into a
  Tailscale rejoin that spends the auth key. A loop that reports per VM across several VMs reports
  it for that VM instead of aborting, and `vm delete`'s best-effort teardown carries on as it does
  for any failure. `trust.py` imports nothing legacy.
- **Preflight.** `require_vm_ssh_boundary` (`vms/manager/boundary.py:64`), the SSH precondition the
  session, agent and workspace commands already run before the gate and any secret, checks the
  record, so those commands refuse before any prompt or best-effort step. Backup checks it before it
  starts. The commands that boot a VM or spend a Tailscale key before their first connection check
  the store first too, before resolving a secret or touching the VM: `vm start`
  (`vms/manager/power.py:50-58`, ahead of `_live_vm_boundary`), the activation gate's start
  (`LiveVMNode.auto_start`, `vms/nodes.py:232`), and `vm rekey` (`vms/manager/power.py:344`), which
  would otherwise spend the new key and record the new address before its reconnect check raised.
  The check also covers a VM with no recorded tailnet address, which `_tailscale_rejoin_required`
  sends to a rejoin without probing (`tailscale.py:268-269`).
- **`ENROLL`** is bound only by create (below), and only to Phase A's verify connection.
- **`UNVERIFIED`** uses a file the create owns, because the platform builds the transport inside
  `create` and core keeps using it afterwards, for `probe_debian_release` and Phase A's fallback
  (below).
  - `trust.provisioning_binding(vm_name)`, a context manager yielding the `UNVERIFIED` binding,
    creates it with `mkstemp` (0600, an unpredictable name) inside the private store directory,
    never a shared temporary directory, so no other local user can seed it with a key.
  - Create enters it on its existing cleanup stack, so the file goes on success and on every failure
    path.
  - `ProvisionRequest` carries the binding, path included, so each cloud plugin builds its
    create-time transport from it.
  - A key is accepted once and then pinned for the rest of the create, which matches what
    provisioning does today against the operator's file, without writing that file.

  The integration report on #856 showed that `StrictHostKeyChecking=no` with `/dev/null`, which D9
  named, re-accepts on every connection. HLA D9 is amended to this file.

## Binding the legacy VM targets

`SSHTransport` (`transports/ssh.py:115`) gains a required keyword, `trust`. Its value is either a
`TrustBinding(mode, vm_name, provisioning_file=None)` or the explicit `HOST_TRUST`, today's
`accept-new` against the operator's configuration, which only host connections use. There is no
default, so every construction site chooses (D9's "the code that builds a connection chooses one
explicitly"), and a missed site fails to type-check rather than silently writing the operator's
`known_hosts`. There are twelve sites (`grep SSHTransport(`).

One method, `_host_key_options()`, replaces the hard-coded option in `_ssh_base_args` (186),
`_interactive` (357), `call_streaming` (440-447) and `_scp_base_args` (105). `SSHTarget` (`ssh.py`)
takes a plain, required `host_key_options: tuple[str, ...]`. The rebuild for `input_text`
(`transports/ssh.py:257-265`) passes the resolved tuple, so `ssh.py` never imports `trust`.
`ssh.py`'s own `_scp_base_args` (479) has no VM caller; its module-level `copy_to` has no caller at
all, which slice 8's removal covers.

| Site                                                                                                                                  | Binding                                                |
| ------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------ |
| `transport_for_user`, `transport()`, `agent_transport()` (`transports/__init__.py:58-118`)                                            | `VM`                                                   |
| Phase A's verify connection (`vms/initializer/driver.py:404-423`), which only create runs                                             | `ENROLL`, then `VM` for Phase B (below)                |
| Backup's scp (`vms/backup.py:464`) and workspace copy (`workspaces/manager/copy.py:116`)                                              | the transport's, through a public `host_key_options()` |
| The port forward (`vms/manager/tailscale.py:154`)                                                                                     | `trust.ssh_options(VM binding)`                        |
| Cloud create-time transports (`aws/platform.py:572`, `azure/platform.py:642`, `gcp/platform.py:410`)                                  | `UNVERIFIED`                                           |
| Cloud native transports (`aws/platform.py:718`, `azure/platform.py:824`, `gcp/platform.py:591`)                                       | `VM`                                                   |
| Remote Lima's host connections (`transports/remote_lima.py:59,66`) and Lima's host transport (`capabilities/vm_platform/lima.py:556`) | `HOST_TRUST`                                           |

**Clouds before enrollment.** A cloud's create-time transport is also `result.native_transport`, so
until slice 3 moves attestation core uses it under `UNVERIFIED` for `probe_debian_release`
(`vms/manager/lifecycle.py:591-594`) and for Phase A's `tailscale ip -4` fallback
(`driver.py:394-396`). That is provisioning-time access to a VM not yet in the store, which D9's
`UNVERIFIED` row covers, and it ends in slice 3. Enrollment happens over the tailnet, whose peer
WireGuard already authenticates, rather than over the public internet.

## Enrollment and the other lifecycle hooks

- **Create.**
  1. After `db.insert_vm` commits and the pending VM is marked realized
     (`vms/manager/lifecycle.py:421-438`), `trust.begin_enrollment(vm_name)` removes any stale
     record and creates the directory, so a reused name never inherits a key. A failure there
     unwinds the row.
  2. Phase A's verify connection runs `ENROLL`, so its first success writes the key to the store and
     nowhere else.
  3. After the verify loop (`driver.py:413-423`), Phase A checks that the store now holds a key. If
     it does not, create fails loudly, naming the store path and saying the host key could not be
     recorded; the remedy is delete and recreate, not an import. Phase A then returns a `VM`-bound
     transport for Phase B, so `ENROLL` covers exactly create's first core connection, as D9 says.
  4. Nothing writes the store before Phase A, so the unwind path needs no store cleanup. The
     provisioning file is removed by create's cleanup stack whatever happens (above).
- **A create that fails before enrolling** is kept as failed (`lifecycle.py:595-606`). In PR A its
  remedy is delete and recreate. From PR B, `agw vm recover` re-establishes trust.
- **Delete.** `trust.remove(vm_name)` runs after the row is deleted (`power.py:336`), and even when
  the following `sync_ssh_config` fails, so the store file goes whenever the row goes and a failure
  in between never leaves a live row without its record. It runs on the path that skips backend
  cleanup after a binding failure (`power.py:245-256`), and not when `platform.delete` raises, which
  keeps the row. Logout and session teardown (279-301) still have the store. A cloud VM with no
  record cannot log out of Tailscale, because its native transport is `VM`-bound; delete warns and
  carries on, and the upgrade guide says to import first or remove the node in the admin console.
- **Rekey and address changes** need nothing: the alias, not the address, keys the record.
- **Stop and start** need nothing: the key persists on the guest (incident row I9).

## Import

`agw vm import-host-keys (--all | VM...) [--known-hosts PATH]`, per D9:

1. **Select.** Named VMs must exist, or `NotFoundError`. `--all` takes every VM in the database. A
   VM without a recorded tailnet address is reported and skipped.
2. **Source.** The default is `~/.ssh/known_hosts`; `--known-hosts` names another file. A missing
   default means nothing to import; a missing named file is a `ConfigError`. The source is never
   modified.
3. **Match with OpenSSH itself.** For each VM, the service runs
   `ssh-keygen -F <tailnet address> -f <source>` with the located `ssh-keygen`. OpenSSH then applies
   its own rules, so the result matches what `ssh` would have trusted for that address: wildcards,
   negation, comma lists, hashed `|1|` entries, and bracketed ports (a `[host]:22` line is not
   consulted for a port-22 host).
   - Exit 1 means no match. Exit 255 means the file could not be read.
   - The output's comment lines are dropped.
   - A record line that begins with `@` carries a `@cert-authority` or `@revoked` marker. Any such
     line for the VM means nothing is imported for it.
4. **Write.** The matched records are rewritten under the alias and written atomically. The writer
   validates each key type and blob, so a malformed source line is never reported as imported.
   - A VM with no record gets them all.
   - A VM with a non-empty record is never written. If the matched keys are a subset of the stored
     ones, it reports `already current`; otherwise `conflicts with the stored key`.
   - A union is never written, because that would widen trust.
5. **Report and exit.** One line per VM, from a closed set: `imported (N keys)`, `already current`,
   `no matching entry`, `trust marker present`, `conflicts with the stored key`,
   `no tailnet address`. The command exits non-zero when any selected VM ends without a record, with
   `--all` as well, since each such VM stays unusable.
6. **Refresh the managed SSH entries** (`sync_ssh_config`) when anything was imported.

**When there is nothing to import.** A VM with no matching entry can still be brought in with an
operator-verified file. The operator gathers the keys with `ssh-keyscan`, checks their fingerprints
against the VM's console or provider, and passes the file with `--known-hosts`. The `TrustMissing`
hint and the upgrade guide name this path until PR B's `agw vm recover` adds re-establishment
through the platform.

## Managed SSH entries

`_format_entry` (`ssh_config.py:258`) takes an optional trust block. Its callers pass the VM name,
so the admin and agent entries for a VM with a store record get these lines, indented like the
existing ones:

```text
    HostKeyAlias agentworks-vm-<name>
    UserKnownHostsFile "<store file>"
    StrictHostKeyChecking yes
        GlobalKnownHostsFile /dev/null
    CheckHostIP no
    UpdateHostKeys no
    HashKnownHosts no
```

(`NUL` replaces `/dev/null` on Windows.) An unreadable record or an unsafe store path for one VM
warns and omits that VM's block; it never aborts the rebuild for the others.

The pins match `ssh_options`: the operator's own `ssh` consults only the store for the alias, never
writes to it, and never hashes it. Entries for a VM without a record stay unchanged until slice 8
makes the block unconditional, as D9 decided: the operator's own `ssh` keeps working until they
import. Every change to a record is followed by `sync_ssh_config`: import, enrollment's existing
post-create sync (`vms/initializer/driver.py:235-237`, right after Phase A, reached even when Phase
B fails), delete (`power.py:340`), and, in slice 4, recovery.

## Errors

- **`TrustMissing(StateError)`**, with `entity_kind="vm"`. Its hint names
  `agw vm import-host-keys <vm>`, and `--known-hosts` with a verified file for a VM that has no
  entry. Slice 4 adds `agw vm recover`. The legacy code raises it directly (above).
- **`TrustMismatch`** lands with the slice 2 core.

## Tests

Each test guards behavior, per the development principles and the no-prose-policing rule.

- **`ssh -G`, against the installed client:**
  - each mode's options take effect, including with a configuration file that sets
    `HashKnownHosts yes` and `UpdateHostKeys yes`;
  - quoting holds for a store path containing a space.
- **Options and binding:**
  - `VM` with no record raises `TrustMissing` and spawns nothing;
  - `vm start`, the activation gate and `vm rekey` on a VM with no record raise `TrustMissing`
    before resolving any secret or calling the platform, and `wait_for_reconnect` and
    `native_transport` raise it on the first attempt;
  - the provisioning file is private, survives into core's post-create use (the release probe and
    Phase A's fallback), and is removed on success and on failure;
    - Windows path forms, and import against the platform's own `ssh-keygen`
      (`@pytest.mark.windows`);
  - `require_vm_ssh_boundary`, `vm start`, the activation gate, `vm rekey` and backup refuse a VM
    with no record before resolving a secret or touching it, and `agent delete` and
    `workspace delete` refuse before touching the database;
  - a multi-VM listing reports a missing record for that VM and carries on.

  `SSHTransport`'s required `trust` is a type-checker guarantee, so no test restates it.

- **Enrollment:**
  - the store holds the key after Phase A's verify connection;
  - an empty store after a successful connection fails create;
  - Phase B's transport is bound `VM`.
- **Import:**
  - the source is byte-identical afterwards;
  - a non-empty record is never written, with subset reported as `already current` and anything else
    as `conflicts`;
  - a marker line blocks the VM;
  - exit status for `--all` and for named VMs;
  - a missing named source is a `ConfigError`.

  The pattern semantics are OpenSSH's, so the tests run `ssh-keygen` on sample files rather than
  re-testing its matcher.

- **Lifecycle:** create begins enrollment after the row is realized; delete removes the record
  whenever it removes the row.
- **Managed entries:** the trust block appears exactly when a record exists, the installed client
  resolves it against the store, and one bad record never aborts the rebuild.
- **ENROLL only from create** holds by construction: Phase A is reached only through `bootstrap_vm`
  from create (`vms/initializer/driver.py:161`, `vms/manager/lifecycle.py:643`), and the factories
  bind `VM`. The enrollment tests cover both; nothing pins the call graph.

## Collateral

- **`cli/command-reference.md`:** the VM command table and an `import-host-keys` section.
- **Completions** (`completions/spec.py:220-248`): `("vm.import-host-keys", "names"): "vms"` and
  `("vm.import-host-keys", "known_hosts"): "files"`.
- **Guide topics:** the virtual-machines and troubleshooting topics name the import for a
  `TrustMissing` failure.
- **`docs/guides/upgrading-to-0.20.md`**, assuming PR A ships in the next release:
  - the one-time import;
  - what fails until it runs, and that the operator's own `ssh` keeps working meanwhile;
  - the verified-file path for a VM with no entry;
    - `agw vm import-host-keys --all` as the checklist of VMs still without a record;
  - that a cloud VM deleted without a record stays on the tailnet until its node is removed;
  - that a database restored onto another workstation needs the store directory copied with it,
    since a VM created on 0.20 has its key nowhere else, with the import as the fallback for older
    VMs;
  - that commands acting on a VM refuse without a record, while describe and listing commands report
    it per VM and carry on.

  The guide and the command reference hold the long form; the `TrustMissing` hint and the other docs
  point to them.

- **`cli/agentworks/execution/README.md`:** what the package now provides in production.
- **`vms/initializer/ssh_keys.py:136-140`:** the stale docstring about clearing `known_hosts` by
  hand.
- **The commit** carries a `BREAKING CHANGE:` footer for the import requirement
  (`CONTRIBUTING.md:69`).

## Live evidence (slice 1 cells)

From the plan:

- create;
- the operator's own `ssh` to a managed entry;
- import of an existing VM;
- an address change after rekey;
- a stop and start (I9);
- the operator's `known_hosts` byte-identical after a create.

These run against Remote Lima from macOS and Linux, local Lima from macOS, WSL2, and Proxmox, AWS,
Azure and GCP from at least one workstation each, Windows among them. They include an encrypted key
served by the agent.
