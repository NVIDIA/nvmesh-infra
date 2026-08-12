#!/usr/bin/env bash

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

# release.sh — cut a new nvmesh-utils Python release.
#
# Maintainer-run (not CI-invoked). Bumps pyproject.toml version, commits,
# tags, and pushes the tag + commit to origin.
#
# Usage:
#   ci/release.sh <version>          # explicit, e.g. 3.5.1
#   ci/release.sh --bump {major|minor|patch}
#   ci/release.sh                    # defaults to --bump patch
#
# Flags:
#   --no-push        Skip the final git push (dry run for CI / preview).
#   --remote <name>  Push target (default: origin).
#   -h | --help      This help.
#
# See docs/RELEASE.md for versioning policy and how infrastructure consumes
# these tags.
set -euo pipefail

die()  { echo "release.sh: ERROR: $*" >&2; exit 1; }
info() { echo "release.sh: $*"; }

usage() {
    sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

PYPROJECT="$REPO_ROOT/pyproject.toml"
REMOTE="origin"
PUSH=1
TARGET_VERSION=""
BUMP_KIND=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help)      usage 0 ;;
        --no-push)      PUSH=0; shift ;;
        --remote)       REMOTE="$2"; shift 2 ;;
        --bump)         BUMP_KIND="$2"; shift 2 ;;
        --bump=*)       BUMP_KIND="${1#--bump=}"; shift ;;
        -*)             die "unknown flag: $1 (try --help)" ;;
        *)              [[ -n "$TARGET_VERSION" ]] && die "unexpected arg: $1"
                        TARGET_VERSION="$1"; shift ;;
    esac
done

[[ -n "$TARGET_VERSION" && -n "$BUMP_KIND" ]] \
    && die "use either <version> or --bump <kind>, not both"
[[ -z "$TARGET_VERSION" && -z "$BUMP_KIND" ]] && BUMP_KIND="patch"

# Preconditions ---------------------------------------------------------------

[[ -f "$PYPROJECT" ]] || die "pyproject.toml not found at $PYPROJECT"

command -v git >/dev/null || die "git is required"

CURRENT_BRANCH="$(git symbolic-ref --short HEAD 2>/dev/null || echo DETACHED)"
[[ "$CURRENT_BRANCH" == "master" ]] \
    || die "must run on master branch (currently: $CURRENT_BRANCH)"

[[ -z "$(git status --porcelain)" ]] \
    || die "working tree is dirty; commit or stash first"

git fetch --tags "$REMOTE" >/dev/null 2>&1 || info "warn: 'git fetch --tags $REMOTE' failed, proceeding with local tags"

LOCAL_SHA="$(git rev-parse HEAD)"
REMOTE_SHA="$(git rev-parse "$REMOTE/master" 2>/dev/null || echo "")"
if [[ -n "$REMOTE_SHA" && "$LOCAL_SHA" != "$REMOTE_SHA" ]]; then
    die "local master ($LOCAL_SHA) diverges from $REMOTE/master ($REMOTE_SHA); sync first"
fi

# Derive current + target version --------------------------------------------

get_current_version() {
    awk -F' *= *' '/^version *= *"/ { gsub(/"/,"",$2); print $2; exit }' "$PYPROJECT"
}

validate_semver() {
    [[ "$1" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[A-Za-z0-9.+-]+)?$ ]] \
        || die "invalid semver: $1 (expected X.Y.Z[-pre])"
}

bump_version() {
    local ver="$1" kind="$2"
    local base="${ver%%-*}"
    IFS=. read -r MA MI PA <<<"$base"
    case "$kind" in
        major) MA=$((MA+1)); MI=0; PA=0 ;;
        minor) MI=$((MI+1)); PA=0 ;;
        patch) PA=$((PA+1)) ;;
        *) die "unknown --bump kind: $kind (use major|minor|patch)" ;;
    esac
    echo "$MA.$MI.$PA"
}

CURRENT_VERSION="$(get_current_version)"
[[ -n "$CURRENT_VERSION" ]] || die "could not read version from $PYPROJECT"
validate_semver "$CURRENT_VERSION"

if [[ -n "$BUMP_KIND" ]]; then
    TARGET_VERSION="$(bump_version "$CURRENT_VERSION" "$BUMP_KIND")"
fi
validate_semver "$TARGET_VERSION"

NEW_TAG="v$TARGET_VERSION"

if git rev-parse -q --verify "refs/tags/$NEW_TAG" >/dev/null; then
    die "tag $NEW_TAG already exists"
fi

info "bumping: $CURRENT_VERSION -> $TARGET_VERSION  (tag: $NEW_TAG)"

# Apply version bump ----------------------------------------------------------

# Only rewrite the top-level [tool.poetry] version, not any other version= lines
# that might appear in dependency constraints below.
python3 - "$PYPROJECT" "$TARGET_VERSION" <<'PY'
import re, sys
path, new = sys.argv[1], sys.argv[2]
with open(path, 'r', encoding='utf-8') as f:
    text = f.read()
# Replace only the first version = "..." after [tool.poetry]
pattern = re.compile(r'(\[tool\.poetry\][^\[]*?\nversion\s*=\s*")([^"]+)(")', re.DOTALL)
new_text, n = pattern.subn(lambda m: f'{m.group(1)}{new}{m.group(3)}', text, count=1)
if n != 1:
    sys.exit(f"release.sh: could not locate [tool.poetry] version in {path}")
with open(path, 'w', encoding='utf-8') as f:
    f.write(new_text)
PY

# Sanity check the rewrite took.
WRITTEN_VERSION="$(get_current_version)"
[[ "$WRITTEN_VERSION" == "$TARGET_VERSION" ]] \
    || die "post-write version mismatch (got '$WRITTEN_VERSION', expected '$TARGET_VERSION')"

# Commit + tag ----------------------------------------------------------------

COMMIT_MSG="chore(release): v$TARGET_VERSION"
CHANGE_ID="I$(printf '%s-%s-%s' "$TARGET_VERSION" "$LOCAL_SHA" "$(date +%s)" | sha1sum | cut -c1-40)"
SIGNED_OFF_BY="$(git config user.name) <$(git config user.email)>"

git add "$PYPROJECT"
git commit -m "$COMMIT_MSG" -m "Change-Id: $CHANGE_ID" -m "Signed-off-by: $SIGNED_OFF_BY"

git tag -a "$NEW_TAG" -m "Release $NEW_TAG"

info "created commit $(git rev-parse --short HEAD) and tag $NEW_TAG"

# Push ------------------------------------------------------------------------

if [[ "$PUSH" -eq 0 ]]; then
    info "--no-push set; local commit+tag ready. To publish:"
    info "  git push $REMOTE master && git push $REMOTE $NEW_TAG"
    exit 0
fi

info "pushing to $REMOTE ..."
git push "$REMOTE" master
git push "$REMOTE" "$NEW_TAG"

info "released $NEW_TAG"
