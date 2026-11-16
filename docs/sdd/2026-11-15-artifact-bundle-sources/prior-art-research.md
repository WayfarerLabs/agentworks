# Artifact bundle sources: prior art

Research snapshot: 2026-11-15. These findings inform the draft; they do not authenticate
requirements.

## Findings and consequences

- [Agent Plugins 1.0](https://github.com/agentplugins/agent-plugins-spec/blob/main/spec/1.0.0.md) is
  published. It uses `plugin.json`, immediate skill directories and `mcp.json`. Its portable
  component types are skills and MCP, not Agentworks' five types. Use a named import profile rather
  than relabeling the internal bundle model or inventing portable rules/personas.
- The same specification gives stdio servers a package working directory and package-root/data
  variables. Those are runtime semantics, not just acquisition paths. Preserve them or reject the
  unsupported import; a temporary source path cannot become a native server's durable location.
- [Agent Plugins 1.1](https://github.com/agentplugins/agent-plugins-spec/blob/main/spec/1.1.0.md) is
  a working draft adding hooks. It is not the published compatibility baseline for this effort.
- [Rulesync](https://github.com/dyoshikawa/rulesync) demonstrates a shared authoring model
  translated for multiple agents. Retain our normalized pipeline; directory ingestion does not
  require a Rulesync process or accepting its full schema.

## Current repository, main 432766fa

| Existing boundary                                | Evidence                                 | Design consequence                                                               |
| ------------------------------------------------ | ---------------------------------------- | -------------------------------------------------------------------------------- |
| Five explicit maps and named bundle inheritance  | `cli/agentworks/artifacts/bundle.py:35`  | Add source ingestion; preserve map composition and owner identity                |
| Local/Git bounded acquisition                    | `cli/agentworks/package_sources.py:123`  | Reuse the existing source grammar, limits and revision cache                     |
| Git reads committed objects                      | `cli/agentworks/package_sources.py:403`  | Resolve once; no executable checkout is required                                 |
| File capture and canonical normalization         | `cli/agentworks/artifacts/capture.py:59` | Directory discovery feeds the current canonical entry validators                 |
| MCP file source selects one named server         | `cli/agentworks/artifacts/mcp.py:165`    | Discovery needs all-server selection while keeping the same definition validator |
| Local relative source currently uses process cwd | `cli/agentworks/package_sources.py:177`  | Define migration for declaration-relative bases explicitly                       |

PR 908 adds direct owner declarations separately. This design can be reviewed on current main;
implementation must refresh to the merged result before relying on its shared declaration schema.

## Claims not relied on

An external package format is not our wire format. No verified claim is made that every current
harness implements Agent Plugins, that every plugin MCP definition fits the current canonical model,
or that directory scanning alone preserves package execution semantics. A Git manifest does not
avoid enumeration unless it can select directories. Repo acquisition is not authorization to execute
code or read the operator's workstation through remote-authored absolute paths.

## Open research at the design gate

Measure the minimum useful Agent Plugins MCP profile against current native adapters before claiming
support. Establish whether preserving package files/data fits this effort or should follow an
explicitly limited first profile. Check source-origin metadata through resource inheritance before
specifying the local-relative-path transition.
