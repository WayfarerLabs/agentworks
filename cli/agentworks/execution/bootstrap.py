"""No-staging Linux bootstrap for the bounded buffered execution proof.

The helper needs Bash, GNU base64/env, and Linux /dev/fd. Account-shell
selection additionally needs getent and id. Its argv is constant: application
arguments, source, environment, directory, and input arrive only through stdin.
This is neither a completion protocol nor an isolation boundary for shell code.
Private helper names use the unexported _agw_ namespace. Its parsing locale is
restored before the application environment is applied.
The parent observes both input producers. SIGPIPE permits a consumer to close
early; other producer failures invalidate delivery even when the payload exits 0.
"""

BOOTSTRAP_ARGV = (
    "/usr/bin/env",
    "-u",
    "BASH_ENV",
    "-u",
    "ENV",
    "-u",
    "SHELLOPTS",
    "-u",
    "BASHOPTS",
    "/bin/bash",
    "--noprofile",
    "--norc",
    "-c",
    r"""
set -o pipefail
for _agw_name in "${!_agw_@}"; do export -n "$_agw_name"; done
unset _agw_name
_agw_inherited_lc_all_set=${LC_ALL+x}
_agw_inherited_lc_all=${LC_ALL-}
export LC_ALL=C
exec 3<&0 7>&1
_agw_token=
_agw_fail() {
    if [[ $_agw_token =~ ^[0-9a-f]{32}$ ]]; then
        printf '%s F\n' "$_agw_token" >&7
    fi
    exit 125
}
_agw_line() { IFS= read -r "$1" <&3 || _agw_fail; }
_agw_decode() {
    local _agw_encoded _agw_decoded
    _agw_line _agw_encoded
    _agw_decoded=$(printf '%s' "$_agw_encoded" | /usr/bin/base64 --decode && printf '.') || _agw_fail
    _agw_decoded=${_agw_decoded%.}
    printf -v "$1" '%s' "$_agw_decoded"
}
_agw_line _agw_version
[[ $_agw_version == AGW1 ]] || _agw_fail
_agw_line _agw_token
[[ $_agw_token =~ ^[0-9a-f]{32}$ ]] || _agw_fail
_agw_line _agw_sensitive
[[ $_agw_sensitive == 0 || $_agw_sensitive == 1 ]] || _agw_fail
_agw_line _agw_kind
[[ $_agw_kind == command || $_agw_kind == script ]] || _agw_fail
_agw_line _agw_shell_choice
_agw_line _agw_count
[[ $_agw_count =~ ^[0-9]+$ && ${#_agw_count} -le 4 && $_agw_count -le 1024 ]] || _agw_fail
_agw_args=()
for ((_agw_i=0; _agw_i<_agw_count; _agw_i++)); do
    _agw_decode _agw_value
    _agw_args+=("$_agw_value")
done
_agw_line _agw_count
[[ $_agw_count =~ ^[0-9]+$ && ${#_agw_count} -le 4 && $_agw_count -le 1024 ]] || _agw_fail
_agw_env_keys=()
_agw_env_values=()
for ((_agw_i=0; _agw_i<_agw_count; _agw_i++)); do
    _agw_line _agw_key
    [[ $_agw_key =~ ^[A-Za-z_][A-Za-z_0-9]*$ ]] || _agw_fail
    case $_agw_key in _agw_*|BASH_ENV|ENV|SHELLOPTS|BASHOPTS|BASH_XTRACEFD) _agw_fail ;; esac
    _agw_decode _agw_value
    _agw_env_keys+=("$_agw_key")
    _agw_env_values+=("$_agw_value")
done
_agw_decode _agw_directory
_agw_line _agw_source
_agw_line _agw_input
printf '%s' "$_agw_source" | /usr/bin/base64 --decode >/dev/null || _agw_fail
printf '%s' "$_agw_input" | /usr/bin/base64 --decode >/dev/null || _agw_fail
if IFS= read -r _agw_remainder <&3 || [[ -n $_agw_remainder ]]; then _agw_fail; fi
exec 3<&-

_agw_encode_stream() {
    local _agw_tag=$1 _agw_chunk
    if /usr/bin/base64 --wrap=76 | while IFS= read -r _agw_chunk; do
        printf '%s %s %s\n' "$_agw_token" "$_agw_tag" "$_agw_chunk" || exit 1
    done; then
        printf '%s %s !\n' "$_agw_token" "$_agw_tag"
    else
        printf '%s F\n' "$_agw_token"
        return 1
    fi
}

_agw_decode_stream() {
    printf '%s' "$1" 2>/dev/null | /usr/bin/env --default-signal=PIPE /usr/bin/base64 --decode
}

_agw_run_payload() {
    local _agw_executable _agw_account _agw_assignment
    if [[ $_agw_kind == script ]]; then
        case $_agw_shell_choice in
            sh) _agw_executable=/bin/sh ;;
            bash) _agw_executable=/bin/bash ;;
            user_default)
                _agw_account=$(/usr/bin/getent passwd "$(/usr/bin/id -u)") || _agw_fail
                _agw_executable=${_agw_account##*:}
                case $_agw_executable in
                    /bin/sh|/usr/bin/sh|/bin/bash|/usr/bin/bash) ;;
                    *) _agw_fail ;;
                esac
                ;;
            *) _agw_fail ;;
        esac
        [[ -x $_agw_executable ]] || _agw_fail
    else
        [[ ${#_agw_args[@]} -gt 0 ]] || _agw_fail
    fi
    [[ -z $_agw_directory ]] || cd -- "$_agw_directory" || _agw_fail
    exec 0<&6 6<&-
    if [[ $_agw_kind == script ]]; then
        set -- "$_agw_executable" /dev/fd/5
    else
        exec 5<&-
        set -- "${_agw_args[@]}"
    fi
    _agw_assignments=()
    for ((_agw_i=0; _agw_i<${#_agw_env_keys[@]}; _agw_i++)); do
        _agw_assignments+=("${_agw_env_keys[_agw_i]}=${_agw_env_values[_agw_i]}")
    done
    if [[ $_agw_inherited_lc_all_set == x ]]; then
        export LC_ALL="$_agw_inherited_lc_all"
    else
        unset LC_ALL
    fi
    for _agw_assignment in "${_agw_assignments[@]}"; do
        export "$_agw_assignment" || {
            printf '%s F\n' "$_agw_token" >&7
            exit 125
        }
    done
    exec 7>&- 8>&- 9>&-
    exec "$@"
}

export -n -f _agw_fail _agw_line _agw_decode _agw_encode_stream _agw_decode_stream _agw_run_payload
exec 5< <(_agw_decode_stream "$_agw_source")
_agw_source_pid=$!
exec 6< <(exec 5<&-; _agw_decode_stream "$_agw_input")
_agw_input_pid=$!
printf '%s B\n' "$_agw_token" >&7
if [[ $_agw_sensitive == 1 ]]; then
    (_agw_run_payload) >/dev/null 2>/dev/null
    _agw_status=$?
else
    exec 8> >(exec 5<&- 6<&-; _agw_encode_stream O >&7)
    _agw_out_pid=$!
    exec 9> >(exec 5<&- 6<&- 8>&-; _agw_encode_stream E >&7)
    _agw_err_pid=$!
    (_agw_run_payload) >&8 2>&9
    _agw_status=$?
fi
exec 5<&- 6<&- 8>&- 9>&-
_agw_producer_failed=0
for _agw_pid in "$_agw_source_pid" "$_agw_input_pid"; do
    wait "$_agw_pid"
    _agw_producer_status=$?
    case $_agw_producer_status in
        0|141) ;;
        *) _agw_producer_failed=1 ;;
    esac
done
if [[ $_agw_sensitive == 0 ]]; then
    wait "$_agw_out_pid" || _agw_producer_failed=1
    wait "$_agw_err_pid" || _agw_producer_failed=1
fi
[[ $_agw_producer_failed == 0 ]] || _agw_fail
exit "$_agw_status"
""",
)
