#!/usr/bin/bash

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

# Build all CLI tools with pyinstaller and verify each starts correctly.
# Usage: tooltest.sh [repo-root]
#
# Environment:
#   MGMT — if set, run live connectivity tests against the given management server
set -ex

REPO_ROOT=${1:-$(cd "$(dirname "$0")/../.." && pwd)}
ROUTES=$REPO_ROOT/xlro/core/util/routes.conf
DIST=$REPO_ROOT/dist

cd "$REPO_ROOT"
rm -rf dist/ build/
poetry install --no-root --quiet
poetry run pyinstaller netinfo.spec -y

ERRORS=0
for cmd in $(cut -f1 -d' ' "$ROUTES"); do
    echo "Testing $cmd..."
    "$DIST/$cmd" --help >/dev/null && echo OK || { let ERRORS++; echo "$cmd FAILED" >&2; }
done
[ $ERRORS -eq 0 ] || { echo "Packaging failed ($ERRORS errors)." >&2; exit $ERRORS; }

if [ -n "$MGMT" ]; then
    for test in "nvshow -M $MGMT" "nvmesh -m $MGMT client show"; do
        echo "$test ..."
        eval "$DIST/$test"
    done
else
    echo "WARNING: No MGMT set, so not testing connectivity"
fi
