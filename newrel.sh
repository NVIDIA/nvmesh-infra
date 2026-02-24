# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

[ -z $1 ] && { echo "Usage: $0 new-rel [base [remote]] # default base=master remote=origin" >&2; exit 2; }
VERSION=$1
BASE=${2:-master}
REMOTE=${3:-origin}

# Validate
git fetch --prune $REMOTE || exit 1
git checkout $REMOTE/$BASE &>/dev/null || { echo "$REMOTE/$BASE does not exist!"; exit 1; }
git checkout $REMOTE/$VERSION &>/dev/null && { echo "$REMOTE/$VERSION already exists!"; exit 1; }

read -p "Are you sure you want to create $REMOTE/$VERSION from $REMOTE/$BASE? [N/y] " ok
[ "${ok^^}" != "Y" ] && { echo "Cancelling."; exit 1; }

set -xe
git checkout -b v$VERSION $REMOTE/$BASE
git commit --allow-empty -m "v$VERSION branch from $BASE" --no-verify
git tag -a $VERSION -m "v$VERSION branch from $BASE"
git push -u $REMOTE v$VERSION:$VERSION
git fetch $REMOTE
git diff $REMOTE/$BASE
