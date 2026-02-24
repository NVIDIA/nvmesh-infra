#!/bin/bash

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

DELAY=2
INTERVAL=1
TRACE_DIR=/var/log/nvmesh/trace_daemon
declare -A TRACE_FILTERS=(
    [client]="trace = trace_1_volume_nvmeibc_volume_detach_o OR ( has @HDR_TYPE && has @RES_MOD_VER )"
)

mkdir -p $TRACE_DIR
cd $TRACE_DIR

# Once pager.py fixed, we don't need the pager.py loop - just use --watch
SINCE=$(date +%s%N -d '15 sec ago')
while true
do
	UNTIL=$(date +%s%N -d "$DELAY sec ago")
	RANGE="--since $SINCE --until $UNTIL"
  for component in ${!TRACE_FILTERS[@]}
  do
	  test -f pager.py && ./pager.py ${RANGE} --$component --silent --mode msg-stream-json -f "${TRACE_FILTERS[$component]}"
	done
	SINCE=$UNTIL
	sleep $INTERVAL
done | sed -u -e 's/"nanoseconds"[^0-9]*\([0-9]\{10\}\)\([0-9]*\)[0-9]*/"ts_sec":\1,"ts_nano":\2/' | nc -N -4 localhost 5171
