# Files config

Status: draft, awaiting the operator's acceptance. R3, R5 and R8 to R10 follow the operator's
2026-11-25 and 2026-11-26 rulings.

## Purpose

A secret reaches a VM today only as an environment variable, through an `env` entry such as
`TOKEN: {secret: npm-token}`. Many tools want a file instead: a kubeconfig, a cloud service-account
key, a certificate, a settings file. Values like these are often multiline or larger than an
environment variable can carry, and the tool reads them from a path. Operators write such files by
hand today, which bypasses secret resolution, redaction, inspection and cleanup.

Agentworks should place declared files on a VM: named in the same declarations as environment
variables, with plaintext or secret content, owned by a scope, and inspectable without revealing
what they hold. Files are new, so they are built declaratively from the start: a scope's declared
files take effect when the scope is applied, and a change not yet applied is visible. Once files
have demonstrated that model, `env` moves onto it in a follow-up.

## Operator and stories

The operator declares VMs, workspaces, the admin and agents through templates and instance specs,
and uses secrets from their configured sources.

- Give every agent on a VM a CA certificate under `/usr/local/share/ca-certificates`.
- Give one agent a kubeconfig at `~/.kube/config` whose content is a secret.
- Give a workspace a shared, non-secret tool config at its root, kept out of `git status`.
- Change a secret in the vault, re-apply the owning scope, and have the file follow.
- Remove a file from a template, re-apply, and have the file gone.
- Edit a template's files and see, before applying, which scopes have changes pending and what they
  are.
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
- **Admin and agent:** paths relative to that account's home, private to it. These are two scopes;
  they share only the shape of their place, as they share the harness `user` facet.

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

### R8. Declarative application

Applying a scope collects its file contributions into the scope's applied set and makes the VM match
it. Each entry records its owner (the template or instance spec that declared it), its destination
and mode, and a reference to its content: a plaintext value's digest, a secret's name, or a source
reference, never a secret's value. Between applications, a change to the declarations changes
nothing on the VM. Templates and instance specs are the only producers in this effort.

### R9. Pending changes are visible

When a scope's declared files differ from its applied set, inspection reports the scope as having
unapplied changes, naming each added, changed and removed path and the command that applies them,
and a command that runs against that scope warns once. A secret whose value changed in its source,
under the same name, is not a declaration change; applying the scope picks the new value up.

### R10. A model `env` can follow

The applied set, its comparison with the declarations, and its warnings are not specific to files,
so that `env` can later move onto the same model: an applied set of references, with secrets still
resolved when they are used.

## Out of scope

- Files at session scope, and per-launch delivery to tmpfs.
- Moving `env` onto the declarative model, which follows once files have demonstrated it, and any
  producer other than templates and instance specs.
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
addition.]

[Asked whether to seed the containment follow-up split into two phases, the operator chose:]

> Not yet

[Asked whether to allow secret files at rest at their scope, as an opt-in with ADR 0013 amended and
rotation on re-apply, the operator chose:]

> Yes, at rest at scope

[Asked whether to accept the scope and path table as the FRD's starting shape (VM paths absolute but
outside homes and workspaces; workspace and user paths relative; no session files), the operator
chose:]

> Accept

[Asked whether to design files as owner-attributed contributions with templates as the only producer
in this effort, the operator asked:]

> How much bigger is the full emitter model?

[The lead sized the full model at roughly two to three times this effort, almost all of it in moving
`env` and its producers onto the model, and asked again. The operator answered:]

> Yeah, so I think the tough thing here is that the emitter model should move how changes land.
>
> Today, a change to an env declaration takes effect on the next operation.
>
> Properly done, I think the emitter model would change that. Init would basically collect all env
> (files). In my mind, these remain references where they are that (secrets, files). And then the
> operations would simply use the last applied set of env and files.
>
> While I think this is technically pure, it definitely could get confusing.
>
> This also really starts to raise questions about a move to being even more declarative with
> commands and warnings about unapplied changes.
>
> Thoughts?

### 2026-11-26

[Offered files landing on apply with env kept live and the declarative question left to its own
effort, seeding that effort first, or folding env into this one, the operator chose "Do the
declarative move first" and wrote:]

> Let's do files in the proper declarative model. They're new so we're not breaking anything. And
> then once that is demonstrated, we port env over.
>
> Does that make sense? Please push back if it doesn't.
