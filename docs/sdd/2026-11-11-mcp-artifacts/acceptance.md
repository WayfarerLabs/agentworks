# MCP artifacts: acceptance matrix

This is a prospective test contract. No row claims completion. Record exact source, native version,
platform, command/mode, observed result, limitations and independently checked cleanup when each row
is exercised. The separate integration-test session owns its backend and resource budget.

| Requirements | Evidence required                                                                                                                                                             |
| ------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| R1-R3        | Schema, inheritance and bundle-order boundaries; inline/file/Git equivalence; strict source rejection; original-owner separation                                              |
| R4           | Actual native env forwarding, custom header and bearer binding with public canaries; missing reference; no resolved value in capture, applied state, argv, inspection or logs |
| R5           | VM default-user route, explicit routes, inactive user/workspace pass-through, two branches without duplicate handling, advisory stale ancestors                               |
| R6           | Each advertised Claude/Codex/Grok user and project path loaded by the real harness; Shell filesystem/index at all scopes                                                      |
| R6 session   | Each advertised opt-in carrier on fresh start and genuine resume, second session isolation, no home/project mutation, default unhandled warning                               |
| R7-R8        | Real shared-document create/adopt/update/remove, foreign conflict, unrelated-key preservation, partial retry, modified obsolete retention, settings mapping coexistence       |
| R8 lifecycle | Activation removal, session deletion/replacement, normal workspace/VM deletion and rehome behavior without a new cleanup prerequisite                                         |
| R9           | Offline public inspection and per-type reporting without source/secret/network/native inventory activity or connection bodies                                                 |
| R10          | v2/v3 retention and v4 validation; unchanged legacy digests/routes/native files; existing workspace usage; standalone installed guides and source/package parity              |

Run all standard gates, required private project/Muntz reviews and the independent PR integration
pipeline. Code-heavy implementation also receives the generic correctness/security lane. Exercise
the actual command surface, including failure and removal paths, rather than only direct helpers.

Native registration, harness loading, successful connection/tool discovery and model use are
different observations. Label them separately. Use a minimal owned deterministic MCP test server for
protocol evidence without paid-model or external service dependencies. A successful model turn is
not required to establish a correctly registered and connected MCP server. Unreachable mechanisms
are reported with their limits; do not infer broad native support from static docs.

Before a final lock, reconcile every FRD requirement with observed evidence or obtain an explicit
scope ruling. Promote surviving contracts into permanent guides and READMEs. The lock cannot ride
the initial artifact-only checkpoint.
