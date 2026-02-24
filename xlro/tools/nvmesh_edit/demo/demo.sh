#!/usr/bin/bash

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

MGMT=n27
VOL=demo-test
VLBA=23456
NVCK=../nvck.py

set -ex
# Ensure .so is there...
scp infra_shared.so $MGMT:/tmp
ssh $MGMT sudo cp /tmp/infra_shared.so /opt/nvmesh/common-repo/tools/
rm -rf ./nvck-data ./nvck-tx0

# Detach/Delete volume if needed
infra cli -M $MGMT Volume --name $VOL - detach $MGMT || :
infra cli -M $MGMT Volume --name $VOL - delete 2>/dev/null && sleep 10 || :

infra cli -M $MGMT Volume --name $VOL --capacity 1G \
      --RAIDLevel='Erasure Coding' --protectionLevel='Ignore Separation' \
      --stripeSize=32 --stripeWidth=1 --dataBlocks=3 --parityBlocks=2 \
      --crc_enabled=False --use_debug_di= \
      - create
infra cli -M $MGMT Volume --name $VOL - attach $MGMT

scp demo-data1 demo-data2 $MGMT:/tmp
$NVCK $MGMT volume $VOL vlba $VLBA md get
ssh $MGMT sudo dd if=/tmp/demo-data1 of=/dev/nvmesh/$VOL bs=4096 count=1 seek=$VLBA
$NVCK $MGMT volume $VOL vlba $VLBA md get
ssh $MGMT sudo dd if=/tmp/demo-data2 of=/dev/nvmesh/$VOL bs=4096 count=1 seek=$VLBA
$NVCK $MGMT volume $VOL vlba $VLBA md get

infra cli -M $MGMT Volume --name $VOL - detach $MGMT || :
