#!/usr/bin/env bash
# Publish a tagged version's release notes to GitHub and gitea.
#
#   tools/release.sh <tag> [--dry-run]
#
# The notes are that version's section of CHANGELOG.md (the "## [x.y.z]" heading
# down to the next "## "), so the changelog stays the one place they are written.
#
# Releases are not git refs, so gitea's push mirror cannot carry them; this does
# both sides with the same text. Run it after the tag has reached GitHub through
# the mirror -- gh is told to verify the tag, so it refuses rather than creating
# one. A side that already has the release is left alone, so a re-run is safe.
#
# Needs: gh logged in to GitHub; GITEA_TOKEN with write access to the repo.
set -euo pipefail

GITHUB_OWNER=${GITHUB_OWNER:-lime-red}
GITEA_URL=${GITEA_URL:-https://gitea-hl.taild204f.ts.net}
GITEA_OWNER=${GITEA_OWNER:-lime}

usage() { echo "usage: $0 <tag> [--dry-run]" >&2; exit 2; }
[[ $# -ge 1 ]] || usage
tag=$1
dry_run=false
[[ ${2:-} == --dry-run ]] && dry_run=true

root=$(cd "$(dirname "$0")/.." && pwd)
repo=$(basename "$(git -C "$root" remote get-url origin)" .git)
version=${tag#v}

# The section's lines, without the blank lines around them.
notes=$(awk -v v="$version" '
    index($0, "## [" v "]") == 1 { on = 1; next }
    on && /^## /                 { exit }
    on && (n || NF)              { line[++n] = $0; if (NF) last = n }
    END                          { for (i = 1; i <= last; i++) print line[i] }
' "$root/CHANGELOG.md")

if [[ -z $notes ]]; then
    echo "No '## [$version]' section in CHANGELOG.md -- write the notes there first." >&2
    exit 1
fi

title="$repo $version"
echo "== $title ($tag)"
echo "$notes" | sed 's/^/   | /'

if $dry_run; then
    echo "(dry run: nothing published)"
    exit 0
fi

notes_file=$(mktemp)
trap 'rm -f "$notes_file"' EXIT
printf '%s\n' "$notes" > "$notes_file"

# GitHub
if gh release view "$tag" --repo "$GITHUB_OWNER/$repo" >/dev/null 2>&1; then
    echo "GitHub: $tag already released, left alone"
else
    gh release create "$tag" --repo "$GITHUB_OWNER/$repo" --verify-tag \
        --title "$title" --notes-file "$notes_file"
    echo "GitHub: released $tag"
fi

# gitea
: "${GITEA_TOKEN:?set GITEA_TOKEN to a gitea token with write access to $GITEA_OWNER/$repo}"
api="$GITEA_URL/api/v1/repos/$GITEA_OWNER/$repo/releases"
status=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: token $GITEA_TOKEN" "$api/tags/$tag")
if [[ $status == 200 ]]; then
    echo "gitea: $tag already released, left alone"
else
    python3 -c 'import json, sys; print(json.dumps({"tag_name": sys.argv[1], "name": sys.argv[2], "body": open(sys.argv[3]).read()}))' \
        "$tag" "$title" "$notes_file" |
        curl -sf -X POST -H "Authorization: token $GITEA_TOKEN" -H 'Content-Type: application/json' \
            --data @- "$api" >/dev/null
    echo "gitea: released $tag"
fi
