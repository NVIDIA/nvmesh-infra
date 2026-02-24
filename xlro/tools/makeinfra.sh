#!/usr/bin/env bash

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

set -e
ISPEC=${1:-master}
ALTROUTES=$2
PYVER=${PYVER:-3.10.19}

CWD=$(pwd)
PYBDIR=$(cd $(dirname $0) &>/dev/null && pwd)
XLRO=${PYBDIR%/xlro/*}/xlro
#OS source ${XLRO}/infra/bin/common.sh
TMP=$(mktemp -t -d mkinfra-XXXXXX)
chmod 777 $TMP
trap "sudo rm -rf ${TMP}" EXIT SIGINT

DOCKERFILE=infra-builder.Dockerfile
IMAGE=infra-builder

echo ISPEC=$ISPEC TMP=$TMP
$XLRO/infra/bin/get-sources.sh -j -i $ISPEC $TMP

# Get git describe from fetched sources for cache key and image tag
[ -d $ISPEC ] && DESC_DIR=$ISPEC || DESC_DIR=$TMP/infra

if false; then
    INFRA_DESCRIBE=$(cd $DESC_DIR && git describe 2>/dev/null || echo "unknown")
    echo "INFRA_DESCRIBE=$INFRA_DESCRIBE INFRA_BUILD_MODE=$INFRA_BUILD_MODE"

    # Check cache - if hit, link cached infra-bin.tgz and exit
    INFRA_CACHEKEY="$INFRA_DESCRIBE+$INFRA_BUILD_MODE"
    if checkPkgCache infra "$INFRA_CACHEKEY" infra-bin.tgz; then
        echo "Using cached infra-bin.tgz"
        exit 0
    fi
    ISPEC_DOCKERFILE=$TMP/infra/xlro/infra/jenkins/nvmesh_cd/compilator/pybuilder/$DOCKERFILE
else
    ISPEC_DOCKERFILE=$TMP/infra/xlro/tools/$DOCKERFILE
fi
if grep -q '^ARG PYVER' "$ISPEC_DOCKERFILE"; then
    echo "Dockerfile supports PYVER override, using PYVER=$PYVER"
    IMAGE_TAG="py-${PYVER}"
    BUILD_ARGS="--build-arg PYVER=$PYVER"
else
    echo "Dockerfile does not support PYVER override, using static Python version"
    STATIC_PYVER=$(sed -nE 's/^ENV[[:space:]]+PYVER[[:space:]]*=?[[:space:]]*([^[:space:]]+).*/\1/p' "$ISPEC_DOCKERFILE" | head -n1)
    if [ -n "$STATIC_PYVER" ]; then
        IMAGE_TAG="py-${STATIC_PYVER}"
    else
        IMAGE_TAG="py-none"
    fi
    BUILD_ARGS=""
fi


echo "Building image: $IMAGE:$IMAGE_TAG"
cp $ISPEC_DOCKERFILE $TMP/
# Copy Poetry files for dependency pre-installation
cp $XLRO/../pyproject.toml $XLRO/../poetry.lock $TMP/
docker build $BUILD_ARGS -t $IMAGE:$IMAGE_TAG -f $TMP/$DOCKERFILE $TMP
# Update built-in infra version
INFRA_VER=$ISPEC:$INFRA_DESCRIBE
echo VER=$INFRA_VER
sed -i 's#^\(src_version:\).*#\1 "'"$INFRA_VER"'"#' $TMP/infra/xlro/core/config/infra_config.yaml
# Enable override of routes.conf for extra utilities
( cd $CWD && test -f "$ALTROUTES" && cp "$ALTROUTES" $TMP/infra/xlro/core/util/routes.conf ) || :
docker run --privileged --rm -e INFRA=/src/infra -e INFRA_BUILD_MODE="$INFRA_BUILD_MODE" -v "$TMP:/src" "$IMAGE${IMAGE_TAG:+:$IMAGE_TAG}" \
    bash -x /src/infra/xlro/tools/buildInfra.sh

find $TMP -name dist
cp -r $TMP/infra/dist $TMP/infra-bin
( cd $TMP && tar czf infra-bin.tgz infra-bin && mv infra-bin.tgz $CWD )

# Update cache with the built package
## ( cd $CWD && PKG_TYPE=tgz updatePkgCache infra "$INFRA_CACHEKEY" infra-bin.tgz )

exit 0
