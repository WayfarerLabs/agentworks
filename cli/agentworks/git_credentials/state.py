"""Build deterministic per-user Git credential state from provider material."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from agentworks.errors import ConfigError

if TYPE_CHECKING:
    from collections.abc import Iterable

    from agentworks.capabilities.git_credential.base import (
        CredentialPayload,
        HttpsCredentialScope,
    )


_HOST_RE = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$")
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]{1,255}$")
_MAX_PATH_SEGMENTS = 64
_MAX_PROTOCOL_FIELD_BYTES = 8192
_MAX_RESPONSE_BYTES = 16384
_MAX_HELPER_BYTES = 131072
_MAX_HINT_BYTES = 1024


@dataclass(frozen=True)
class UserCredentialState:
    """One complete immutable desired generation for a target user."""

    include_content: str
    dispatcher_script: str
    stored_credential_files: tuple[tuple[str, bytes], ...] = field(repr=False)
    managed_helper_files: tuple[tuple[str, bytes], ...] = field(repr=False)

    @property
    def has_credentials(self) -> bool:
        return bool(self.include_content)


@dataclass(frozen=True)
class _Route:
    host: str
    path_prefix: tuple[str, ...]
    credential_id: str
    credential_name: str
    payload_kind: Literal["stored", "helper"]
    failure_hint: str
    declaration_order: int


def _require_line_value(value: str, *, label: str, max_bytes: int) -> str:
    if not isinstance(value, str) or not value:
        raise ConfigError(f"git credential {label} must be a nonempty string")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise ConfigError(f"git credential {label} must be valid UTF-8") from None
    if len(encoded) > max_bytes:
        raise ConfigError(f"git credential {label} exceeds the {max_bytes}-byte limit")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ConfigError(f"git credential {label} contains a control character")
    return value


def _validate_scope(host: str, path_prefix: tuple[str, ...]) -> None:
    if not isinstance(host, str) or not _HOST_RE.fullmatch(host):
        raise ConfigError(f"git credential HTTPS host {host!r} is not a normalized host")
    if host != host.lower():
        raise ConfigError(f"git credential HTTPS host {host!r} must be lowercase")
    if not isinstance(path_prefix, tuple):
        raise ConfigError("git credential HTTPS path_prefix must be a tuple of segments")
    if len(path_prefix) > _MAX_PATH_SEGMENTS:
        raise ConfigError(f"git credential HTTPS path prefix exceeds {_MAX_PATH_SEGMENTS} segments")
    for segment in path_prefix:
        if not isinstance(segment, str) or not _SEGMENT_RE.fullmatch(segment) or segment in {".", ".."}:
            raise ConfigError(f"git credential HTTPS path segment {segment!r} is not normalized")


def validate_credential_scope_claims(
    claims: Iterable[tuple[str, tuple[HttpsCredentialScope, ...]]],
) -> None:
    """Validate static provider scopes and reject every exact collision."""
    from agentworks.capabilities.git_credential.base import HttpsCredentialScope

    claimed: dict[tuple[str, tuple[str, ...]], str] = {}
    for credential_name, scopes in claims:
        _require_line_value(credential_name, label="name", max_bytes=255)
        if not isinstance(scopes, tuple):
            raise ConfigError(f"git credential {credential_name!r} scopes must be a tuple")
        if not scopes:
            raise ConfigError(f"git credential {credential_name!r} returned no HTTPS scope")
        for scope in scopes:
            if not isinstance(scope, HttpsCredentialScope):
                raise ConfigError(f"git credential {credential_name!r} returned an unsupported HTTPS scope")
            _validate_scope(scope.host, scope.path_prefix)
            key = (scope.host, scope.path_prefix)
            previous = claimed.get(key)
            if previous is not None:
                suffix = f"/{'/'.join(scope.path_prefix)}" if scope.path_prefix else ""
                raise ConfigError(
                    f"git credentials {previous!r} and {credential_name!r} both claim "
                    f"scope {scope.host}{suffix}; scopes must be unambiguous"
                )
            claimed[key] = credential_name


def _stored_record(username: str, password: str) -> bytes:
    username = _require_line_value(username, label="username", max_bytes=_MAX_PROTOCOL_FIELD_BYTES)
    password = _require_line_value(password, label="password", max_bytes=_MAX_PROTOCOL_FIELD_BYTES)
    record = f"username={username}\npassword={password}\n\n".encode()
    if len(record) > _MAX_RESPONSE_BYTES:
        raise ConfigError(f"git credential stored response exceeds the {_MAX_RESPONSE_BYTES}-byte limit")
    return record


def _managed_helper(program: bytes, failure_hint: str) -> tuple[bytes, str]:
    if not isinstance(program, bytes) or not program:
        raise ConfigError("git credential managed-helper program must be nonempty bytes")
    if len(program) > _MAX_HELPER_BYTES:
        raise ConfigError(f"git credential managed-helper program exceeds the {_MAX_HELPER_BYTES}-byte limit")
    hint = _require_line_value(failure_hint, label="managed-helper failure hint", max_bytes=_MAX_HINT_BYTES)
    return program, hint


def build_user_credential_state(
    materials: Iterable[tuple[str, tuple[HttpsCredentialScope, ...], CredentialPayload]],
) -> UserCredentialState:
    """Validate provider output and compile one generic Git credential state."""
    from agentworks.capabilities.git_credential.base import (
        ManagedHelper,
        StoredCredential,
    )

    bindings = tuple(materials)
    if not bindings:
        return UserCredentialState("", "", (), ())

    routes: list[_Route] = []
    payloads: dict[str, bytes] = {}

    for declaration_order, (credential_name, scopes, payload) in enumerate(bindings):
        credential_id = f"credential-{declaration_order:04d}"
        if isinstance(payload, StoredCredential):
            payload_kind: Literal["stored", "helper"] = "stored"
            payloads[credential_id] = _stored_record(payload.username, payload.password)
            failure_hint = ""
        elif isinstance(payload, ManagedHelper):
            payload_kind = "helper"
            payloads[credential_id], failure_hint = _managed_helper(
                payload.program,
                payload.failure_hint,
            )
        else:
            raise ConfigError(f"git credential {credential_name!r} returned an unsupported payload")

        for scope in scopes:
            routes.append(
                _Route(
                    scope.host,
                    scope.path_prefix,
                    credential_id,
                    credential_name,
                    payload_kind,
                    failure_hint,
                    declaration_order,
                )
            )

    routes.sort(key=lambda route: (route.host, -len(route.path_prefix), route.path_prefix, route.declaration_order))
    used_ids = {route.credential_id for route in routes}
    stored_files = tuple(
        (f"stored/{credential_id}", payloads[credential_id])
        for credential_id in sorted(used_ids)
        if next(route for route in routes if route.credential_id == credential_id).payload_kind == "stored"
    )
    helper_files = tuple(
        (f"helpers/{credential_id}", payloads[credential_id])
        for credential_id in sorted(used_ids)
        if next(route for route in routes if route.credential_id == credential_id).payload_kind == "helper"
    )
    hosts = sorted({route.host for route in routes})
    include = _render_include(hosts)
    return UserCredentialState(include, _render_dispatcher(routes), stored_files, helper_files)


def _render_include(hosts: list[str]) -> str:
    blocks = ["# Managed by Agentworks. Rebuilt during user initialization.\n"]
    for host in hosts:
        blocks.append(
            f'[credential "https://{host}"]\n'
            "\thelper =\n"
            "\thelper = !~/.agentworks/git-credentials/launch\n"
            "\tuseHttpPath = true\n"
        )
    return "\n".join(blocks)


def _route_body(routes: list[_Route]) -> str:
    cases: list[str] = []
    for route in routes:
        joined = "/".join(route.path_prefix)
        exact = shlex.quote(f"{route.host}|{joined}")
        pattern = f"{exact}*" if not joined else f"{exact}|{shlex.quote(f'{route.host}|{joined}/')}*"
        directory = "stored" if route.payload_kind == "stored" else "helpers"
        cases.append(
            f"        {pattern})\n"
            f"            selected_kind={shlex.quote(route.payload_kind)}\n"
            f"            selected_file={shlex.quote(f'{directory}/{route.credential_id}')}\n"
            f"            selected_name={shlex.quote(route.credential_name)}\n"
            f"            failure_hint={shlex.quote(route.failure_hint)}\n"
            "            return 0\n"
            "            ;;"
        )
    return "\n".join(cases)


def _render_dispatcher(routes: list[_Route]) -> str:
    route_body = _route_body(routes)
    return f"""#!/bin/bash
# Managed by Agentworks. Routes one bounded Git credential request.
set -o pipefail
shopt -s lastpipe
root="$HOME/.agentworks/git-credentials"
op=${{1:-}}
fail_request() {{
    exec 9>&-
    echo "agentworks: invalid or oversized Git credential request" >&2
    exit 1
}}

fail_helper() {{
    exec 9>&-
    printf '%s\\n' "$failure_hint" >&2
    exit 1
}}

case "$op" in
    store) head -c 8193 >/dev/null; exec 9>&-; exit 0 ;;
    get|erase) ;;
    *) exec 9>&-; exit 0 ;;
esac

protocol=""; host=""; qpath=""; username=""; ended=0; lines=0; size=0
set +e
head -c 8193 |
    LC_ALL=C tr '\\000-\\011\\013-\\037\\177' '\\377' |
    iconv -f UTF-8 -t UTF-8 2>/dev/null |
    while IFS= read -r line || [ -n "$line" ]; do
    line_size=$(LC_ALL=C printf '%s' "$line" | wc -c)
    size=$((size + line_size + 1))
    [ "$size" -le 8192 ] || fail_request
    if [ -z "$line" ]; then
        [ "$ended" -eq 0 ] || fail_request
        ended=1
        continue
    fi
    [ "$ended" -eq 0 ] || fail_request
    lines=$((lines + 1)); [ "$lines" -le 64 ] || fail_request
    key=${{line%%=*}}
    [ "$key" != "$line" ] || fail_request
    value=${{line#*=}}
    case "$key" in
        protocol) protocol="$value" ;;
        host) host="$value" ;;
        path) qpath="$value" ;;
        username) username="$value" ;;
        *) : ;;
    esac
done
pipeline_status=("${{PIPESTATUS[@]}}")
set -e
for status in "${{pipeline_status[@]}}"; do
    [ "$status" -eq 0 ] || fail_request
done
path=${{qpath#/}}
path=${{path%.git}}
if [ -n "$path" ]; then
    case "/$path/" in *"//"*|*"/./"*|*"/../"*) fail_request ;; esac
fi

selected_kind=""; selected_file=""; selected_name=""; failure_hint=""
select_credential() {{
    [ "$protocol" = "https" ] || return 1
    case "$host|$path" in
{route_body}
    esac
    return 1
}}

case "$op" in
    get)
        select_credential || exit 0
        if [ "$selected_kind" = "stored" ]; then
            record="$root/current/$selected_file"
            record_size=$(wc -c <"$record") || fail_request
            [ "$record_size" -le {_MAX_RESPONSE_BYTES} ] || fail_request
            iconv -f UTF-8 -t UTF-8 "$record" >/dev/null 2>&1 || fail_request
            clean_size=$(LC_ALL=C tr -d '\\000-\\011\\013-\\037\\177' <"$record" | wc -c)
            [ "$clean_size" -eq "$record_size" ] || fail_request
            exec 8<"$record" || fail_request
            first=""; second=""; blank=""; extra=""
            IFS= read -r first <&8 || fail_request
            IFS= read -r second <&8 || fail_request
            IFS= read -r blank <&8 || fail_request
            [ -z "$blank" ] || fail_request
            if IFS= read -r extra <&8 || [ -n "$extra" ]; then fail_request; fi
            exec 8<&-
            case "$first" in username=*) : ;; *) fail_request ;; esac
            case "$second" in password=*) : ;; *) fail_request ;; esac
            [ -n "${{first#username=}}" ] || fail_request
            [ -n "${{second#password=}}" ] || fail_request
            exec 9>&-
            printf '%s\\n%s\\n\\n' "$first" "$second"
            exit 0
        fi

        exec 8<"$root/current/$selected_file" || fail_helper
        exec 9>&-
        helper_request="protocol=$protocol
host=$host"
        if [ -n "$qpath" ]; then helper_request="$helper_request
path=$qpath"; fi
        if [ -n "$username" ]; then helper_request="$helper_request
username=$username"; fi
        if ! response=$({{ (printf '%s\\n\\n' "$helper_request") 2>/dev/null || true; }} |
            timeout --signal=TERM --kill-after=1s 10s /proc/self/fd/8 get 2>/dev/null |
            timeout --signal=TERM --kill-after=1s 10s head -c {_MAX_RESPONSE_BYTES + 1} |
            LC_ALL=C tr '\\000-\\011\\013-\\037\\177' '\\377' |
            iconv -f UTF-8 -t UTF-8 2>/dev/null); then
            fail_helper
        fi
        exec 8<&-
        response_size=$(LC_ALL=C printf '%s' "$response" | wc -c)
        [ "$response_size" -le {_MAX_RESPONSE_BYTES - 2} ] || fail_helper
        newline='
'
        case "$response" in *"$newline"*) : ;; *) fail_helper ;; esac
        first=${{response%%"$newline"*}}
        second=${{response#*"$newline"}}
        case "$second" in *"$newline"*) fail_helper ;; esac
        case "$first" in username=*) response_username=${{first#username=}} ;; *) fail_helper ;; esac
        case "$second" in password=*) response_password=${{second#password=}} ;; *) fail_helper ;; esac
        [ -n "$response_username" ] && [ -n "$response_password" ] || fail_helper
        printf 'username=%s\\npassword=%s\\n\\n' "$response_username" "$response_password"
        ;;
    erase)
        if select_credential; then
            exec 9>&-
            printf "agentworks: the remote rejected declared git credential '%s'; " "$selected_name" >&2
            printf "inspect its source or active CLI identity\\n" >&2
        fi
        ;;
    *)
        exec 9>&-
        ;;
esac
"""
