# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

set -ex
INFRA=${1:-$(cd $(dirname $0)/../.. &>/dev/null && pwd)}
echo INFRA=$INFRA
$INFRA/xlro/infra/jenkins/nvmesh_cd/compilator/pybuilder/makeinfra.sh $INFRA
tar xzf infra-bin.tgz

# Test all vs. fast fail
ERRORS=0
for cmd in $(cut -f1 -d' ' $INFRA/xlro/core/util/routes.conf)
do
    echo "Testing $cmd..."
    ./infra-bin/$cmd --help >/dev/null && echo OK || { let ERRORS++ || echo "$x FAILED" >&2; }
done
[ $ERRORS -eq 0 ] || { echo "Packaging failed." >&2 ; exit $ERRORS ; }

if [ -n "$MGMT" ]
then
    # We can add, but nvshow will at least connect to mgmt and retrieve some stuff, to exercise Entities/SDK
    for test in "nvshow -M $MGMT" "nvmesh -m $MGMT client show"
    do
        echo "$test ..."
        eval ./infra-bin/$test
    done
else
    echo "WARNING: No MGMT set, so not testing connectivity"
fi
