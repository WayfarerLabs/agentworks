"""Inert, strictly validated records contributed to the guide catalog."""

from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass
from enum import Enum
from typing import NewType, cast

from agentworks.errors import NotFoundError, ValidationError
from agentworks.resource_names import MAX_RESOURCE_NAME_LENGTH, RESOURCE_NAME_RE

TopicSlug = NewType("TopicSlug", str)
BlockId = NewType("BlockId", str)
ActionId = NewType("ActionId", str)
FRAMEWORK_HEADING_LABEL = "⟦AGW framework⟧"
FRAMEWORK_HEADING_DELIMITERS = frozenset({"⟦", "⟧"})

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
_MAX_ACTIONS = 32
_MAX_ACTION_BYTES = 128 * 1024
_MAX_ACTION_INPUTS = 32
_MAX_ACTION_TOKENS = 64
_MAX_ACTION_TOKEN_BYTES = 1024
_MAX_ACTION_INPUT_NAME_BYTES = 64
_MAX_ACTION_INPUT_DESCRIPTION_BYTES = 2 * 1024
_MAX_ACTION_PROSE_BYTES = 8 * 1024


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
        return namespace == "plugin" and is_valid_topic_segment(plugin) and is_valid_topic_segment(topic)
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
class ReleaseNotes:
    id: BlockId


@dataclass(frozen=True, slots=True)
class ActionList:
    id: BlockId
    actions: tuple[GuideAction, ...]


@dataclass(frozen=True, slots=True)
class TopicLinks:
    id: BlockId


type GuideBlock = (
    Overview
    | Teaching
    | AgentContract
    | InstanceList
    | State
    | Relationships
    | FieldReference
    | Sample
    | ReleaseNotes
    | ActionList
    | TopicLinks
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
    READ_CONFIGURED_STATE = "read-configured-state"
    EXAMINE_WORKSTATION = "examine-workstation"
    RESOLVE_NAMED_SECRET = "resolve-named-secret"
    CONNECT_NAMED_VM = "connect-named-vm"
    MUTATE_AGENTWORKS = "mutate-agentworks"
    READ_CANONICAL_SOURCE = "read-canonical-source"
    READ_CANONICAL_RELEASE_NOTES = "read-canonical-release-notes"


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
    command: tuple[str, ...] | None
    expected_state: str
    verification: tuple[str, ...] | None
    refusal_alternative: str
    manual_steps: str | None = None


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


def _contains_framework_heading_delimiter(value: str) -> bool:
    """Detect either reserved delimiter after bounded textual decoding."""
    normalized = unicodedata.normalize("NFKC", html.unescape(value))
    return not FRAMEWORK_HEADING_DELIMITERS.isdisjoint(normalized)


def _unescaped(value: str, index: int) -> bool:
    backslashes = 0
    cursor = index - 1
    while cursor >= 0 and value[cursor] == "\\":
        backslashes += 1
        cursor -= 1
    return backslashes % 2 == 0


def _code_ranges(markdown: str) -> tuple[tuple[int, int], ...]:
    """Find closed same-line literal spans delimited by one backtick."""
    ranges: list[tuple[int, int]] = []
    blocked: list[tuple[int, int]] = []
    offset = 0
    fence: tuple[str, int] | None = None
    for line in markdown.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        match = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", body)
        if fence is None:
            if match is not None:
                opening = match.group(1)
                fence = (opening[0], len(opening))
                blocked.append((offset, offset + len(line)))
            offset += len(line)
            continue
        if fence is not None:
            blocked.append((offset, offset + len(line)))
            character, length = fence
            if re.fullmatch(rf" {{0,3}}{re.escape(character)}{{{length},}}[ \t]*", body):
                fence = None
        offset += len(line)

    for line_match in re.finditer(r"[^\r\n]*(?:\r\n|\r|\n|$)", markdown):
        line_start, line_end = line_match.span()
        if line_start == line_end or any(start <= line_start < end for start, end in blocked):
            continue
        cursor = line_start
        while cursor < line_end:
            if markdown[cursor] != "`" or not _unescaped(markdown, cursor):
                cursor += 1
                continue
            run_end = cursor
            while run_end < line_end and markdown[run_end] == "`":
                run_end += 1
            length = run_end - cursor
            if length < 2:
                cursor = run_end
                continue
            closing = markdown.find("`" * length, run_end, line_end)
            block_end = line_end if closing < 0 else closing + length
            blocked.append((cursor, block_end))
            cursor = block_end

    def is_blocked(position: int) -> bool:
        return any(start <= position < end for start, end in blocked)

    index = 0
    while index < len(markdown):
        if (
            markdown[index] != "`"
            or not _unescaped(markdown, index)
            or is_blocked(index)
            or index > 0
            and markdown[index - 1] == "`"
            or index + 1 < len(markdown)
            and markdown[index + 1] == "`"
        ):
            index += 1
            continue
        cursor = index + 1
        while cursor < len(markdown) and markdown[cursor] not in "\r\n":
            if (
                markdown[cursor] != "`"
                or not _unescaped(markdown, cursor)
                or is_blocked(cursor)
                or markdown[cursor - 1] == "`"
                or cursor + 1 < len(markdown)
                and markdown[cursor + 1] == "`"
            ):
                cursor += 1
                continue
            ranges.append((index + 1, cursor))
            index = cursor + 1
            break
        else:
            index += 1
    return tuple(ranges)


def _has_unsafe_expression_marker(markdown: str) -> bool:
    protected = _code_ranges(markdown)
    for marker in _EXPRESSION_MARKERS:
        start = markdown.find(marker)
        while start >= 0:
            end = start + len(marker)
            if not any(range_start <= start and end <= range_end for range_start, range_end in protected):
                return True
            start = markdown.find(marker, start + 1)
    return False


def _reject_framework_heading_delimiter(
    value: str,
    *,
    source: str,
    topic: str,
    path: str,
    error_type: type[GuideContributionError] = GuideContributionError,
) -> str:
    if _contains_framework_heading_delimiter(value):
        raise _error(error_type, source, topic, path, "contains a reserved framework heading delimiter")
    return value


def _reject_expression_delimiter(
    value: str,
    *,
    source: str,
    topic: str | None,
    path: str,
    error_type: type[GuideContributionError] = GuideContributionError,
) -> str:
    """Reject executable-looking markers outside exact inert code spans."""
    if _has_unsafe_expression_marker(value):
        raise _error(error_type, source, topic, path, "contains an expression delimiter")
    return value


def _sequence(
    value: object,
    *,
    source: str,
    topic: str | None,
    path: str,
) -> tuple[object, ...]:
    if type(value) not in {tuple, list}:
        raise _error(GuideContributionError, source, topic, path, "must be a sequence")
    return tuple(cast("tuple[object, ...] | list[object]", value))


def _parse_action_input(value: object, source: str, topic: str | None, path: str) -> ActionInput:
    data = _mapping(
        value,
        {"name", "description", "required", "sensitive"},
        {"name", "description", "required"},
        source=source,
        topic=topic,
        path=path,
    )
    name = _bounded_string(
        data["name"],
        source=source,
        topic=topic,
        path=f"{path}.name",
        max_bytes=_MAX_ACTION_INPUT_NAME_BYTES,
    )
    if not _INPUT_NAME.fullmatch(name):
        raise _error(GuideContributionError, source, topic, f"{path}.name", "is not a valid input name")
    description = _bounded_string(
        data["description"],
        source=source,
        topic=topic,
        path=f"{path}.description",
        max_bytes=_MAX_ACTION_INPUT_DESCRIPTION_BYTES,
    )
    _reject_expression_delimiter(description, source=source, topic=topic, path=f"{path}.description")
    required = data["required"]
    sensitive = data.get("sensitive", False)
    if type(required) is not bool or type(sensitive) is not bool:
        raise _error(GuideContributionError, source, topic, path, "boolean fields must be bool values")
    return ActionInput(name, description, required, sensitive)


def _parse_action_tokens(
    value: object,
    *,
    source: str,
    topic: str | None,
    path: str,
    input_names: set[str],
    sensitive_names: set[str],
) -> tuple[str, ...]:
    values = _sequence(value, source=source, topic=topic, path=path)
    if not values:
        raise _error(GuideContributionError, source, topic, path, "must be non-empty")
    if len(values) > _MAX_ACTION_TOKENS:
        raise _error(GuideContributionError, source, topic, path, f"exceeds the {_MAX_ACTION_TOKENS}-token limit")
    result: list[str] = []
    for index, token in enumerate(values):
        token_path = f"{path}[{index}]"
        normalized = _bounded_string(
            token,
            source=source,
            topic=topic,
            path=token_path,
            max_bytes=_MAX_ACTION_TOKEN_BYTES,
        )
        if not _is_literal_action_token(normalized, input_names):
            raise _error(GuideContributionError, source, topic, token_path, "is not a literal command token")
        if normalized.startswith("$") and normalized[1:] in sensitive_names:
            raise _error(GuideContributionError, source, topic, token_path, "references a sensitive input")
        result.append(normalized)
    return tuple(result)


def _parse_action(value: object, source: str, topic: str | None, path: str) -> GuideAction:
    data = _mapping(
        value,
        {
            "id",
            "precondition",
            "required_inputs",
            "consent",
            "command",
            "expected_state",
            "verification",
            "refusal_alternative",
            "manual_steps",
        },
        {"id", "precondition", "required_inputs", "consent", "expected_state", "refusal_alternative"},
        source=source,
        topic=topic,
        path=path,
    )
    action_id = _string(data["id"], source=source, topic=topic, path=f"{path}.id")
    if not _ID_RE.fullmatch(action_id):
        raise _error(GuideContributionError, source, topic, f"{path}.id", "is not a valid action ID")
    prose: dict[str, str] = {}
    for name in ("precondition", "expected_state", "refusal_alternative"):
        prose[name] = _bounded_string(
            data[name],
            source=source,
            topic=topic,
            path=f"{path}.{name}",
            max_bytes=_MAX_ACTION_PROSE_BYTES,
        )
        _reject_expression_delimiter(prose[name], source=source, topic=topic, path=f"{path}.{name}")
    raw_inputs = _sequence(data["required_inputs"], source=source, topic=topic, path=f"{path}.required_inputs")
    if len(raw_inputs) > _MAX_ACTION_INPUTS:
        raise _error(
            GuideContributionError,
            source,
            topic,
            f"{path}.required_inputs",
            f"exceeds the {_MAX_ACTION_INPUTS}-input limit",
        )
    inputs = tuple(
        _parse_action_input(item, source, topic, f"{path}.required_inputs[{index}]")
        for index, item in enumerate(raw_inputs)
    )
    input_names = {item.name for item in inputs}
    sensitive_names = {item.name for item in inputs if item.sensitive}
    if len(input_names) != len(inputs):
        raise _error(GuideContributionError, source, topic, f"{path}.required_inputs", "contains duplicate names")
    consent_value = data["consent"]
    if type(consent_value) is not str:
        raise _error(GuideContributionError, source, topic, f"{path}.consent", "is not a valid consent boundary")
    try:
        consent = ConsentBoundary(consent_value)
    except ValueError:
        raise _error(
            GuideContributionError, source, topic, f"{path}.consent", "is not a valid consent boundary"
        ) from None
    raw_command = data.get("command")
    raw_manual_steps = data.get("manual_steps")
    if (raw_command is None) == (raw_manual_steps is None):
        raise _error(
            GuideContributionError,
            source,
            topic,
            path,
            "must contain exactly one of command or manual_steps",
        )
    command = (
        None
        if raw_command is None
        else _parse_action_tokens(
            raw_command,
            source=source,
            topic=topic,
            path=f"{path}.command",
            input_names=input_names,
            sensitive_names=sensitive_names,
        )
    )
    manual_steps = (
        None
        if raw_manual_steps is None
        else _bounded_string(
            raw_manual_steps,
            source=source,
            topic=topic,
            path=f"{path}.manual_steps",
            max_bytes=_MAX_ACTION_PROSE_BYTES,
        )
    )
    if manual_steps is not None:
        _reject_expression_delimiter(manual_steps, source=source, topic=topic, path=f"{path}.manual_steps")
    raw_verification = data.get("verification")
    verification = (
        None
        if raw_verification is None
        else _parse_action_tokens(
            raw_verification,
            source=source,
            topic=topic,
            path=f"{path}.verification",
            input_names=input_names,
            sensitive_names=sensitive_names,
        )
    )
    return GuideAction(
        ActionId(action_id),
        prose["precondition"],
        inputs,
        consent,
        command,
        prose["expected_state"],
        verification,
        prose["refusal_alternative"],
        manual_steps,
    )


def _action_bytes(action: GuideAction) -> int:
    values = [
        str(action.id),
        action.precondition,
        action.consent.value,
        action.expected_state,
        action.refusal_alternative,
        action.manual_steps or "",
        *(action.command or ()),
        *(action.verification or ()),
    ]
    for item in action.required_inputs:
        values.extend((item.name, item.description))
    return sum(len(value.encode("utf-8")) for value in values)


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
    data = _mapping(
        value,
        {"type", "id", "markdown", "section", "actions"},
        {"type", "id"},
        source=source,
        topic=topic,
        path=path,
    )
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
        "release-notes",
        "action-list",
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
        _reject_expression_delimiter(
            markdown,
            source=source,
            topic=topic,
            path=f"{path}.markdown",
            error_type=InvalidBlockError,
        )
        _reject_framework_heading_delimiter(
            markdown,
            source=source,
            topic=topic,
            path=f"{path}.markdown",
            error_type=InvalidBlockError,
        )
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
    if discriminator == "action-list":
        exact = _mapping(
            value,
            {"type", "id", "actions"},
            {"type", "id", "actions"},
            source=source,
            topic=topic,
            path=path,
        )
        raw_actions = _sequence(exact["actions"], source=source, topic=topic, path=f"{path}.actions")
        if len(raw_actions) > _MAX_ACTIONS:
            raise _error(
                InvalidBlockError,
                source,
                topic,
                f"{path}.actions",
                f"exceeds the {_MAX_ACTIONS}-action limit",
            )
        actions = tuple(
            _parse_action(item, source, topic, f"{path}.actions[{action_index}]")
            for action_index, item in enumerate(raw_actions)
        )
        ids = [action.id for action in actions]
        if len(ids) != len(set(ids)):
            raise _error(InvalidBlockError, source, topic, f"{path}.actions", "contains duplicate action IDs")
        if sum(_action_bytes(action) for action in actions) > _MAX_ACTION_BYTES:
            raise _error(
                InvalidBlockError,
                source,
                topic,
                f"{path}.actions",
                f"exceeds the {_MAX_ACTION_BYTES}-byte action-data limit",
            )
        return ActionList(BlockId(block_id), actions)
    _mapping(value, {"type", "id"}, {"type", "id"}, source=source, topic=topic, path=path)
    if discriminator == "instance-list":
        return InstanceList(BlockId(block_id))
    if discriminator == "state":
        return State(BlockId(block_id))
    if discriminator == "relationships":
        return Relationships(BlockId(block_id))
    if discriminator == "sample":
        return Sample(BlockId(block_id))
    if discriminator == "release-notes":
        return ReleaseNotes(BlockId(block_id))
    return TopicLinks(BlockId(block_id))


def _action_record_value(value: object) -> dict[str, object]:
    if type(value) is not GuideAction:
        return {"id": object()}
    action = value
    inputs: list[dict[str, object]] = []
    if type(action.required_inputs) is tuple:
        for item in cast("tuple[object, ...]", action.required_inputs):
            if type(item) is ActionInput:
                inputs.append(
                    {
                        "name": item.name,
                        "description": item.description,
                        "required": item.required,
                        "sensitive": item.sensitive,
                    }
                )
            else:
                inputs.append({"name": object()})
    else:
        inputs = cast("list[dict[str, object]]", object())
    command: object = action.command
    if command is not None and type(command) is not tuple:
        command = object()
    verification: object = action.verification
    if verification is not None and type(verification) is not tuple:
        verification = object()
    return {
        "id": action.id,
        "precondition": action.precondition,
        "required_inputs": inputs,
        "consent": action.consent.value if type(action.consent) is ConsentBoundary else object(),
        "command": command,
        "expected_state": action.expected_state,
        "verification": verification,
        "refusal_alternative": action.refusal_alternative,
        "manual_steps": action.manual_steps,
    }


_BLOCK_DISCRIMINATORS: dict[type[GuideBlock], str] = {
    Overview: "overview",
    Teaching: "teaching",
    AgentContract: "agent-contract",
    InstanceList: "instance-list",
    State: "state",
    Relationships: "relationships",
    FieldReference: "field-reference",
    Sample: "sample",
    ReleaseNotes: "release-notes",
    ActionList: "action-list",
    TopicLinks: "topic-links",
}


def _decoded_contribution(value: TopicContribution) -> dict[str, object]:
    """Project a typed contribution into the decoded shape the parser reads.

    Contributions arrive as frozen ``TopicContribution`` records, while the
    contract's rules (byte caps, markdown safety, anchor grammar) are defined
    over the decoded shape, so the catalog converts before it validates.  The
    record's own types carry its shape; what the parser then judges is content.
    """
    anchor = value.anchor
    anchor_value: dict[str, object]
    if isinstance(anchor, ConceptAnchor):
        anchor_value = {"type": "concept", "name": anchor.name}
    elif isinstance(anchor, KindAnchor):
        anchor_value = {"type": "kind", "kind": anchor.kind}
    elif isinstance(anchor, ResourceAnchor):
        anchor_value = {"type": "resource", "kind": anchor.kind, "name": anchor.name}
    else:
        anchor_value = {"type": "implementation", "kind": anchor.kind, "name": anchor.name}
    block_values: list[dict[str, object]] = []
    for block in value.blocks:
        block_value: dict[str, object] = {"type": _BLOCK_DISCRIMINATORS[type(block)], "id": block.id}
        if isinstance(block, (Overview, Teaching, AgentContract)):
            block_value["markdown"] = block.markdown
        elif isinstance(block, FieldReference):
            block_value["section"] = block.section
        elif isinstance(block, ActionList):
            block_value["actions"] = [_action_record_value(action) for action in block.actions]
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
    """Parse one closed decoded contribution without retaining its containers."""
    if type(source) is not str or not source:
        raise _error(GuideContributionError, "<invalid-source>", None, "source", "must be a non-blank string")
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
    authored_bytes = sum(
        len(block.markdown.encode("utf-8"))
        for block in blocks
        if isinstance(block, (Overview, Teaching, AgentContract))
    )
    authored_bytes += sum(
        _action_bytes(action) for block in blocks if isinstance(block, ActionList) for action in block.actions
    )
    if authored_bytes > _MAX_TOPIC_MARKDOWN_BYTES:
        raise _error(
            InvalidBlockError,
            source,
            topic,
            "blocks",
            f"exceeds the {_MAX_TOPIC_MARKDOWN_BYTES}-byte authored-content limit",
        )
    action_ids = [action.id for block in blocks if isinstance(block, ActionList) for action in block.actions]
    if len(action_ids) != len(set(action_ids)):
        raise _error(InvalidBlockError, source, topic, "blocks", "contains duplicate action IDs")
    permitted: dict[type[object], tuple[type[object], ...]] = {
        InstanceList: (ConceptAnchor, KindAnchor, ResourceAnchor, ImplementationAnchor),
        State: (ResourceAnchor, ImplementationAnchor),
        Relationships: (ResourceAnchor, ImplementationAnchor),
        FieldReference: (KindAnchor, ImplementationAnchor),
        Sample: (KindAnchor,),
        ReleaseNotes: (ConceptAnchor,),
    }
    for index, block in enumerate(blocks):
        supported = permitted.get(type(block))
        if supported is not None and not isinstance(anchor, supported):
            raise _error(InvalidBlockError, source, topic, f"blocks[{index}]", "is not supported by this anchor")
        if isinstance(block, ReleaseNotes) and not (
            topic == "concept-release-notes" or topic.startswith("concept-release-notes/v")
        ):
            raise _error(
                InvalidBlockError,
                source,
                topic,
                f"blocks[{index}]",
                "is reserved for core release-note topics",
            )
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
    title = _bounded_string(data["title"], source=source, topic=topic, path="title", max_bytes=_MAX_TITLE_BYTES)
    summary = _bounded_string(data["summary"], source=source, topic=topic, path="summary", max_bytes=_MAX_SUMMARY_BYTES)
    _reject_expression_delimiter(title, source=source, topic=topic, path="title")
    _reject_expression_delimiter(summary, source=source, topic=topic, path="summary")
    _reject_framework_heading_delimiter(title, source=source, topic=topic, path="title")
    _reject_framework_heading_delimiter(summary, source=source, topic=topic, path="summary")
    return TopicContribution(
        TopicSlug(topic),
        title,
        summary,
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
    return _parse_action(_action_record_value(action), source, None, "value")
