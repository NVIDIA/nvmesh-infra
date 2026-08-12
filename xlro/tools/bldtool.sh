#!/usr/bin/bash

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

# Build a single CLI tool (or all tools) as a pyinstaller binary.
# Usage: bldtool.sh [tooldir] [--all]
#   tooldir: name of the tool directory (default: basename of cwd)
#   --all:   build all tools from routes.conf
#
# Environment:
#   BUILDHOST  — if set, build on a remote host via SSH instead of locally
set -e

REPO_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
ROUTES=$REPO_ROOT/xlro/core/util/routes.conf

[ "$1" == "-h" -o "$1" == "--help" -o "$1" == "-?" ] && {
    echo "Usage: $(basename "$0") [tooldir|--all] [-h]" >&2
    echo "  tooldir   Build a single tool (default: basename of cwd)" >&2
    echo "  --all     Build all tools from routes.conf" >&2
    echo "  BUILDHOST env var: build on remote host via SSH" >&2
    exit 2
}

if [ "$1" == "--all" ]; then
    BUILD_ROUTES=$ROUTES
    echo "Building all tools from routes.conf"
else
    TOOLDIR=${1:-$(basename "$(pwd)")}
    read TOOL MODULE < <(grep "${TOOLDIR}\$" "$ROUTES") || { echo "Tool '$TOOLDIR' not found in routes.conf" >&2; exit 1; }
    BUILD_ROUTES=$(mktemp /tmp/bldtool-routes.XXXXXX)
    echo "$TOOL $MODULE" > "$BUILD_ROUTES"
    trap "rm -f $BUILD_ROUTES" EXIT
    echo "Building tool: $TOOL ($MODULE)"
fi

if [ -n "$BUILDHOST" ]; then
    TMP=bldtool.$$
    trap "ssh $BUILDHOST rm -rf $TMP; rm -f $BUILD_ROUTES 2>/dev/null" EXIT
    ssh "$BUILDHOST" mkdir "$TMP"
    scp -r "$REPO_ROOT"/{xlro,pyproject.toml,poetry.lock,netinfo.spec} "$BUILDHOST:$TMP/"
    [ "$BUILD_ROUTES" != "$ROUTES" ] && scp "$BUILD_ROUTES" "$BUILDHOST:$TMP/xlro/core/util/routes.conf"
    ssh "$BUILDHOST" bash -c "'
        set -e
        cd $TMP
        rm -rf dist/ build/
        poetry install --no-root --quiet
        poetry run pyinstaller netinfo.spec -y
    '"
    scp -r "$BUILDHOST:$TMP/dist" "$REPO_ROOT/"
else
    cd "$REPO_ROOT"
    [ "$BUILD_ROUTES" != "$ROUTES" ] && cp "$BUILD_ROUTES" xlro/core/util/routes.conf
    rm -rf dist/ build/
    poetry install --no-root --quiet
    poetry run pyinstaller netinfo.spec -y
    [ "$BUILD_ROUTES" != "$ROUTES" ] && git checkout xlro/core/util/routes.conf 2>/dev/null
fi

echo "Done. Output in $REPO_ROOT/dist/"
