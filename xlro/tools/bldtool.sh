#!/usr/bin/bash

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

set -e
[ "$1" == "-h" -o "$1" == "--help" -o "$1" == "-?" ] && { HELP=true; shift; }
BUILDHOST=${BUILDHOST:-mtv-excelero1}
TOOLDIR=${1:-$(basename $(pwd))}
SPEC=${2:-${REPO:-$(id -un)}:${BRANCH:-$(git rev-parse --abbrev-ref HEAD 2> /dev/null)}}
if [ -n "$HELP" ]
then
    echo "Usage: $(basename $0) [[tooldir ($TOOLDIR)] spec ($SPEC)]" >&2
    echo "BUILDHOST=${BUILDHOST}" >&2
    exit 2
fi
read TOOL MODULE < <(grep "$TOOLDIR\$" $(dirname $0)/../core/util/routes.conf)
TMP=bldtool.$$
trap "ssh $BUILDHOST rm -rf $TMP" EXIT
ssh $BUILDHOST bash - <<!
    set -e
    mkdir $TMP
    cd $TMP
    echo "TOOL: $TOOL, SRC: $SPEC"
    /usr/local/lib/infra/src/master/xlro/infra/bin/get-sources.sh -j -i "$SPEC" .
    echo "$TOOL $MODULE" >tmp.route
    bash -x ./infra/xlro/infra/jenkins/nvmesh_cd/compilator/pybuilder/makeinfra.sh "$SPEC" tmp.route
!

scp $BUILDHOST:$TMP/infra-bin.tgz .
# tar xvf infra-bin.tgz
# mv infra-bin $TOOL
# rm infra-bin.tgz
