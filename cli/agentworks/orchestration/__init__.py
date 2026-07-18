"""Shared orchestration helpers.

A command is a plan over a graph of nodes, and each command's
orchestrator (the bespoke service-layer root) owns its plan. This
package holds the SHARED, contractual surface those orchestrators
drive: the node protocol and the helper semantics. It contains no
orchestrators (those stay bespoke, one per command, in the domain
managers) and no node implementations (those live in their domains:
``vms/nodes.py``, ``sessions/nodes.py``, ...).

Layering rule: modules here depend only on the node protocol, the run
context, and the secrets framework, never on a domain. Domains
implement their own nodes; orchestrators drive both. This mirrors the
capability layering rule and keeps a helper-imports-domain violation
as visible as a capability-imports-domain one.

The package grows lazily, each helper forced by the first migrated
command that needs it; there is no up-front framework:

- ``node``: the ``Readiness`` / ``Node`` protocols, the key
  convention, and the creatable-node ``teardown`` surface.
- ``walk``: the memoized multi-root walk over declared edges.
- ``secrets``: secret union, central resolvability prediction, and the
  scoped delivery reader.
- ``readiness``: the preflight sweep (the runup policy helpers arrive
  with the commands that need them).
- ``activation``: the activation gate (power-state convergence and the
  held-active span).
- ``unwind`` (future): the ``RealizationLog``, first forced by
  ``vm create``'s pending nodes.
"""
