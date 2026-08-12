#!/usr/bin/env bash

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

# nvmesh-utils selftest harness.
#
# Forked from infrastructure/xlro/infra/bin/selftest.sh (NVMESH-8474).
# Differences vs. the infra copy:
#   - Default DIRS covers nvmesh-utils' trees ('core tools') instead of
#     ('core infra'); infrastructure's selftest keeps owning 'infra'.
#   - Fallback path resolution accepts this repo's layout ($BIN/../xlro/$DIRS).
#
# Callers pass a directory (relative to the repo root or absolute) and a
# phase (mypy/pylint/unittest/layers/slash). Run from a git checkout — the
# PYFILES discovery uses `git ls-files`.
BIN=$(dirname "$0")
FASTFAIL=false
while :
do
    case "$1" in
        -m|--mgmt)
            MGMT=$2
            export NVMESH_MGMT=$2
            shift 2
            ;;
        -d|--dir)
            DIRS="${DIRS:+"$DIRS "}$(echo $2 | tr ',' ' ')"
            shift 2
            ;;
        -p|--phase)
            PHASES="${PHASES:+"$PHASES "}$(echo $2 | tr ',' ' ')"
            shift 2
            ;;
        -f|--fastfail)
            FASTFAIL=true
            shift
            ;;
        --|"")
            shift
            break
            ;;
        *)
            echo "Invalid arg: $1" >&2
            echo "Usage: $0 [-m <mgmt-host>] [-d <dirs>] [-p <phases>]" >&2
            exit 2
            ;;
    esac
done
: ${DIRS:="core tools"}
: ${PHASES:="mypy pylint unittest layers"}
if [[ "$DIRS" =~ \  ]]
then
    for dir in $DIRS
    do
        "$0" -d $dir ${MGMT:+-m $MGMT} ${PHASES:+-p "$PHASES"} || { echo "Failed on $dir." >&2; exit 1; }
    done
    exit 0
fi

echo "running selftest on $DIRS"
echo "PYTHON: $(type -p python) $(python -V)"
# Resolution order: absolute/relative-from-CWD -> <repo>/xlro/$DIRS -> legacy $BIN/../../$DIRS.
CDPATH= cd "$DIRS" 2>/dev/null \
    || cd "$BIN/../xlro/$DIRS" 2>/dev/null \
    || cd "$BIN/../../$DIRS" 2>/dev/null \
    || { echo "$DIRS not found!"; exit 1; }
DIRBASE=$(basename "$DIRS")

BREAK=:
: ${CONFIG:="$PWD/selftest.conf"}
[ -f "$CONFIG" ] && source "$CONFIG"


function phase() {
    PHASE="${1^^}"
    [[ "$PHASES" == *"${1,,}"* ]] && { $BREAK; echo "[${1^^}]"; BREAK=echo ; }
}

function error() {
    $FASTFAIL && { echo "$PHASE on $DIRS Failed.  Exiting." >&2; exit 1; }
    EXIT_STATUS=1
}

EXIT_STATUS=0

: ${LOGD:=./test-logs}
mkdir -p "$LOGD"

PYFILES=$(git ls-files | grep '.py$')
TESTS=$(git ls-files './test/[^_]*.py')
PYLINT_FLAGS="--errors-only --disable=no-member --output-format=parseable"
! phase pylint || pylint $PYLINT_FLAGS $PYFILES || error

if phase mypy
then
    RESULTS=$LOGD/$DIRBASE-MYPY.results
    rm -rf "$RESULTS"
    RELDIR=xlro/$(basename "$(pwd)")/
    MYPYFILES=$(echo "$PYFILES" | grep -v "${MYPY_IGNORE:-NO__PATTERN}" | sed -e "s?^?$RELDIR/?")
    MYPY_FLAGS=" --implicit-optional --explicit-package-bases --namespace-packages --allow-untyped-defs \
        --check-untyped-defs --ignore-missing-imports --show-error-codes --no-error-summary"
    cmd="mypy --python-executable=$(which python) $MYPY_FLAGS"
    echo "MYPY-CMD: $cmd"
    ( cd ../.. && $cmd $MYPYFILES ) &>"$RESULTS.raw"
    ECODE=$?
    echo EXIT_CODE=$ECODE
    grep "^$RELDIR" "$RESULTS.raw" | grep -v "${MYPY_IGNORE:-NO__PATTERN}" | tee "$RESULTS"
    [ $ECODE -ne 0 -o -s "$RESULTS" ] && error
fi

UTESTS_SED_PAT='s?/?.?g;s?.py$??'
if phase unittest
then
    UTESTS=$(grep -l unittest $TESTS | sed -e $UTESTS_SED_PAT)
    if [ -n "$UTESTS" ]
    then
        python -Wignore -m unittest -f $UTESTS || error
    else
        echo "No unittests found."
    fi
fi

# 'slash' phase intentionally omitted: nvmesh-utils CI runs unit + static tests only.
# Integration/slash testing continues to live in the infrastructure repo's infra-ci
# for now — the nvmesh-utils Jenkinsfile gates slash via a downstream trigger.

RESULTS=$LOGD/LAYERS.results
rm -rf "$RESULTS"
phase layers && grep -o -E $LAYERS_GREP_PATTERN $PYFILES 2>&1 | sort -u | tee "$RESULTS" >&2
[ -s "$RESULTS" ] && error

[ $EXIT_STATUS -eq 0 ] && echo "$DIRS Success" || echo "$DIRS Failed"
exit $EXIT_STATUS
