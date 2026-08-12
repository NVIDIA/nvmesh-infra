#!/usr/bin/env bash

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

# xUnit aggregator for nvmesh-utils Jenkins pipeline.
# Verbatim port of infrastructure/xlro/infra/jenkins/xunit.sh (NVMESH-8474);
# kept as a separate copy so the nvmesh-utils CI has no build-time dep on
# infrastructure when the repos are not siblings.
export XU_SKIPCODE=39

: ${XU_RESULTS:=./xunit.xml} ${XU_SUITE:=$(basename "$0")}
: ${XU_DIR:=$(dirname "$XU_RESULTS")}
declare -A XU_COUNTS=([success]=0 [failure]=0 [error]=0 [skip]=0 [notrun]=0)

XU_N=0

htmlcode() {
    sed 's/&/\&amp;/g; s/</\&lt;/g; s/>/\&gt;/g; s/"/\&quot;/g; s/'"'"'/\&#39;/g; ' -
}

xtest() {
    XU_NAME=$1
    XU_SKIP=
    [ $# -gt 1 ] && shift
    if [ $XU_N -eq 0 ]
    then
        mkdir -p "$XU_DIR"
        echo -e "<testsuite name=\"$XU_SUITE\">\n</testsuite>\n" >"$XU_RESULTS"
    fi
    XU_N=$((XU_N+1))
    XU_TLOGS=$XU_DIR/test-$XU_N:$(echo "${XU_NAME}" | tr -s ' /' '-')
    rm -rf "$XU_TLOGS"; mkdir -p "$XU_TLOGS"
    echo 0 >"$XU_TLOGS/code"
    echo "# START TEST #$XU_N [$XU_NAME] $*..."
    XU_START=$(date +%s.%N)
    { { eval "$@" 3>&1 1>&2 2>&3 3>&- || echo $? >"$XU_TLOGS/code"; } | tee "$XU_TLOGS/err" ;} |& tee "$XU_TLOGS/logs"
    XU_CODE=$(cat "$XU_TLOGS/code")
    XU_TIME=$(date "+scale=3; (%s.%N-$XU_START)/1" | bc)
    echo "TAIL:"
    tail "$XU_TLOGS/logs"
    echo "END TAIL:"
    if [ "$XU_CODE" -eq "$XU_SKIPCODE" ] && XU_SKIP=$(tail "$XU_TLOGS/logs" | grep -Po '^SKIP: \K.*')
    then
        XU_STATUS=skip
    elif [ "$XU_CODE" -eq 0 ]
    then
        XU_STATUS=success
    else
        XU_STATUS=failure
    fi
    echo -e "# END TEST #$XU_N - EXITCODE: $XU_CODE [status=$XU_STATUS] (fatal? ${XU_FATAL:-false}, skip? ${XU_SKIP:-None})\n"
    let XU_COUNTS[$XU_STATUS]++ || true
    (
        echo "  <testcase name=\"$XU_NAME\" time=\"$XU_TIME\" status=\"$XU_STATUS\" logdir=\"$XU_TLOGS\" logpath=\"$XU_TLOGS/logs\" code=\"$XU_CODE\">"
        if [ "$XU_CODE" -ne 0 ] && [ -s "$XU_TLOGS/err" ]
        then
            if [ -n "$XU_SKIP" ]
            then
                echo "<error>$(echo -e "Test Skipped:\n$XU_SKIP\nMore info in logs." | htmlcode)</error>"
            else
                echo "<error>$(htmlcode < "$XU_TLOGS/err")</error>"
            fi
        fi
        echo "  </testcase>"
    ) >"$XU_RESULTS.tmp"

    sed -i "/\/testsuite/e cat $XU_RESULTS.tmp" "$XU_RESULTS"
    [ -z "$XU_FATAL" ] || [ "$XU_CODE" -eq 0 ] || { echo "FATAL!"; exit 1; }
    return "$XU_CODE"
}

xerrors() {
    echo $(( ${XU_COUNTS[failure]} + ${XU_COUNTS[error]} ))
}

xskip() {
    echo "SKIP: $*"
    return "$XU_SKIPCODE"
}
