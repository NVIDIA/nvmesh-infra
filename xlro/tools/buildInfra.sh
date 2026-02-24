#!/usr/bin/env bash

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

set -e

[ -f /.dockerenv -o -f /run/.containerenv ] || { echo "This is currently only meant to run in a docker!"; exit 1; }

# TODO: no need for infra dir under mgmt dir since we removed the SDK dependency. One day we will need to change this. Today is not that day.
[ -d "${INFRA:=/management/infrastructure}" ] || { echo "$INFRA missing."; exit 1; }
cd $INFRA
poetry install --sync --no-interaction --quiet --no-root

export PYTHONDONTWRITEBYTECODE=1
pyinstaller netinfo.spec --log-level WARN --clean --workpath=/tmp -y
