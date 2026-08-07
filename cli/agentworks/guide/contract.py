"""Inert, strictly validated records contributed to the guide catalog."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import NewType, cast

from agentworks.errors import NotFoundError, ValidationError
from agentworks.resource_names import MAX_RESOURCE_NAME_LENGTH, RESOURCE_NAME_RE

TopicSlug = NewType("TopicSlug", str)
BlockId = NewType("BlockId", str)
ActionId = NewType("ActionId", str)

_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_TOPIC_SEGMENT_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_EXPRESSION_MARKERS = ("{{", "}}", "${", "<%", "%>", "{%", "%}")
_MAX_TITLE_BYTES = 256
_MAX_SUMMARY_BYTES = 2 * 1024
_MAX_MARKDOWN_BYTES = 64 * 1024
_MAX_TOPIC_MARKDOWN_BYTES = 256 * 1024
_MAX_BLOCKS = 64
_MAX_RELATED_TOPICS = 64
_MAX_TOPIC_SLUG_BYTES = 63 + 1 + MAX_RESOURCE_NAME_LENGTH
_MAX_SECTION_ITEMS = 32
_MAX_SECTION_ITEM_BYTES = 256


class GuideContributionError(ValidationError):
    """Base class for a rejected guide contribution."""

    def __init__(
        self,
        message: str,
        *,
        source: str,
        topic: str | None = None,
        field_path: str = "",
    ) -> None:
        super().__init__(message)
        self.source = source
        self.topic = topic
        self.field_path = field_path


class InvalidTopicSlugError(GuideContributionError):
    """A contribution uses an invalid or mismatched topic slug."""


class InvalidAnchorError(GuideContributionError):
    """A contribution uses an invalid anchor."""


class InvalidBlockError(GuideContributionError):
    """A contribution uses an invalid block."""


class DuplicateTopicError(GuideContributionError):
    """More than one contribution claims the same topic."""


class BrokenTopicLinkError(GuideContributionError):
    """A contribution links to a topic absent from the retained catalog."""


class UnknownGuideTopicError(NotFoundError):
    """A requested guide topic is absent from the catalog."""

    def __init__(self, topic: str, close_matches: tuple[str, ...]) -> None:
        super().__init__(f"unknown guide topic {topic!r}", entity_kind="guide-topic", entity_name=topic)
        self.topic = topic
        self.close_matches = close_matches


class GuideTraversalError(ValidationError):
    """The current guide anchor does not permit a requested traversal."""


def is_valid_topic_segment(value: object) -> bool:
    """Return whether ``value`` is one strict bare guide topic segment."""
    return type(value) is str and _TOPIC_SEGMENT_RE.fullmatch(value) is not None


def is_valid_topic_slug(value: object) -> bool:
    """Return whether ``value`` is one requestable guide topic identity."""
    if type(value) is not str:
        return False
    parts = value.split("/")
    if len(parts) == 1:
        return is_valid_topic_segment(parts[0])
    if len(parts) == 2:
        kind, name = parts
        return (
            is_valid_topic_segment(kind)
            and len(name) <= MAX_RESOURCE_NAME_LENGTH
            and RESOURCE_NAME_RE.fullmatch(name) is not None
        )
    if len(parts) == 3:
        namespace, plugin, topic = parts
        return (
            namespace == "plugin"
            and is_valid_topic_segment(plugin)
            and is_valid_topic_segment(topic)
        )
    return False


@dataclass(frozen=True, slots=True)
class ConceptAnchor:
    name: str


@dataclass(frozen=True, slots=True)
class KindAnchor:
    kind: str


@dataclass(frozen=True, slots=True)
class ResourceAnchor:
    kind: str
    name: str


@dataclass(frozen=True, slots=True)
class ImplementationAnchor:
    kind: str
    name: str


type TopicAnchor = ConceptAnchor | KindAnchor | ResourceAnchor | ImplementationAnchor


@dataclass(frozen=True, slots=True)
class Overview:
    id: BlockId
    markdown: str


@dataclass(frozen=True, slots=True)
class Teaching:
    id: BlockId
    markdown: str


@dataclass(frozen=True, slots=True)
class AgentContract:
    id: BlockId
    markdown: str


@dataclass(frozen=True, slots=True)
class InstanceList:
    id: BlockId


@dataclass(frozen=True, slots=True)
class State:
    id: BlockId


@dataclass(frozen=True, slots=True)
class Relationships:
    id: BlockId


@dataclass(frozen=True, slots=True)
class FieldReference:
    id: BlockId
    section: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Sample:
    id: BlockId


@dataclass(frozen=True, slots=True)
class TopicLinks:
    id: BlockId


type GuideBlock = (
    Overview | Teaching | AgentContract | InstanceList | State | Relationships | FieldReference | Sample | TopicLinks
)


@dataclass(frozen=True, slots=True)
class TopicContribution:
    topic: TopicSlug
    title: str
    summary: str
    anchor: TopicAnchor
    blocks: tuple[GuideBlock, ...]
    related_topics: tuple[TopicSlug, ...] = ()


class ConsentBoundary(Enum):
    NONE = "none"
    READ_CONFIGURED_STATE = "read-configured-state"
    EXAMINE_WORKSTATION = "examine-workstation"
    RESOLVE_NAMED_SECRET = "resolve-named-secret"
    CONNECT_NAMED_VM = "connect-named-vm"
    MUTATE_AGENTWORKS = "mutate-agentworks"


@dataclass(frozen=True, slots=True)
class ActionInput:
    name: str
    description: str
    required: bool
    sensitive: bool = False


@dataclass(frozen=True, slots=True)
class GuideAction:
    id: ActionId
    precondition: str
    required_inputs: tuple[ActionInput, ...]
    consent: ConsentBoundary
    command: tuple[str, ...]
    expected_state: str
    verification: tuple[str, ...] | None
    refusal_alternative: str


def _error(
    error: type[GuideContributionError], source: str, topic: str | None, path: str, detail: str
) -> GuideContributionError:
    return error(
        f"invalid guide contribution from {source}: {path or 'value'} {detail}",
        source=source,
        topic=topic,
        field_path=path,
    )


def _mapping(
    value: object, allowed: set[str], required: set[str], *, source: str, topic: str | None, path: str
) -> dict[str, object]:
    if type(value) is not dict:
        raise _error(GuideContributionError, source, topic, path, "must be an object")
    result = cast("dict[str, object]", value)
    unknown = sorted(set(result) - allowed)
    missing = sorted(required - set(result))
    if unknown:
        raise _error(GuideContributionError, source, topic, f"{path}.{unknown[0]}".lstrip("."), "is unknown")
    if missing:
        raise _error(GuideContributionError, source, topic, f"{path}.{missing[0]}".lstrip("."), "is required")
    return result


def _string(
    value: object,
    *,
    source: str,
    topic: str | None,
    path: str,
    blank: bool = False,
    error_type: type[GuideContributionError] = GuideContributionError,
) -> str:
    if type(value) is not str or (not blank and not value.strip()):
        raise _error(error_type, source, topic, path, "must be a non-blank string")
    return value


def _bounded_string(
    value: object,
    *,
    source: str,
    topic: str | None,
    path: str,
    max_bytes: int,
    blank: bool = False,
    error_type: type[GuideContributionError] = GuideContributionError,
) -> str:
    result = _string(
        value,
        source=source,
        topic=topic,
        path=path,
        blank=blank,
        error_type=error_type,
    )
    if len(result.encode("utf-8")) > max_bytes:
        raise _error(error_type, source, topic, path, f"exceeds the {max_bytes}-byte limit")
    return result


def _parse_anchor(value: object, source: str, topic: str) -> TopicAnchor:
    data = _mapping(value, {"type", "name", "kind"}, {"type"}, source=source, topic=topic, path="anchor")
    discriminator = data["type"]
    if type(discriminator) is not str:
        raise _error(InvalidAnchorError, source, topic, "anchor.type", "has an unknown discriminator")
    if discriminator == "concept":
        data = _mapping(value, {"type", "name"}, {"type", "name"}, source=source, topic=topic, path="anchor")
        return ConceptAnchor(_string(data["name"], source=source, topic=topic, path="anchor.name"))
    if discriminator == "kind":
        data = _mapping(value, {"type", "kind"}, {"type", "kind"}, source=source, topic=topic, path="anchor")
        return KindAnchor(_string(data["kind"], source=source, topic=topic, path="anchor.kind"))
    if discriminator in {"resource", "implementation"}:
        data = _mapping(
            value, {"type", "kind", "name"}, {"type", "kind", "name"}, source=source, topic=topic, path="anchor"
        )
        kind = _string(data["kind"], source=source, topic=topic, path="anchor.kind")
        name = _string(data["name"], source=source, topic=topic, path="anchor.name")
        return ResourceAnchor(kind, name) if discriminator == "resource" else ImplementationAnchor(kind, name)
    raise _error(InvalidAnchorError, source, topic, "anchor.type", "has an unknown discriminator")


def _parse_block(value: object, source: str, topic: str, index: int) -> GuideBlock:
    path = f"blocks[{index}]"
    data = _mapping(value, {"type", "id", "markdown", "section"}, {"type", "id"}, source=source, topic=topic, path=path)
    discriminator = data["type"]
    known = {
        "overview",
        "teaching",
        "agent-contract",
        "instance-list",
        "state",
        "relationships",
        "field-reference",
        "sample",
        "topic-links",
    }
    if type(discriminator) is not str or discriminator not in known:
        raise _error(InvalidBlockError, source, topic, f"{path}.type", "has an unknown discriminator")
    block_id = _string(data["id"], source=source, topic=topic, path=f"{path}.id")
    if not _ID_RE.fullmatch(block_id):
        raise _error(InvalidBlockError, source, topic, f"{path}.id", "is not a valid block ID")
    if discriminator in {"overview", "teaching", "agent-contract"}:
        exact = _mapping(
            value, {"type", "id", "markdown"}, {"type", "id", "markdown"}, source=source, topic=topic, path=path
        )
        markdown = _bounded_string(
            exact["markdown"],
            source=source,
            topic=topic,
            path=f"{path}.markdown",
            max_bytes=_MAX_MARKDOWN_BYTES,
            blank=True,
            error_type=InvalidBlockError,
        )
        if any(marker in markdown for marker in _EXPRESSION_MARKERS):
            raise _error(InvalidBlockError, source, topic, f"{path}.markdown", "contains an expression delimiter")
        if discriminator == "overview":
            return Overview(BlockId(block_id), markdown)
        if discriminator == "teaching":
            return Teaching(BlockId(block_id), markdown)
        return AgentContract(BlockId(block_id), markdown)
    if discriminator == "field-reference":
        exact = _mapping(value, {"type", "id", "section"}, {"type", "id"}, source=source, topic=topic, path=path)
        raw = exact.get("section", ())
        if type(raw) not in {tuple, list}:
            raise _error(InvalidBlockError, source, topic, f"{path}.section", "must contain non-blank strings")
        section = cast("list[object] | tuple[object, ...]", raw)
        if len(section) > _MAX_SECTION_ITEMS:
            raise _error(
                InvalidBlockError,
                source,
                topic,
                f"{path}.section",
                f"exceeds the {_MAX_SECTION_ITEMS}-item limit",
            )
        normalized_items: list[str] = []
        for section_index, item in enumerate(section):
            item_path = f"{path}.section[{section_index}]"
            normalized_items.append(
                _bounded_string(
                    item,
                    source=source,
                    topic=topic,
                    path=item_path,
                    max_bytes=_MAX_SECTION_ITEM_BYTES,
                    error_type=InvalidBlockError,
                ).strip()
            )
        normalized = tuple(normalized_items)
        return FieldReference(BlockId(block_id), normalized)
    _mapping(value, {"type", "id"}, {"type", "id"}, source=source, topic=topic, path=path)
    if discriminator == "instance-list":
        return InstanceList(BlockId(block_id))
    if discriminator == "state":
        return State(BlockId(block_id))
    if discriminator == "relationships":
        return Relationships(BlockId(block_id))
    if discriminator == "sample":
        return Sample(BlockId(block_id))
    return TopicLinks(BlockId(block_id))


def _record_value(value: TopicContribution, source: str) -> dict[str, object]:
    """Copy a programmatic inert record into the same closed decoded shape."""
    anchor = value.anchor
    if type(anchor) is ConceptAnchor:
        anchor_value: dict[str, object] = {"type": "concept", "name": anchor.name}
    elif type(anchor) is KindAnchor:
        anchor_value = {"type": "kind", "kind": anchor.kind}
    elif type(anchor) is ResourceAnchor:
        anchor_value = {"type": "resource", "kind": anchor.kind, "name": anchor.name}
    elif type(anchor) is ImplementationAnchor:
        anchor_value = {"type": "implementation", "kind": anchor.kind, "name": anchor.name}
    else:
        anchor_value = {"type": object()}
    block_values: list[dict[str, object]] = []
    names = {
        Overview: "overview",
        Teaching: "teaching",
        AgentContract: "agent-contract",
        InstanceList: "instance-list",
        State: "state",
        Relationships: "relationships",
        FieldReference: "field-reference",
        Sample: "sample",
        TopicLinks: "topic-links",
    }
    if type(value.blocks) not in {tuple, list}:
        raise _error(InvalidBlockError, source, None, "blocks", "must be a sequence")
    for block in cast("tuple[object, ...] | list[object]", value.blocks):
        discriminator = names.get(type(block))
        if discriminator is None:
            block_values.append({"type": object(), "id": "invalid"})
            continue
        block = cast("GuideBlock", block)
        block_value: dict[str, object] = {"type": discriminator, "id": block.id}
        if isinstance(block, (Overview, Teaching, AgentContract)):
            block_value["markdown"] = block.markdown
        elif isinstance(block, FieldReference):
            block_value["section"] = block.section
        block_values.append(block_value)
    return {
        "topic": value.topic,
        "title": value.title,
        "summary": value.summary,
        "anchor": anchor_value,
        "blocks": block_values,
        "related_topics": value.related_topics,
    }


def parse_topic_contribution(value: object, source: str) -> TopicContribution:
    """Parse one closed contribution without retaining decoded containers."""
    if type(source) is not str or not source:
        raise _error(GuideContributionError, "<invalid-source>", None, "source", "must be a non-blank string")
    if type(value) is TopicContribution:
        value = _record_value(value, source)
    data = _mapping(
        value,
        {"topic", "title", "summary", "anchor", "blocks", "related_topics"},
        {"topic", "title", "summary", "anchor", "blocks"},
        source=source,
        topic=None,
        path="",
    )
    topic = _bounded_string(
        data["topic"],
        source=source,
        topic=None,
        path="topic",
        max_bytes=_MAX_TOPIC_SLUG_BYTES,
        error_type=InvalidTopicSlugError,
    )
    if not is_valid_topic_slug(topic):
        raise _error(InvalidTopicSlugError, source, topic, "topic", "is not a valid topic slug")
    anchor = _parse_anchor(data["anchor"], source, topic)
    expected = anchor.name if isinstance(anchor, ConceptAnchor) else anchor.kind
    if isinstance(anchor, (ResourceAnchor, ImplementationAnchor)):
        expected += f"/{anchor.name}"
    if (
        topic != expected
        or isinstance(anchor, ConceptAnchor)
        and not (topic.startswith("concept-") or topic.startswith("plugin/"))
    ):
        raise _error(InvalidTopicSlugError, source, topic, "anchor", "does not match the topic slug")
    raw_blocks = data["blocks"]
    if type(raw_blocks) not in {tuple, list}:
        raise _error(InvalidBlockError, source, topic, "blocks", "must be a sequence")
    block_values = cast("list[object] | tuple[object, ...]", raw_blocks)
    if len(block_values) > _MAX_BLOCKS:
        raise _error(InvalidBlockError, source, topic, "blocks", f"exceeds the {_MAX_BLOCKS}-block limit")
    blocks = tuple(_parse_block(block, source, topic, index) for index, block in enumerate(block_values))
    ids = [block.id for block in blocks]
    if len(ids) != len(set(ids)):
        raise _error(InvalidBlockError, source, topic, "blocks", "contains duplicate block IDs")
    markdown_bytes = sum(
        len(block.markdown.encode("utf-8"))
        for block in blocks
        if isinstance(block, (Overview, Teaching, AgentContract))
    )
    if markdown_bytes > _MAX_TOPIC_MARKDOWN_BYTES:
        raise _error(
            InvalidBlockError,
            source,
            topic,
            "blocks",
            f"exceeds the {_MAX_TOPIC_MARKDOWN_BYTES}-byte authored-content limit",
        )
    permitted: dict[type[object], tuple[type[object], ...]] = {
        InstanceList: (ConceptAnchor, KindAnchor, ResourceAnchor, ImplementationAnchor),
        State: (ResourceAnchor, ImplementationAnchor),
        Relationships: (ResourceAnchor, ImplementationAnchor),
    }
    for index, block in enumerate(blocks):
        supported = permitted.get(type(block))
        if supported is not None and not isinstance(anchor, supported):
            raise _error(InvalidBlockError, source, topic, f"blocks[{index}]", "is not supported by this anchor")
    raw_related = data.get("related_topics", ())
    if type(raw_related) not in {tuple, list}:
        raise _error(GuideContributionError, source, topic, "related_topics", "must be a sequence")
    related_values = cast("list[object] | tuple[object, ...]", raw_related)
    if len(related_values) > _MAX_RELATED_TOPICS:
        raise _error(
            GuideContributionError,
            source,
            topic,
            "related_topics",
            f"exceeds the {_MAX_RELATED_TOPICS}-link limit",
        )
    related_items: list[TopicSlug] = []
    for index, item in enumerate(related_values):
        item_path = f"related_topics[{index}]"
        related_topic = _bounded_string(
            item,
            source=source,
            topic=topic,
            path=item_path,
            max_bytes=_MAX_TOPIC_SLUG_BYTES,
            error_type=InvalidTopicSlugError,
        )
        if not is_valid_topic_slug(related_topic):
            raise _error(InvalidTopicSlugError, source, topic, item_path, "is not a valid topic slug")
        related_items.append(TopicSlug(related_topic))
    related = tuple(related_items)
    if len(related) != len(set(related)):
        raise _error(GuideContributionError, source, topic, "related_topics", "contains a repeated link")
    return TopicContribution(
        TopicSlug(topic),
        _bounded_string(data["title"], source=source, topic=topic, path="title", max_bytes=_MAX_TITLE_BYTES),
        _bounded_string(data["summary"], source=source, topic=topic, path="summary", max_bytes=_MAX_SUMMARY_BYTES),
        anchor,
        blocks,
        related,
    )


_LITERAL_ACTION_TOKEN_RE = re.compile(r"^(?:[A-Za-z0-9][A-Za-z0-9._:/-]*|--?[a-z0-9][a-z0-9-]*)$")
_INPUT_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _is_literal_action_token(token: object, input_names: set[str]) -> bool:
    if type(token) is not str:
        return False
    if token.startswith("$"):
        return token.count("$") == 1 and token[1:] in input_names
    return _LITERAL_ACTION_TOKEN_RE.fullmatch(token) is not None


def validate_guide_action(action: GuideAction, source: str) -> GuideAction:
    """Validate an inert action and return a normalized frozen copy."""
    if type(source) is not str or not source:
        raise _error(GuideContributionError, "<invalid-source>", None, "source", "must be a non-blank string")
    if type(action) is not GuideAction:
        raise _error(GuideContributionError, source, None, "value", "must be a GuideAction record")
    if type(action.id) is not str or not _ID_RE.fullmatch(action.id):
        raise _error(GuideContributionError, source, None, "id", "is not a valid action ID")
    for field, value in (
        ("precondition", action.precondition),
        ("expected_state", action.expected_state),
        ("refusal_alternative", action.refusal_alternative),
    ):
        if type(value) is not str or not value.strip():
            raise _error(GuideContributionError, source, None, field, "must be a non-blank string")
    if type(action.required_inputs) is not tuple:
        raise _error(GuideContributionError, source, None, "required_inputs", "must be a tuple")
    copied_inputs: list[ActionInput] = []
    for index, item in enumerate(action.required_inputs):
        path = f"required_inputs[{index}]"
        if type(item) is not ActionInput:
            raise _error(GuideContributionError, source, None, path, "must be an ActionInput record")
        if type(item.name) is not str or not _INPUT_NAME.fullmatch(item.name):
            raise _error(GuideContributionError, source, None, f"{path}.name", "is not a valid input name")
        if type(item.description) is not str or not item.description.strip():
            raise _error(GuideContributionError, source, None, f"{path}.description", "must be non-blank")
        if type(item.required) is not bool or type(item.sensitive) is not bool:
            raise _error(GuideContributionError, source, None, path, "boolean fields must be bool values")
        copied_inputs.append(ActionInput(item.name, item.description.strip(), item.required, item.sensitive))
    inputs = tuple(copied_inputs)
    names = {item.name for item in inputs}
    if len(names) != len(inputs):
        raise _error(GuideContributionError, source, None, "required_inputs", "contains duplicate names")
    if type(action.consent) is not ConsentBoundary:
        raise _error(GuideContributionError, source, None, "consent", "must be a ConsentBoundary")
    if type(action.command) is not tuple or not action.command:
        raise _error(GuideContributionError, source, None, "command", "must be a non-empty tuple")
    command = action.command
    for index, token in enumerate(command):
        if not _is_literal_action_token(token, names):
            raise _error(GuideContributionError, source, None, f"command[{index}]", "is not a literal command token")
    if action.verification is not None and type(action.verification) is not tuple:
        raise _error(GuideContributionError, source, None, "verification", "must be a tuple or None")
    verification = action.verification
    if verification is not None:
        if not verification:
            raise _error(GuideContributionError, source, None, "verification", "must be non-empty or None")
        for index, token in enumerate(verification):
            if not _is_literal_action_token(token, names):
                raise _error(
                    GuideContributionError,
                    source,
                    None,
                    f"verification[{index}]",
                    "is not a literal command token",
                )
    return GuideAction(
        ActionId(action.id),
        action.precondition.strip(),
        inputs,
        action.consent,
        tuple(command),
        action.expected_state.strip(),
        None if verification is None else tuple(verification),
        action.refusal_alternative.strip(),
    )
