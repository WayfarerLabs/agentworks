# Native lock hierarchy

Status: accepted by the operator on 2026-11-23; R2's wording amended on the operator's suggestion on
2026-11-25.

## Purpose

Agentworks serializes every native change on a VM behind one lock. Two commands that touch unrelated
parts of a VM, such as starting a session in one workspace while creating an agent for another,
cannot run together, and the second fails at once with "another operation is changing native state
on this VM". Even commands that change nothing native take the lock briefly to admit themselves, so
a long operation such as `vm reinit`, `session create` or `agent create` makes `vm shell`,
`session attach` and `session logs` fail for its whole duration.

The lock should follow the shape of what a command changes: a VM, the workspaces and accounts on it,
and the sessions within them. A command should be refused only by an operation that changes
something it depends on.

## Operator and stories

The operator runs several Agentworks commands at once, often from different terminals or agents,
against VMs that hold several workspaces, agents and sessions.

- Start, stop or restart a session while another session on the same VM is being created, when the
  two share neither workspace nor agent.
- Create or reinitialize an agent while sessions of other agents run and change.
- Create, repair or delete a workspace while sessions in other workspaces start and stop.
- Open `vm shell`, `vm exec`, `agent shell`, `session attach` or `session logs` while unrelated
  creates, reinitializations and session changes run on that VM.
- Run a VM-wide operation (`vm reinit`, `vm stop`, `vm delete`, `vm rekey`, `vm recover`) and have
  it refused while anything below it is changing, rather than racing it.
- When refused, learn which operation holds what, without guessing.

## Requirements

### R1. A lock per level

Native state is locked at four levels: the VM; each workspace and each account on it (an agent's
account, or the admin's); and each session. A session belongs to one workspace and to one account
(its agent's, or the admin's in admin mode), so it sits below both. Named consoles, which span
sessions, are locked so that they cannot interleave with changes to the sessions they hold.

### R2. Lower levels block higher levels; unrelated subtrees proceed

An operation locks the entity it changes and marks every entity above it as in use. A change at any
level is refused while a change is held at that entity, below it, or above it. Changes in
non-overlapping subtrees run at the same time, on the same VM and across VMs.

### R3. Fail fast, naming the holder

A refused command fails at once, as today, without waiting. Its error names what holds the lock: the
command, the entity it holds (for example `session/foo` or `workspace/bar`), and how long it has
held it. The hint says to retry when that operation finishes.

### R4. Observation and access are not blocked by unrelated changes

A command that only reads, connects or forwards (`vm shell`, `vm exec`, `agent shell`, `agent exec`,
`session attach`, `session logs`, `vm port-forward` and the like) is refused only by a VM-wide
operation that would break it (stop, delete, rekey, recover, reinit), never by a change below the VM
level. Starting a stopped VM before access still works as today.

### R5. No new races

Finer locking keeps every guarantee the single lock gives today. Operations that edit state shared
across entities (the VM's account and group files, an agent's membership in a workspace's group,
VM-wide files written during session launch, a console's membership) stay correct when they run
concurrently. Mutations that today take no lock (workspace grants, repair, copy, host-key import)
take the lock for what they change.

### R6. No lock held across a prompt

A secret prompt or a confirmation is answered before the lock is taken, wherever the answer does not
depend on state that only the lock makes stable. Where it does, the command says so and holds only
the entity it needs.

### R7. Crash safety and platforms

A process that exits or crashes releases everything it held, as today. The lock works the same on
Linux, macOS and Windows workstations, and across processes and terminals on one workstation.

## Out of scope

- Coordination across workstations. Locks are per workstation and per database, as today.
- Waiting for a lock to free (ruled 2026-11-22: fail fast).
- Changing what any command does beyond the locks it takes and when.

## Rulings

### 2026-11-22

[The operator raised the need:]

> Also, I feel like we really overdid it with the locking.
>
> I get that error all the time (Error: another operation is changing native state on this VM), even
> when the operations are entirely unrelated and would be safe to run concurrently.
>
> Let's tighten that up. And if we haven't already, the lock should be hierarchical, at vm,
> workspace, agent, and session level. Lower levels should block higher levels but unrelated
> subtrees on the same vm should be able to proceed at the same time.

[Asked what a conflicting operation should do, and offered waiting up to a bound, failing fast while
naming the holder, or waiting indefinitely, the operator chose:]

> Fail fast, name holder

[Asked whether to run this as its own small SDD or as a direct change, the operator chose:]

> Own small SDD (Recommended)

### 2026-11-23

[Asked whether to accept requirements R1 to R7 as drafted, with R4 (access not blocked by changes
below the VM), R5 (no new races) and R6 (no lock held across a prompt) named as the lead's
additions, and offered accepting without R4, without R6, or changing something, the operator chose:]

> Accept as drafted

### 2026-11-25

[Asked whether to change R2's "subtrees that share no entity" to "subtrees that share no entity
below the VM", since every subtree on a VM shares the VM, the operator answered:]

> Non-overlapping subtrees?

[The lead adopted that wording in R2.]

[Asked how the design PR should proceed after the tester found one blocking and two important design
defects, and offered one final round then merge, merging and carrying the fixes into the
implementation PRs, or more iteration, the operator chose:]

> One final round, then merge

### 2026-11-26

[Asked whether to fix the two Important findings of the integration report on 6cc1791db before
merge, or merge and carry them into the implementation PRs, the operator chose "Fix now, then merge"
and wrote:]

> And then please just get started on the implementation immediately, stacked on 949. Standard rules
> apply. Up to 3 PR feedback/fix rounds if needed. Always wait for the integration test results
> (which may take several hours) before iterating.

[Asked how to protect a harness tool home that two agents might share, and offered one VM-level lock
for agent harness setup, continuing with a per-directory lock, or refusing shared tool homes, the
operator chose "Refuse shared tool homes" and wrote:]

> What do we actually lose with this?
>
> In my mind, any user-level tool should only write to the user's home subtree. The lock is but one
> symptom of this not being true.
>
> As always the answer is to simplify I think.
