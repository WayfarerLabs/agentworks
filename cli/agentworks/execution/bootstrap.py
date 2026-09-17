"""No-staging Linux bootstrap for the bounded buffered execution proof.

The helper needs Bash, GNU base64, env, and Linux /dev/fd. Account-shell
selection additionally needs getent and id. Its argv is constant: application
arguments, source, environment, directory, and input arrive only through stdin.
This is neither a completion protocol nor an isolation boundary for shell code.
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
export LC_ALL=C
exec 3<&0 7>&1
token=
fail() {
    if [[ $token =~ ^[0-9a-f]{32}$ ]]; then
        printf '%s F\n' "$token" >&7
    fi
    exit 125
}
line() { IFS= read -r "$1" <&3 || fail; }
decode() {
    local encoded decoded
    line encoded
    decoded=$(printf '%s' "$encoded" | /usr/bin/base64 --decode && printf '.') || fail
    decoded=${decoded%.}
    printf -v "$1" '%s' "$decoded"
}
line version
[[ $version == AGW1 ]] || fail
line token
[[ $token =~ ^[0-9a-f]{32}$ ]] || fail
line sensitive
[[ $sensitive == 0 || $sensitive == 1 ]] || fail
line kind
[[ $kind == command || $kind == script ]] || fail
line shell_choice
line count
[[ $count =~ ^[0-9]+$ && ${#count} -le 4 && $count -le 1024 ]] || fail
args=()
for ((i=0; i<count; i++)); do
    decode value
    args+=("$value")
done
line count
[[ $count =~ ^[0-9]+$ && ${#count} -le 4 && $count -le 1024 ]] || fail
env_keys=()
env_values=()
for ((i=0; i<count; i++)); do
    line key
    [[ $key =~ ^[A-Za-z_][A-Za-z_0-9]*$ ]] || fail
    case $key in _agw_*|BASH_ENV|ENV|SHELLOPTS|BASHOPTS|BASH_XTRACEFD) fail ;; esac
    decode value
    env_keys+=("$key")
    env_values+=("$value")
done
decode directory
line source
line input
printf '%s' "$source" | /usr/bin/base64 --decode >/dev/null || fail
printf '%s' "$input" | /usr/bin/base64 --decode >/dev/null || fail
if IFS= read -r remainder <&3 || [[ -n $remainder ]]; then fail; fi
exec 3<&-

encode_stream() {
    local tag=$1 chunk
    if /usr/bin/base64 --wrap=76 | while IFS= read -r chunk; do
        printf '%s %s %s\n' "$token" "$tag" "$chunk" || exit 1
    done; then
        printf '%s %s !\n' "$token" "$tag"
    else
        printf '%s F\n' "$token"
        return 1
    fi
}

run_payload() {
    local executable account _agw_assignment _agw_token=$token
    if [[ $kind == script ]]; then
        case $shell_choice in
            sh) executable=/bin/sh ;;
            bash) executable=/bin/bash ;;
            user_default)
                account=$(/usr/bin/getent passwd "$(/usr/bin/id -u)") || fail
                executable=${account##*:}
                case $executable in
                    /bin/sh|/usr/bin/sh|/bin/bash|/usr/bin/bash) ;;
                    *) fail ;;
                esac
                ;;
            *) fail ;;
        esac
        [[ -x $executable ]] || fail
    else
        [[ ${#args[@]} -gt 0 ]] || fail
    fi
    [[ -z $directory ]] || cd -- "$directory" || fail
    exec 5< <(printf '%s' "$source" | /usr/bin/base64 --decode)
    exec 0< <(printf '%s' "$input" | /usr/bin/base64 --decode)
    if [[ $kind == script ]]; then
        set -- "$executable" /dev/fd/5
    else
        exec 5<&-
        set -- "${args[@]}"
    fi
    assignments=()
    for ((i=0; i<${#env_keys[@]}; i++)); do
        assignments+=("${env_keys[i]}=${env_values[i]}")
    done
    for _agw_assignment in "${assignments[@]}"; do
        export "$_agw_assignment" || {
            printf '%s F\n' "$_agw_token" >&7
            exit 125
        }
    done
    exec 7>&- 8>&- 9>&-
    exec "$@"
}

printf '%s B\n' "$token" >&7
if [[ $sensitive == 1 ]]; then
    (run_payload) >/dev/null 2>/dev/null
    status=$?
else
    exec 8> >(encode_stream O >&7)
    out_pid=$!
    exec 9> >(exec 8>&-; encode_stream E >&7)
    err_pid=$!
    (run_payload) >&8 2>&9
    status=$?
    exec 8>&- 9>&-
    wait "$out_pid" || fail
    wait "$err_pid" || fail
fi
exit "$status"
""",
)
