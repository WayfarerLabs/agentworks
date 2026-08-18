# Guide content

Markdown files in a first-party `guide-content` directory are automatically exposed as
`concept-<filename>` topics. An exact direct child named `README.md` is ignored by discovery; use it
for author guidance, not operator content.

## Shell shape

Every concept shell has restricted frontmatter, one level-one ATX heading outside agent-only
regions, and ordinary Markdown:

```markdown
---
description: One concise sentence used by guide list and completion metadata.
index-order: 20
---

# Topic title

Shared guidance for operators and assistant agents.
```

`description` is required. `index-order` is optional; topics that have it appear in the concise
index, ordered by the number and then by slug. The filename must be lowercase kebab-case and owns
the topic identity. Underlined Markdown headings are not supported.

The core directory also contains `_index.md`, the reserved no-topic shell. Other underscore-prefixed
Markdown filenames are invalid.

## Agent-only context

Put genuinely assistant-specific context inside exact, column-zero fences:

```markdown
<!-- agw:agent-only -->

Context addressed directly to the Agentworks assistant agent.

<!-- /agw:agent-only -->
```

Human rendering removes the fenced region. Agent rendering removes only the markers. Shared content
should be useful to either audience and should refer to the operator in the third person. Agent-only
content may address the assistant directly, but the operator remains a third person there too. Use
recommendations sparingly where Agentworks has a consequential opinion; keep the operator's final
authority clear.

## Section imports

An exact, column-zero include imports one heading and its body from trusted packaged Markdown:

```markdown
<!-- agw:include path="_guide_sources/README.md" heading="Core Concepts" heading-offset="0" -->
```

`heading-offset` is optional and shifts every heading in the imported section uniformly; all results
must remain between H2 and H6. Imports are one level only. Imported directives stay inert. Relative
links and images are rewritten to canonical repository URLs.

Controls inside code fences, quoted content, or lists are ordinary Markdown rather than directives.
Keep topics concise, prefer command-owned `--help` for syntax details, and do not add tests that
assert on wording we author ourselves.
