#!/usr/bin/env bash

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

set -e

[ -f /.dockerenv -o -f /run/.containerenv ] || { echo "Must run inside a container." >&2; exit 1; }

UTILS=${UTILS:-/src/nvmesh-utils}
[ -d "$UTILS" ] || { echo "$UTILS missing." >&2; exit 1; }
cd "$UTILS"

poetry install --sync --no-interaction --quiet --no-root

export PYTHONDONTWRITEBYTECODE=1
pyinstaller netinfo.spec --log-level WARN --clean --workpath=/tmp -y
