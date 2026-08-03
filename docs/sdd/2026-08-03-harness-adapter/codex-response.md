# Codex response: harness adapter FRD

## Overall assessment

The rename is warranted. Agentworks currently uses `harness` for its own capability object while
Anthropic and OpenAI use the same word for Claude Code and Codex themselves. The new README section
has to distinguish the two by putting the Agentworks "harness" in quotation marks. That is direct
evidence that the existing vocabulary makes the architecture harder to explain.

The multi-scope expansion makes the mismatch more important. An object that installs, authenticates,
configures, launches, and resumes an external agent harness is an Agentworks adapter or integration
for that harness. It is not itself the harness.

Recommendation: proceed with the rename, keep `workload` for the session-scoped running facet, and
make the adapter or integration identity explicit throughout the resource model and selector
surface.

## Research findings

The leading coding-agent vendors use `harness` for the agent runtime around the model:

- Anthropic defines an agentic harness as the tools, context management, execution environment, and
  loop that turn a model into an agent. It explicitly says that Claude Code is the harness and
  Claude is the model inside it. See the
  [Claude Code glossary](https://code.claude.com/docs/en/glossary).
- OpenAI describes the Codex harness as the shared agent loop and supporting logic behind its Codex
  surfaces. It presents App Server as the integration boundary through which clients embed and drive
  that harness. See
  [Unlocking the Codex harness](https://openai.com/index/unlocking-the-codex-harness/).
- OpenAI uses `tool` for actions available to an agent, including shell execution, function calls,
  and web search. That makes `tool-adapter` vulnerable to a different ambiguity in agent-system
  vocabulary. See the
  [OpenAI Agents SDK tools documentation](https://openai.github.io/openai-agents-python/tools/).

The evidence supports saying that major vendors increasingly use `harness` for the agent runtime. It
does not support saying that the term is unambiguous or universally settled. `Harness` is still used
for evaluation runners, computer-control implementations, and concepts overlapping with scaffolds,
frameworks, SDKs, and orchestrators. A recent paper specifically examines these inconsistent uses:
[What makes a harness a harness](https://arxiv.org/abs/2606.10106).

The FRD should therefore replace claims such as "the industry has settled" and "the term is no
longer ambiguous" with a narrower, well-supported statement, for example:

> Leading coding-agent vendors increasingly use "harness" for the agent loop and runtime surrounding
> a model. Agentworks uses the same word for its integration with that runtime, creating an
> avoidable collision.

The local naming problem remains sufficient justification for the rename even without universal
industry consensus.

## D1: capability kind

Of the candidates presently in the FRD, `harness-adapter` is the strongest. It produces a coherent
three-layer vocabulary:

1. **Model**, such as Claude: model weights and inference behavior.
2. **Harness**, such as Claude Code or Codex: the agent loop, context management, tools, and
   runtime.
3. **Harness adapter**, supplied by Agentworks: the integration that provisions and drives a harness
   within Agentworks.

`tool` and `tool-adapter` should be rejected because `tool` normally means an action callable by an
agent, not the agent runtime itself.

The HLA should also evaluate `harness-integration`. Once the capability owns installation,
authentication, configuration, workspace publication, launch, and resume behavior, `integration` may
describe the full cross-scope responsibility more naturally than the narrower adapter pattern. The
choice should be made by testing both names against resource identifiers, class names, CLI output,
documentation prose, and the multiple-adapters-per-harness case.

## D2: selector field

The FRD's current recommendation to retain the operator-facing `harness:` selector should be
reversed.

The selected resource is not the underlying harness. It is a particular Agentworks adapter or
integration, identified by its own discriminator. That integration in turn declares or selects the
harness it drives. Even if a future integration supports multiple harnesses, the session template
would still select one integration and express the harness as a separate option within that
integration's configuration.

Keeping `harness:` would therefore preserve the same identity collapse that R8 is intended to
remove. The resource kind, selector field, and implementation vocabulary should agree on what is
being selected. For example, if D1 settles on `harness-adapter`, the conceptual shape is:

```yaml
harness-adapter:
  name: claude-code
```

If an adapter can later drive more than one harness, the distinction remains explicit:

```yaml
harness-adapter:
  name: example-provider
  harness: example-harness
```

The precise YAML spelling remains an HLA and migration decision, but the functional requirement
should state that the selector identifies the adapter or integration, not the harness.

## README feedback

The new "Agentworks Is Not a Harness" section makes an important architectural distinction, but it
should rely less on absolute and predictive language. Phrases such as "absolutely critical," "the
writing is on the wall," and custom harnesses "simply won't be able to compete" are not needed to
establish the product boundary.

The vendor evidence supports a more durable claim: Claude Code and Codex already provide native
agent harnesses, their vendors are investing in stable ways to embed them, and Agentworks adds value
by providing consistent, least-privilege environments and lifecycle integration around them.

A concise framing would be:

> Agentworks does not implement the agent loop, context management, or model-facing tool runtime.
> Claude Code, Codex, and similar products provide that harness. Agentworks provides the secure,
> consistent environment around it and the integration needed to install, configure, launch, and
> resume it.

## Migration consequence

The rename is a resource-contract migration, not a documentation-only cleanup. The term currently
appears in capability kinds, class and package names, manifests, session templates, configuration,
migration code, tests, completions, and documentation. R10 is correct to require an explicit
migration path.

Renaming the selector along with the kind increases the migration surface, but it also finishes the
conceptual repair. Carrying `harness:` forward as permanent compatibility sugar would leave the
operator model inconsistent with the resource model. A temporary compatibility shim may still be
appropriate, provided it has a defined removal path and the canonical emitted form uses the new
name.
