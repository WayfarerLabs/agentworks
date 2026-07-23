#!/usr/bin/env bash

# ============================================================================
# check-locked-sdds.sh: enforce SDD lockfile immutability.
#
# Per the `sdd` skill, once an SDD's `locked.md` lands on `main` the feature
# directory is locked: its artifacts must not be added to or modified. The two
# carve-outs the skill allows are enforced here:
#
#   1. `locked.md` itself may be modified (to record a dated post-lock update).
#   2. The whole SDD may be deleted down to a tombstone: every artifact under
#      the feature dir is removed, leaving only `locked.md` behind. A *partial*
#      deletion (removing some artifacts but keeping others) is NOT allowed.
#
# The lock takes effect only when `locked.md` is present at the comparison base
# (the merge-base with `main`), NOT merely when it appears in the diff. That is
# what lets a feature branch introduce `locked.md` alongside the final SDD
# edits in a single PR: at the merge-base the lockfile does not yet exist, so
# the SDD is still in-flight and freely editable. It is only a *pre-existing*
# lockfile that freezes the directory.
#
# Usage:
#   ./scripts/check-locked-sdds.sh [BASE]
#
#   BASE  Ref/SHA representing `main`'s state to compare against. The script
#         compares against the merge-base of BASE and HEAD. Defaults to
#         `origin/main` (falling back to `main`) for local use. CI passes the
#         PR base SHA (or the push's "before" SHA).
#
# Note: this compares committed history (merge-base..HEAD). Staged or
# working-tree edits that are not yet committed are not seen.
# ============================================================================

set -uo pipefail

SDD_ROOT="docs/sdd"
LOCKFILE="locked.md"

# Run from the repo root so the git pathspecs below resolve regardless of the
# caller's working directory (matches the sibling scripts in this dir).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

# --- Arg parsing ---

BASE_ARG=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help)
            sed -n '/^# Usage:/,/^# ====/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//; /^====/d'
            exit 0
            ;;
        -*)
            echo "Error: unknown argument '$1'. Run with --help for usage." >&2
            exit 1
            ;;
        *)
            if [[ -n "$BASE_ARG" ]]; then
                echo "Error: unexpected extra argument '$1'. Run with --help for usage." >&2
                exit 1
            fi
            BASE_ARG="$1"
            ;;
    esac
    shift
done

# --- Resolve the base ref ---

if [[ -n "$BASE_ARG" ]]; then
    BASE="$BASE_ARG"
elif git rev-parse --verify -q origin/main >/dev/null; then
    BASE="origin/main"
else
    BASE="main"
fi

# A push to a brand-new branch reports an all-zero "before" SHA; there is no
# prior state to compare against, so nothing can be locked. Treat as a no-op.
if [[ "$BASE" =~ ^0+$ ]]; then
    echo "check-locked-sdds: no base commit to compare against (new branch); skipping."
    exit 0
fi

if ! git rev-parse --verify -q "$BASE^{commit}" >/dev/null; then
    echo "Error: base ref '$BASE' is not a valid commit. Fetch it first (CI needs fetch-depth: 0)." >&2
    exit 1
fi

# Compare against the point where this branch diverged from the base, so a
# lockfile that main gained *after* the branch started does not retroactively
# freeze edits the branch made in good faith. The merge-base is the honest
# answer to "what did this branch inherit?".
MERGE_BASE=$(git merge-base "$BASE" HEAD) || {
    echo "Error: could not compute merge-base of '$BASE' and HEAD." >&2
    exit 1
}

# Disable path quoting so comparisons use literal paths (SDD paths are ASCII,
# but this keeps the diff and ls-tree outputs consistent regardless).
GIT=(git -c core.quotepath=false)

# --- Collect changed SDD feature directories ---

# Feature dirs are docs/sdd/<dir>/... ; a bare docs/sdd/<file> has no feature
# dir and is ignored (the sed only prints paths with a component after <dir>).
# Read into an array with a while-loop rather than `mapfile` so the script
# runs on bash 3.2 (stock macOS) as well as the bash 5 CI uses.
changed_dirs=()
while IFS= read -r changed_dir; do
    [[ -n "$changed_dir" ]] && changed_dirs+=("$changed_dir")
done < <(
    "${GIT[@]}" diff --name-only --no-renames "$MERGE_BASE" HEAD -- "$SDD_ROOT/" \
        | sed -n "s#^\($SDD_ROOT/[^/]*\)/.*#\1#p" \
        | sort -u
)

if [[ ${#changed_dirs[@]} -eq 0 ]]; then
    echo "check-locked-sdds: no SDD changes to check."
    exit 0
fi

# --- Evaluate each changed feature directory ---

VIOLATIONS=""
add_violation() {
    VIOLATIONS+="  - $1"$'\n'
}

for dir in "${changed_dirs[@]}"; do
    # The lock is defined by locked.md existing at the merge-base. If it does
    # not exist there, the SDD is still in-flight (even if this PR adds the
    # lockfile) and is freely editable.
    if ! "${GIT[@]}" cat-file -e "$MERGE_BASE:$dir/$LOCKFILE" 2>/dev/null; then
        continue
    fi

    lockfile_deleted=0
    added_or_modified=0
    deleted_nonlock=""   # newline-delimited paths deleted (excluding lockfile)

    while IFS=$'\t' read -r status path; do
        [[ -z "$status" ]] && continue
        if [[ "$path" == "$dir/$LOCKFILE" ]]; then
            # Updating the lockfile is allowed; deleting it removes the tombstone.
            if [[ "$status" == D* ]]; then
                lockfile_deleted=1
            fi
            continue
        fi

        case "$status" in
            D*)
                deleted_nonlock+="$path"$'\n'
                ;;
            *)
                # A, M, T, etc. on a non-lockfile artifact: adding or modifying
                # a locked SDD.
                added_or_modified=1
                add_violation "$path: locked SDD artifact was added or modified (status $status)."
                ;;
        esac
    done < <("${GIT[@]}" diff --name-status --no-renames "$MERGE_BASE" HEAD -- "$dir/")

    if [[ "$lockfile_deleted" -eq 1 ]]; then
        add_violation "$dir/$LOCKFILE: the lockfile must remain as a tombstone; deleting it is not allowed."
    fi

    # If anything under the dir is being deleted, it must be a *full* wipe:
    # every artifact at the base (other than locked.md) must be gone. A partial
    # deletion is not allowed. Skip this scan when the dir already has an
    # add/modify violation: the dir is plainly not a clean tombstone wipe, and
    # flagging untouched siblings as "still present" would just be noise.
    if [[ -n "$deleted_nonlock" && "$added_or_modified" -eq 0 ]]; then
        while IFS= read -r base_file; do
            [[ -z "$base_file" ]] && continue
            [[ "$base_file" == "$dir/$LOCKFILE" ]] && continue
            if ! grep -Fxq "$base_file" <<<"$deleted_nonlock"; then
                add_violation "$dir: partial deletion of a locked SDD. Delete the entire feature directory (keeping $LOCKFILE) or nothing; '$base_file' is still present."
            fi
        done < <("${GIT[@]}" ls-tree -r --name-only "$MERGE_BASE" -- "$dir/")
    fi
done

# --- Report ---

if [[ -n "$VIOLATIONS" ]]; then
    echo "ERROR: locked SDD artifacts were modified (see the sdd skill's lockfile rules)." >&2
    echo "" >&2
    printf '%s' "$VIOLATIONS" >&2
    echo "" >&2
    echo "A locked SDD (its locked.md already on main) may only have its lockfile updated," >&2
    echo "or be deleted in full down to the locked.md tombstone." >&2
    exit 1
fi

echo "check-locked-sdds: no locked SDDs were improperly modified."
exit 0
