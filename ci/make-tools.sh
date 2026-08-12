#!/usr/bin/env bash

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

# Build nvmesh-utils CLI tools (pyinstaller executables) inside a Docker container.
# Runs from a local nvmesh-utils checkout -- no source fetching needed.
#
# Usage: make-tools.sh [alt-routes] [--docker-host <uri>]
#
# Environment:
#   PYVER              Python version (default: 3.10.19)
#   INFRA_BUILD_MODE   Build mode passed to buildInfra (onefile/onedir)
#   INFRA_VER          Version stamp (default: git describe)
set -e

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CI_DIR="$REPO_ROOT/ci"

ALTROUTES="${1:-}"; [[ $# -ge 1 ]] && shift
PYVER=${PYVER:-3.10.19}

DOCKERFILE=builder.Dockerfile
IMAGE=utils-builder

TMP=$(mktemp -t -d mktools-XXXXXX)
chmod 777 "$TMP"
trap "sudo rm -rf $TMP" EXIT SIGINT

DESCRIBE=$(cd "$REPO_ROOT" && git describe 2>/dev/null || echo "unknown")
echo "DESCRIBE=$DESCRIBE INFRA_BUILD_MODE=$INFRA_BUILD_MODE PYVER=$PYVER"

# Copy repo into build context
cp -a "$REPO_ROOT" "$TMP/nvmesh-utils"

# Version stamp
INFRA_VER="${INFRA_VER:-$DESCRIBE}"
sed -i 's#^\(src_version:\).*#\1 "'"$INFRA_VER"'"#' "$TMP/nvmesh-utils/xlro/core/config/infra_config.yaml"

# Optional routes override
if [ -n "$ALTROUTES" ] && [ -f "$ALTROUTES" ]; then
    cp "$ALTROUTES" "$TMP/nvmesh-utils/xlro/core/util/routes.conf"
fi

# Prepare Docker build context
cp "$TMP/nvmesh-utils/pyproject.toml" "$TMP/"
[ -f "$TMP/nvmesh-utils/poetry.lock" ] && cp "$TMP/nvmesh-utils/poetry.lock" "$TMP/"
cp "$CI_DIR/$DOCKERFILE" "$TMP/"

IMAGE_TAG="py-${PYVER}"
echo "Building image: $IMAGE:$IMAGE_TAG"
docker build --build-arg PYVER="$PYVER" -t "$IMAGE:$IMAGE_TAG" -f "$TMP/$DOCKERFILE" "$TMP"

docker run --privileged --rm -w / \
    -e UTILS=/src/nvmesh-utils \
    -e INFRA_BUILD_MODE="$INFRA_BUILD_MODE" \
    -v "$TMP:/src" \
    "$IMAGE:$IMAGE_TAG" \
    bash -x /src/nvmesh-utils/ci/build-in-docker.sh

cp -r "$TMP/nvmesh-utils/dist" "$TMP/infra-bin"
( cd "$TMP" && tar czf infra-bin.tgz infra-bin )
mv "$TMP/infra-bin.tgz" "$(pwd)/"

echo "Done. Output: infra-bin.tgz"
