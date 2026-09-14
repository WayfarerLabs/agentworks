"""Bounded YAML metadata loading shared by capture and native inventory."""

from __future__ import annotations

from typing import cast

import yaml

from agentworks.sources import SourceRefError


def parse_metadata(header: str, *, unique_keys: bool = False) -> dict[str, object]:
    """Reject expansion and excessive nesting before constructing YAML values.

    Rule/persona authoring opts into unique string keys. Native inventory observes
    existing files without imposing that authoring schema; standard skills retain
    their existing key policy too. Both use the default key handling.
    """
    if len(header.encode()) > 64 * 1024:
        raise SourceRefError("artifact frontmatter exceeds its size limit")
    try:
        if any(isinstance(token, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken)) for token in yaml.scan(header)):
            raise SourceRefError("artifact metadata cannot contain YAML aliases or anchors")
        depth = 0
        for event in yaml.parse(header):
            if isinstance(event, (yaml.events.MappingStartEvent, yaml.events.SequenceStartEvent)):
                depth += 1
                if depth > 32:
                    raise SourceRefError("artifact metadata exceeds its depth limit")
            elif isinstance(event, (yaml.events.MappingEndEvent, yaml.events.SequenceEndEvent)):
                depth -= 1
        if unique_keys:
            pending = [yaml.compose(header, Loader=yaml.SafeLoader)]
            while pending:
                node = pending.pop()
                if isinstance(node, yaml.MappingNode):
                    keys: set[str] = set()
                    for key, child in node.value:
                        if not isinstance(key, yaml.ScalarNode) or key.tag != "tag:yaml.org,2002:str":
                            raise SourceRefError("artifact metadata keys must be strings")
                        if key.value in keys:
                            raise SourceRefError("artifact metadata cannot contain duplicate keys")
                        keys.add(key.value)
                        pending.append(child)
                elif isinstance(node, yaml.SequenceNode):
                    pending.extend(node.value)
        value = yaml.safe_load(header)
    except yaml.YAMLError:
        raise SourceRefError("invalid artifact YAML frontmatter") from None
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise SourceRefError("artifact frontmatter must be a metadata object")
    return cast("dict[str, object]", value)
