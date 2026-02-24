#!/usr/bin/env bash

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

#####
# Prior get-sources used nvmesh as the master source for all implicit specs.
# This is clearly no longer the case with interop-db, upgrader, etc. being sources
# from manager, which itself might be implied from nvmesh.
# The point of this code is to more generally follow all ci_settings.yaml's even for implied branches
# to find the proper branch for ANY repo if possible.
#####
set -e
source "${INFRABIN:=$(dirname "$0")}/common.sh"
GIT_SERVER=$GITURL

declare -A EXPLICIT # maps pkg to spec
declare -A IMPLICIT # maps pkg to spec
declare -A SOURCE # maps pkg to source
PKGLIST=()
QUEUE=()
IS_DEBUG=false

declare -A DEFAULTS
# We shouldn't need this...
DEFAULTS['upgrader']=master
DEFAULTS['interop-db']=master

TMP=$(mktemp -d getspecs-XXXXX)
trap 'rm -rf "$TMP"' EXIT

function usage() {
    [ -n "$1" ] && echo -e "ERROR: $*" >&2
    cat >&2 <<!

Options:
    --<proj> <src-spec>                 # See below for src-spec definition. Proj is one of: nvmesh, management, upgrader, interop-db, cloud, infrastructure
    -s|--spec <src-spec>                # Test src-spec parsing
    -g|--gitcache <dir>                 # Reuse git under <dir> to save cloning (default is $XLRO_GITCACHE)
    -S|--git-server                     # git server to use (default is $GIT_SERVER)
    --debug                             # debug mode

    <src-spec> is one of the following:
    * a source directory - e.g., -n ~/projects/nvmesh
    * a git reference as [<repo>]:<branch>[@<commit>] - e.g., -n joeweisblatt/nvmesh:master@ or -m ":#CI_1.3"
      (default commit is HEAD; default repo for management is $GITGROUP/management, for nvmesh is $GITGROUP/nvmesh)
    NOTE: when using "implied" spec of "-", there MUST be at least one explicit spec from which to imply it.
!
    exit 2
}

function debug() {
    ! $IS_DEBUG || echo "# $*" >&2
}


function specParse() {
    # Usage: specParse <srcspec> [<defrepo/defproj>]
    # Returns: <GIT|SRC> <repo/project> <branch> <revision>
    # debug "SPEC-PARSING: $@" >&2
    srcspec=${1:?specParse() called without srcspec}
    if [ -d "$srcspec" ]
    then
        echo "SRC $srcspec"
    elif [[ "$srcspec" =~ ^([^, :]+.(rpm|deb)[, ]?){1,}$ ]]
    then
        echo "PKGS $(echo "$srcspec" | tr -s ',' ' ')"
    elif [[ "$srcspec" =~ ^(((([^:]*)/)?([^/:]*))?:)?([^/@]*)(@(.*))?$ ]]
    then
        DEF_REPO=${2%/*}
        DEF_PROJ=${2##*/}

        SPEC_REPO="${BASH_REMATCH[4]:-${BASH_REMATCH[5]}}"
        SPEC_PROJ="${BASH_REMATCH[4]:+${BASH_REMATCH[5]}}"
        # debug "REPO=$SPEC_REPO PROJ=$SPEC_PROJ DEF_REPO=$DEF_REPO DEF_PROJ=$DEF_PROJ" >&2
        # New gitlab has slashes in path, so double-check that project matches what it should be
        if [ -n "$SPEC_PROJ" ] && [ "$SPEC_PROJ" != "$DEF_PROJ" ]
        then
            SPEC_REPO+="/$SPEC_PROJ"
            SPEC_PROJ="$DEF_PROJ"
        fi
        # Enforce users/teams for non-default namespace
        # GROUP=$(dirname "$SPEC_REPO")
        # debug "REPO=$SPEC_REPO PROJ=$SPEC_PROJ GROUP=$GROUP" >&2
        if [ -n "$SPEC_REPO" ] && [ "$SPEC_REPO" != "$GITGROUP" ]
        then
            case "$SPEC_REPO" in
                */*)  # If someone explicitly used /, leave it alone
                    ;;
                team-*)
                    SPEC_REPO=$GITGROUP/teams/$SPEC_REPO
                    ;;
                *)
                    SPEC_REPO=$GITGROUP/users/$SPEC_REPO
                    ;;
            esac
            # debug "NEW SPEC_REPO=$SPEC_REPO" >&2
        fi

        SPEC_BRANCH="${BASH_REMATCH[6]}"
        SPEC_COMMIT="${BASH_REMATCH[8]}"

        debug "SPEC-PARSED: $srcspec -> GIT ${SPEC_REPO:-$DEF_REPO}/${SPEC_PROJ:-$DEF_PROJ} ${SPEC_BRANCH:-master}  ${SPEC_COMMIT:-HEAD}"
        echo "GIT ${SPEC_REPO:-$DEF_REPO}/${SPEC_PROJ:-$DEF_PROJ} ${SPEC_BRANCH:-master}  ${SPEC_COMMIT:-HEAD}"
    else
        ERR="Invalid source-spec: $srcspec"
        [ -n "$SPECCHECK" ] && { echo "$ERR" >&2 && exit 1; } || usage "$ERR"
    fi
}

function is_explicit_spec() {
    # Check if a spec is explicit, so we can derive another spec from it
    [[ -n "$1" && ! "$1" =~ " - " ]]
}


function addpkg() {
    debug "ADDPKG: $*"
    local pkg=$1
    local spec
    spec=$(specParse "$2" "$GITGROUP/${3:-$1}")
    if is_explicit_spec "$spec"
    then
        EXPLICIT[$pkg]="$spec"
        SOURCE[$pkg]="ARGS"
        unset 'IMPLICIT[$pkg]'
        QUEUE+=("$pkg")
    else
        [ -z "${EXPLICIT[$pkg]}" ] && IMPLICIT[$pkg]="$spec"
    fi
    PKGLIST+=("$pkg")
}

function getfile() {
    file=$1
    srctype=$2
    path=$3
    branch=$4

    debug "getfile($*)" >&2
    if [ "$srctype" = "GIT" ]
    then
        ( git archive "--remote=${GIT_SERVER}$path.git" "$branch" $file | tar -xO ) 2>/dev/null
    elif [ "$srctype" = "SRC" ]
    then
        cat $path/$file 2>/dev/null
    fi
}

function getimplied() {
    # Scan provided package/spec (e.g., nvmesh) for implied package/specs (e.g., management, etc.)
    # from either ci_settings.yaml or legacy .gitlab-ci.yml.  Echo each found.
    local pkg=$1
    local srctype=$2
    local path=$3
    local branch=$4

    debug "getimplied($*)"

    # Validate branch or fail
    if [ "$srctype" = "GIT" ] && ! git ls-remote --exit-code --heads ${GIT_SERVER}$path.git $branch &>/dev/null
    then
        echo "Invalid GIT spec - missing remote/branch $path $branch" >&2
        return 1
    fi

    # Don't bother with interop-db, cloud, or upgrader
    if [ "$pkg" = "interop-db" ] || [ "$pkg" = "cloud" ] || [ "$pkg" = "upgrader" ]; then return 0; fi

    getfile ci_settings.yaml $srctype $path $branch >$TMP/ci_settings.yaml
    if grep -q build-with $TMP/ci_settings.yaml; then
        yq --version | grep -q 'mikefarah' && YQARG="eval" || YQARG="-r"
        while read -r proj branch; do
            debug "implied from $pkg: $proj $branch"
            echo "$proj" "$branch"
        done < <(yq $YQARG '.["build-with"] // {} | to_entries | .[] | .key + " " + .value' $TMP/ci_settings.yaml)
    elif [ "$pkg" = "nvmesh" ] || [ "$pkg" = "management" ]; then
        # Backwards compatibility - check .gitlab-ci.yml.  Not sure still relevant
        branch=$(getfile .gitlab-ci.yml $srctype $path $branch | grep -Po '^[^#]*TEST_WITH_BRANCH="*\K[^"]*') || :
        [ "$pkg" = "nvmesh" ] && proj=management || proj=nvmesh
        debug "implied from $pkg: $proj $branch"
        echo "$proj $branch"
    fi
    return 0
}

while true
do
    case "$1" in
        -s|--spec)
            # Just test a spec, with optional default
            SPECCHECK=true
            echo "SPEC=$2"
            specParse "$2" "${3:-defrepo/defproject}"
            exit 0
            ;;
        --management|--nvmesh|--nvmeshum|--upgrader|--interop-db|--cloud|--infrastructure|--nvmesh-csi-driver)
            addpkg "${1#--}" "$2"
            shift 2
            ;;
        --debug)
            IS_DEBUG=true
            shift
            ;;
        -*)
            usage "Unmatched option: $1"
            ;;
        *)
            break
            ;;
    esac
done

while [ "${#QUEUE[@]}" -gt 0 ] && [ "${#IMPLICIT[@]}" -gt 0 ]; do
    current="${QUEUE[0]}"
    QUEUE=("${QUEUE[@]:1}")
    debug "PROCESSING $current"

    # Capture getimplied output and check return status
    getimplied "$current" ${EXPLICIT[$current]} > "$TMP/implied.txt"

    while read -r pkg spec; do
        if [ -n "${EXPLICIT[$pkg]}" ]; then continue; fi # Already found
        # For DS team - enable somerepo:- where branch is derived, but default repo is somerepo
        [[ "$spec" =~ : ]] || spec=$(echo ${IMPLICIT[$pkg]} | cut -f2 -d' '):$spec
        debug "IMPLY $current -> $pkg ($spec) was ${EXPLICIT[$pkg]}"
        EXPLICIT[$pkg]=$(specParse "$spec" "$GITGROUP/$pkg")
        QUEUE+=("$pkg")
        if [ -n "${IMPLICIT[$pkg]}" ]; then
            debug "Setting $pkg to $spec from $current.  Was ${IMPLICIT[$pkg]}"
            SOURCE[$pkg]="$current"
            unset 'IMPLICIT[$pkg]'
            if [ ${#IMPLICIT[@]} -eq 0 ]; then break; fi # We're done
        fi
    done < "$TMP/implied.txt"
done

for pkg in "${PKGLIST[@]}"
do
    if [ -n "${EXPLICIT[$pkg]}" ]; then
        echo "$pkg ${EXPLICIT[$pkg]} # ${SOURCE[$pkg]:-Unknown}"
    elif [ -n "${DEFAULTS[$pkg]}" ]; then
        echo "$pkg $(specParse "${DEFAULTS[$pkg]}" "$GITGROUP/$pkg") # DEFAULTS"
    else
        echo "$pkg SKIP"
    fi
done
