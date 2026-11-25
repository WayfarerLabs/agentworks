# Files config

Status: draft, awaiting the operator's acceptance. R5 and R8 are drafted under the lead's
recommended answers to the 2026-11-25 asks, which are still open.

## Purpose

A secret reaches a VM today only as an environment variable, through an `env` entry such as
`TOKEN: {secret: npm-token}`. Many tools want a file instead: a kubeconfig, a cloud service-account
key, a certificate, a settings file. Values like these are often multiline or larger than an
environment variable can carry, and the tool reads them from a path. Operators write such files by
hand today, which bypasses secret resolution, redaction, inspection and cleanup.

Agentworks should place declared files on a VM the way it exports declared environment variables:
named in the same declarations, with plaintext or secret content, owned by a scope, and inspectable
without revealing what they hold.

## Operator and stories

The operator declares VMs, workspaces, the admin and agents through templates and instance specs,
and uses secrets from their configured sources.

- Give every agent on a VM a CA certificate under `/usr/local/share/ca-certificates`.
- Give one agent a kubeconfig at `~/.kube/config` whose content is a secret.
- Give a workspace a shared, non-secret tool config at its root, kept out of `git status`.
- Change a secret in the vault, re-apply the owning scope, and have the file follow.
- Remove a file from a template, re-apply, and have the file gone.
- See which files a scope declares, where they go, and whether they are current, without seeing
  their content.

## Requirements

### R1. A `files` block, a peer of `env`

The declarations that carry `env` for the VM, a workspace, the admin and an agent (templates and
instance specs) also carry `files`. Each entry names a destination path and its content, which is a
plaintext value, a declared secret, or a `source` in the forms agent artifacts already accept (a
workstation file or a Git reference). Sessions carry no `files` in this effort.

### R2. Applied at the declaring scope, with no rollup across scopes

A file belongs to the scope that declares it and is applied as that scope's identity when the scope
is set up: create, reinit, and the scope's repair. Unlike `env`, files do not merge across scopes,
because a file is shared filesystem state rather than one process's view. Within a scope, templates
and the instance spec merge entries by key, as `env` and agent artifacts do.

### R3. Each scope has its own place

- **VM:** absolute paths, but none inside a user's home or a workspace root, which belong to those
  scopes. Written as root, so that system paths are reachable.
- **Workspace:** paths relative to the workspace root, shared with the workspace's group, and
  excluded from Git through the clone's local exclude file.
- **User (the admin or an agent):** paths relative to that user's home, private to that user.

A path that leaves its scope's place is refused when the declaration loads. Because each scope has
its own place, two scopes cannot declare the same file.

### R4. Agentworks changes only what it wrote

Agentworks replaces or removes only a file it recorded writing. A path where a file already exists
that Agentworks did not write is refused, naming the path and the scope, and nothing is changed.
Nothing is written or removed through a symbolic link.

### R5. Secret files are an explicit choice to hold a secret at rest

A secret file holds its value on the VM's disk for the life of its scope, with the scope's private
or group mode. Declaring one is that choice; `env` remains the way to deliver a secret that never
rests on the VM. A secret file's value changes when its scope is re-applied, not at each shell-open.
Its value never appears in logs, records, inspection output or a command line. A secret a scope
needs is checked as resolvable before the scope changes anything, as `env` secrets are.

### R6. Lifecycle

Re-applying a scope writes what changed and removes the files of entries no longer declared.
Deleting the scope removes its files with it (the home, the workspace, or the VM). Inspection
reports a scope whose files are not current, by path, never by value.

### R7. Inspection

The operator can list a scope's declared files with each destination, content kind (plaintext,
secret or source), mode and status, without any command revealing content.

### R8. One contribution model

Each file is recorded as a contribution attributed to the owner that declared it, so that other
producers can contribute files later without a second lifecycle. Templates and instance specs are
the only producer in this effort.

## Out of scope

- Files at session scope, and per-launch delivery to tmpfs.
- Moving `env` onto the contribution model, or any producer other than templates and instance specs.
- Directories, archives, and syncing files back from the VM.
- Changing how `env` delivers secrets.

## Rulings

### 2026-11-25

[The operator raised the need:]

> I'd really like a way to mount a secret in the filesystem as an alternative to the existing env
> var path.
>
> Do you think we have a good way to do that? Obviously we can just write files but that seems
> inelegant.

[Offered a file delivery form of the existing `env` entry, written by the guest launcher to a tmpfs
file under the per-agent `/run/agentworks/...` area, the operator answered:]

> So first, I'd favor a new 'files' config as a peer to 'env'. Like env, there's a need to map both
> secret and non-secret files. This seems a better fit.
>
> And then I'm ok with the /run/agentworks/... placement you propose.

[After an assessment of session cgroups, the operator directed the SDD and set out the scope model:]

> That all makes sense. Let's start the files SDD. The only question I have is where the files go?
> Do we want to be able to mount them in any location accessible to the user? And do we have the
> same scope rollup for files like we do for env? Since these aren't injected, my thinking is that
> they apply immediately at the specified scope (VM/workspace as admin, user/session as user). And
> maybe we limit where workspace and user files can go? And maybe we just disallow them for
> sessions?
>
> And fwiw, my original thinking was that env and files could be "emitted" by other stuff, much like
> agent artifacts. In that model, env and files would just be an emitter. If there's an elegant play
> here, lmk.

[The lead's reading: applying files at their scope replaces per-launch delivery, so the
`/run/agentworks` tmpfs placement, which served per-launch session files, does not apply. VM files
are written as root through the admin's access, since system paths need it. R4 is the lead's
addition. Asks are open on R5 (secret files at rest), R8 (the contribution model) and the R3
placement table.]
