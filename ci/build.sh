#!/usr/bin/env bash

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

#
# Two stages:
#   1. pyinstaller — runs ci/build-in-docker.sh inside ci/builder.Dockerfile to
#      produce dist/infra/infra (and routed symlinks).
#   2. rpmbuild  — runs RPM/buildrpm.sh inside ci/rpmbuilder.Dockerfile (or
#      a caller-supplied --builder-image, e.g. mgmt_builder_${OS}_${NODEJS} from
#      infrastructure's compile_for_mgmt2.sh).
#
# Output: nvmesh-utils-<ver>-<rel>.<dist>.{rpm,deb} copied to --output.
# Naming matches management's legacy buildrpm so signing + mv_pkg keep working.

set -e

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CI_DIR="$REPO_ROOT/ci"
RPM_DIR="$REPO_ROOT/RPM"

print_help() {
    cat <<EOF
usage: build.sh [options]

  --output DIR             where to drop the built .rpm/.deb (default: cwd)
  --pkg-type rpm|deb       build target (default: rpm)
  --build-num N            build number suffix (default: buildnumber)
  --dist-tag TAG           distribution tag (e.g. .el8_4 or .ubuntu2204)
  --branch BRANCH          override branch label
  --commit-id COMMIT       override commit label
  --git-describe DESC      override git describe value (e.g. v3.1.0-1-g38409b6e)
  --builder-image TAG      reuse an existing rpm-builder image (skips docker build)
  --skip-pyinstaller       reuse the existing dist/ tree (CI re-runs)
  --skip-rpmbuild          stop after stage 1; produce dist/ but do not package (RPM/DEB built elsewhere)
  --no-sign                do not run rpm --addsign (default)
  --sign                   sign the rpm with rpmmacros %_gpg_name
  --pyver VER              Python version for builder.Dockerfile (default: 3.10.19)
  -h, --help               this help

Environment overrides:
  PYVER, INFRA_BUILD_MODE, RPM_BUILDER_BASE (default ubuntu:20.04)
EOF
}

OUTPUT_DIR="$(pwd)"
PKG_TYPE="rpm"
BUILD_NUM="buildnumber"
DIST_TAG=""
BRANCH=""
COMMIT_ID=""
DESCRIBE=""
BUILDER_IMAGE=""
SKIP_PYINSTALLER=false
SKIP_RPMBUILD=false
SIGN_RPM=false
PYVER="${PYVER:-3.10.19}"
RPM_BUILDER_BASE="${RPM_BUILDER_BASE:-ubuntu:20.04}"

while [[ $# -gt 0 ]]; do
case "$1" in
    -h|--help)              print_help; exit 0 ;;
    --output)               OUTPUT_DIR="$2"; shift 2 ;;
    --pkg-type)             PKG_TYPE="$2"; shift 2 ;;
    --build-num|--build-number) BUILD_NUM="$2"; shift 2 ;;
    --dist-tag)             DIST_TAG="$2"; shift 2 ;;
    --branch)               BRANCH="$2"; shift 2 ;;
    --commit-id)            COMMIT_ID="$2"; shift 2 ;;
    --git-describe)         DESCRIBE="$2"; shift 2 ;;
    --builder-image)        BUILDER_IMAGE="$2"; shift 2 ;;
    --skip-pyinstaller)     SKIP_PYINSTALLER=true; shift ;;
    --skip-rpmbuild)        SKIP_RPMBUILD=true; shift ;;
    --sign)                 SIGN_RPM=true; shift ;;
    --no-sign)              SIGN_RPM=false; shift ;;
    --pyver)                PYVER="$2"; shift 2 ;;
    *)                      echo "Unknown option: $1" >&2; print_help; exit 1 ;;
esac
done

mkdir -p "$OUTPUT_DIR"
OUTPUT_DIR="$(cd "$OUTPUT_DIR" && pwd)"

case "$PKG_TYPE" in
    rpm) UBUNTU_FLAG=""          ; DIST_OVERRIDE="rpm-only" ;;
    deb) UBUNTU_FLAG="--ubuntu"  ; DIST_OVERRIDE=""         ;;
    *)   echo "ERROR: --pkg-type must be rpm or deb (got '$PKG_TYPE')" >&2; exit 1 ;;
esac

[ -z "$DESCRIBE" ] && DESCRIBE=$(cd "$REPO_ROOT" && git describe 2>/dev/null || echo "0.0.0-0")
[ -z "$BRANCH" ] && BRANCH=$(cd "$REPO_ROOT" && git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")
[ -z "$COMMIT_ID" ] && COMMIT_ID=$(cd "$REPO_ROOT" && git log -n1 --format=%h 2>/dev/null || echo "unknown")
echo "[build] DESCRIBE=$DESCRIBE BRANCH=$BRANCH COMMIT=$COMMIT_ID PKG_TYPE=$PKG_TYPE DIST_TAG=$DIST_TAG"

sed -i 's#^\(src_version:\).*#\1 "'"$DESCRIBE"'"#' "$REPO_ROOT/xlro/core/config/infra_config.yaml"

# Stage 1: pyinstaller (skipped on CI re-runs that cached dist/)
if [ "$SKIP_PYINSTALLER" = true ]; then
    echo "[build] --skip-pyinstaller: reusing existing $REPO_ROOT/dist"
    [ -d "$REPO_ROOT/dist" ] || { echo "ERROR: $REPO_ROOT/dist missing" >&2; exit 1; }
else
    PY_IMAGE="utils-builder:py-${PYVER}"
    echo "[build] stage 1: pyinstaller via $PY_IMAGE"
    (
        cd "$CI_DIR"
        BUILD_CTX=$(mktemp -d)
        trap 'sudo rm -rf "$BUILD_CTX"' EXIT
        cp "$CI_DIR/builder.Dockerfile" "$BUILD_CTX/"
        cp "$REPO_ROOT/pyproject.toml" "$BUILD_CTX/"
        [ -f "$REPO_ROOT/poetry.lock" ] && cp "$REPO_ROOT/poetry.lock" "$BUILD_CTX/"
        docker build --build-arg PYVER="$PYVER" -t "$PY_IMAGE" \
            -f "$BUILD_CTX/builder.Dockerfile" "$BUILD_CTX"
    )
    docker run --privileged --rm -w / \
        -e UTILS=/src/nvmesh-utils \
        -e INFRA_BUILD_MODE="${INFRA_BUILD_MODE:-onedir}" \
        -v "$REPO_ROOT:/src/nvmesh-utils" \
        "$PY_IMAGE" \
        bash -x /src/nvmesh-utils/ci/build-in-docker.sh
fi

if [ "$SKIP_RPMBUILD" = true ]; then
    echo "[build] --skip-rpmbuild: stopping after pyinstaller stage; dist/ left in $REPO_ROOT/dist"
    exit 0
fi

# Stage 2: rpmbuild (+ alien if --pkg-type deb)
if [ -n "$BUILDER_IMAGE" ]; then
    echo "[build] stage 2: reusing caller-supplied image $BUILDER_IMAGE"
    RPM_IMAGE="$BUILDER_IMAGE"
else
    RPM_IMAGE="utils-rpmbuilder:${RPM_BUILDER_BASE//[:\/]/-}"
    echo "[build] stage 2: building $RPM_IMAGE from $RPM_BUILDER_BASE"
    docker build --build-arg BASE="$RPM_BUILDER_BASE" \
        -t "$RPM_IMAGE" -f "$CI_DIR/rpmbuilder.Dockerfile" "$CI_DIR"
fi

# Forward dist-tag positional spelled the buildrpm.sh way
DIST_TAG_ARGS=()
[ -n "$DIST_TAG" ] && DIST_TAG_ARGS=("--dist-tag" "$DIST_TAG")

# Run buildrpm.sh inside the rpmbuilder, mounting the repo + output dir.
docker run --rm \
    -v "$REPO_ROOT:/src/nvmesh-utils" \
    -v "$OUTPUT_DIR:/out" \
    -w /src/nvmesh-utils \
    -e DISTRIBUTION_INFO="$DIST_OVERRIDE" \
    --entrypoint bash \
    "$RPM_IMAGE" \
    -x /src/nvmesh-utils/RPM/buildrpm.sh \
        --branch       "$BRANCH" \
        --commit-id    "$COMMIT_ID" \
        --git-describe "$DESCRIBE" \
        --build-number "$BUILD_NUM" \
        --output       /out \
        $UBUNTU_FLAG \
        "${DIST_TAG_ARGS[@]}" \
        $([ "$SIGN_RPM" = true ] && echo "--sign-rpm")

echo "[build] artifacts in $OUTPUT_DIR:"
ls -la "$OUTPUT_DIR" | grep -E '\.(rpm|deb)$' || echo "  (none)"
