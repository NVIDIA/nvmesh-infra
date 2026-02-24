#!/usr/bin/env bash

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

set -e
source ${INFRABIN:=$(dirname $0)}/common.sh
GIT_SERVER=$GITURL
GET_SPECS=${SPECSBIN:-${INFRABIN}}/get-specs.sh

SPECS_ONLY=false

function usage() {
    [ -n "$1" ] && echo -e "ERROR: $*" >&2
    cat >&2 <<!

Usage: $0 [options] targetdir
    Gathers required sources into targetdir, copying or fetching from git as needed.

Options:
    -s|--spec <src-spec>                # Test src-spec parsing
    -m|--management|--mgmt <src-spec>   # See below for src-spec definition (--mgmt for backwargs compatibility)
    -n|--nvmesh <src-spec>              # See below for src-spec definition
    -u|--nvmeshum <src-spec>            # See below for src-spec definition
    --upgrader <src-spec>               # See below for src-spec definition
    -c|--cloud <src-spec>               # See below for src-spec definition
    --nvmesh-csi-driver <src-spec>      # See below for src-spec definition
    -i|--infra <src-spec>               # See below for src-spec definition (cannot be "-")
    -g|--gitcache <dir>                 # Reuse git under <dir> to save cloning (default is $XLRO_GITCACHE)
    -j|--just-src                       # Don't zip or extract info.  Just clone source
    -d|--dry-run                        # Do spec calculations, but don't actuall copy/fetch source
    -S|--git-server                     # git server to use (default is $GIT_SERVER)
    --git-log                           # detailed log of git commands
    --specs-only                        # Only generate specs (for debugging)

    <src-spec> is one of the following:
    * a source directory - e.g., -n ~/projects/nvmesh
    * a git reference as [<repo>]:<branch>[@<commit>] - e.g., -n joeweisblatt/nvmesh:master@ or -m ":#CI_1.3"
      (default commit is HEAD; default repo for management is $GITGROUP/management, for nvmesh is $GITGROUP/nvmesh)
    NOTE: when using "implied" spec of "-", there MUST be at least one explicit spec from which to imply it.
!
    exit 2
}

function fatal() {
    echo "ERROR in get-sources: $@" | tee -a ${FAIL:-/dev/null} >&2
    exit 1
}

declare -A SRCS
# Map repos to IDs so I don't have to worry about thread-safety of variable increment
# FD must be valid, but way out of range, file descriptor
LOCK_FD=80
declare -A LOCKID

export GIT_LOG_ON=false
while :
do
    PACKAGE_ARG=
    case "$1" in
        -m|--mgmt|--management) PACKAGE_ARG=management ;;
        -n|--nvmesh) PACKAGE_ARG=nvmesh ;;
        -u|--nvmeshum) PACKAGE_ARG=nvmeshum ;;
        --upgrader) PACKAGE_ARG=upgrader ;;
        --interop-db|--interop) PACKAGE_ARG=interop-db ;;
        -c|--cloud) PACKAGE_ARG=cloud ;;
        --csi|--nvmesh-csi-driver) PACKAGE_ARG=nvmesh-csi-driver ;;
        -i|--infra|--infrastructure) PACKAGE_ARG=infrastructure ;;
        -s|--spec)
            # Just test a spec, with optional default
            ${GET_SPECS} -s $2
            exit 0
            ;;
        -g|--gitcache)
            XLRO_GITCACHE=$2
            shift 2
            ;;
        -j|--just-src)
            JUST_SRC=true
            shift
            ;;
        -d|--dry-run)
            DRY_RUN=true
            shift
            ;;
        --specs-only)
            SPECS_ONLY=true
            DRY_RUN=true
            shift
            ;;
        -x)
            ECHO=echo
            shift
            ;;
        -S|--git-server)
            GIT_SERVER=$2
            shift 2
            ;;
        --git-log)
            GIT_LOG_ON=true
            shift
            ;;
        -*)
            usage "Unmatched option: $1"
            ;;
        *)
            break
            ;;
    esac
    if [ -n "$PACKAGE_ARG" ]; then
        spec_args+=" --$PACKAGE_ARG $2"
        shift 2
    fi
done

[ $# -eq 1 -o -n "$DRY_RUN" ] || usage "target-dir is required."
[ -n "$DRY_RUN" ] && XLRO_GITCACHE=
if [ -n "$XLRO_GITCACHE" ]
then
    mkdir -p $XLRO_GITCACHE
    export XLRO_GITCACHE="$(readlink -f $XLRO_GITCACHE)"

    function lockCache() {      # lockCache <pkgName> <repoId>
        FD=${LOCKID[${1:?}]}
        LOCKFILE=$XLRO_GITCACHE/${2:?}.lock
        mkdir -p $(dirname $LOCKFILE)
        touch $LOCKFILE 2>/dev/null || :            # Force existence of lock file
        # echo "git-cache: locking $LOCKFILE (#$FD)" >&2
        eval "exec $FD<$LOCKFILE"                   # Use < (read) to avoid permission issues
        flock_start=$(date +%s%N)                   # time flock delay for now
        flock -w 500 $FD || fatal "git-cache timeout $LOCKFILE" # Worst case full clone of UM can take a few mins
        # echo "git-cache waited $((($(date +%s%N)-flock_start) / 1000000))ms for $LOCKFILE" >&2
    }

    function releaseCache() {   # releaseCache <pkgName>
        FD=${LOCKID[${1:?}]}
        # echo "git-cache: releasing (#$FD)" >&2
        flock -u $FD || :
    }
fi

function fetchSources() {
    pkg=$1
    target=$(CDPATH= cd $2 && pwd)
    spec=(${@:3})
    files=
    fetch_start=$SECONDS
    [ $spec == "GIT" ] && GIT_BRANCH=${spec[2]} || GIT_BRANCH=
    if [ $spec == "GIT" -o $spec == "SRC" ]
    then
        if [ $spec == "SRC" ]
        then
            PSRC=${spec[1]}
            if [ -n "$JUST_SRC" ]
            then
                ( $ECHO cd ${spec[1]} && ${ECHO:-eval} "(git ls-files . 2>/dev/null || find . -type f) | cpio -pdm --quiet $target/$pkg" )
            fi
        elif [ -n "$XLRO_GITCACHE" ]
        then
            cd $XLRO_GITCACHE
            PDIR=${spec[1]/\//_} # Not sure why just replace 1 /.  But rather not change now
            # TODO: PDIR is wasteful of space.  Need to use multiple remotes in same cache dir.
            PSRC=$XLRO_GITCACHE/$PDIR
            lockCache $pkg $PDIR || return 1
            # Fetch tags so git describe (e.g. v3.3.2-299-g9900b4a) matches a direct checkout of the branch
            [ -d "$PDIR" ] && ( cd ./$PDIR && $ECHO git fetch -q --tags ) \
                || { rm -rf $PDIR; $ECHO git clone -c advice.detachedHead=false -q ${GIT_SERVER}${spec[1]}.git $PDIR; } \
                || return 1
            $ECHO cd $PSRC \
                && $ECHO git checkout -q "${spec[2]}" \
                && $ECHO git reset -q --hard "origin/${spec[2]}" \
                && $ECHO git reset -q --hard "${spec[3]:-HEAD}" \
                || return 1
        else
            PSRC=$target/$pkg
            mkdir -p $PSRC
            # echo "git clone -q -b "${spec[2]}" ${GIT_SERVER}${spec[1]}.git $PSRC" >&2
            $ECHO git clone -c advice.detachedHead=false -q -b "${spec[2]}" ${GIT_SERVER}${spec[1]}.git $PSRC || return 1
            (
                $ECHO cd $PSRC
                COMMIT="${spec[3]}"
                [ -z "$COMMIT" -o "${COMMIT^^}" == "HEAD" ] || $ECHO git reset --hard "$COMMIT"
            ) || return 1
        fi

        # When using a fork, fetch tags from the canonical repo so git describe works correctly
        [ "$pkg" == "infra" ] && CANONICAL_REPO="$GITGROUP/infrastructure" || CANONICAL_REPO="$GITGROUP/$pkg"
        if [ $spec == "GIT" ] && [ "${spec[1]}" != "$CANONICAL_REPO" ]; then
            ( cd "$PSRC" && $ECHO git fetch -q --tags "${GIT_SERVER}${CANONICAL_REPO}.git" ) 2>/dev/null || :
        fi

        # Ignore nvmesh.kernel submodule (could have used deinit, but prefer not to touch)
        [ "${pkg}" == "nvmeshum" ] \
            && SUBMODULES=$(grep -Po 'submodule "\K[^"]*' $PSRC/.gitmodules | grep -v nvmesh.kernel)
        if [ "${pkg}" == "nvmeshum" ] && [ $spec != "SRC" ]
        then
            git submodule --help | grep -q -- --jobs && JOBS_OPT="--jobs $(echo "$SUBMODULES" | wc -w)" || JOBS_OPT=""
            (cd $PSRC && git submodule sync --quiet --recursive $SUBMODULES \
                && git submodule update $JOBS_OPT --quiet --init --recursive $SUBMODULES) || echo "WARNING: Failed to init submodules of $pkg" >&2
            # TODO: remove this if will ever need to support SNAP/NVMX again
            #[ -d "${PSRC}/nvmx/subprojects" ] && { (cd $PSRC/nvmx && meson subprojects download 1> /dev/null && cd subprojects/core && meson subprojects download flexio 1> /dev/null) || fatal "Failed to meson download nvmx subprojects"; }
        fi
        if [ "${pkg}" == "infra" ]
        then
            (
                cd $PSRC >/dev/null
                echo CWD="$(pwd)"
                echo SPEC="$(git describe)"
                echo DESCRIBE="$(git describe)"
                echo BRANCH="${GIT_BRANCH:-$(git rev-parse --abbrev-ref HEAD 2> /dev/null)}"
            ) >$PSRC/version_info
        fi
        if [ -n "$JUST_SRC" -a "$PSRC" != "$target/$pkg" ]
        then
            cp -R $PSRC $target/$pkg
            PSRC=$target/$pkg
        fi

        # Show branch/commit details, if possible, for debugging. Stdout is used to return the file list, so send to stderr
        commit=$(cd $PSRC/.git 2>/dev/null && git rev-parse --short HEAD || echo non-git)
        echo "${pkg^^} - ${spec[@]}, commit: ${commit}" >&2
        # [ "$commit" != "non-git" ] && ( cd $PSRC && git log -1 --oneline --format='    %h: %s' ) >&2
        [ "$pkg" == nvmeshum ] && ( cd $PSRC && git submodule status $SUBMODULES) >&2

        if [ -z "$JUST_SRC" ]
        then
            (
                cd $PSRC
                [ -n "$ECHO" ] && { PSRC=$PSRC; pwd; git ls-files . | head; }
                ${ECHO:-eval} "git ls-files . | tar -czf $target/$pkg.tgz -T -"
                # reproducible TGZ (no timestamp/user diffs) for testing
                # ${ECHO:-eval} "git ls-files . | tar -T - -c --mtime=@0 --owner=0 --group=0 --numeric-owner --sort=name | gzip -n >$target/$pkg.tgz"
                COMMIT_ID="$(git log -n1 --format=%h)"
                CHANGE_ID="$(git log -n1 --format=%b | awk '/^Change-Id: / {print $2}')"
                DESCRIBE="$(git describe --long)"
                BRANCH="${GIT_BRANCH:-$(git rev-parse --abbrev-ref HEAD 2> /dev/null)}"
                FULL_ID="$(git rev-parse HEAD)"
                echo -c "'${COMMIT_ID:-unknown}' -g '${CHANGE_ID:-unknown}' -d '${DESCRIBE:-unknown}' -b '${BRANCH:-unknown}' -f '${FULL_ID:-unknown}'" > $target/${pkg}.gitinfo
                if [ "${pkg}" == "nvmeshum" ]; then
                    git submodule status | grep -v spdk.public | awk 'NF-=1' > $target/nvmeshum_submodules.txt
                    RPM/gen_info.sh $target/nvmeshum_info > /dev/null || :
                fi
            )
            rm -rf $target/$pkg
        fi
    fi
    [ -z "$XLRO_GITCACHE" -o "$spec" != "GIT" ] || releaseCache $pkg
    # echo "fetched $pkg in $((SECONDS-fetch_start))s" >&2
}

if [ -n "$DRY_RUN" ]
then
    TARGET="[dry-run]"
    GITLOG=./git-logs
    FAIL=/dev/null
else
    TARGET=$(readlink -f $1)
    mkdir -p $TARGET
    FAIL=$TARGET/get-source.failure
    >$FAIL
    GITLOG=$TARGET/git-logs
fi
if $GIT_LOG_ON
then
    GIT=$(type -p git)
    function git() {
        set -o | grep -q '^pipefail.*off' && SETPF=true || SETPF=false
        id=$RANDOM
        echo "#[$id-${pkg:-no-pkg}] git" "$@" >>$GITLOG
        start=$SECONDS
        $SETPF && set -o pipefail
        [ $1 != "archive" ] && ( $GIT "$@" |& tee -a $GITLOG ) || $GIT "$@"
        CODE=$?
        echo "#[$id-${pkg:-no-pkg}] exit: $CODE, time: $((SECONDS-start)) s" >>$GITLOG
        $SETPF && set +o pipefail
        return $CODE
    }
fi

if $SPECS_ONLY
then
    ${GET_SPECS} --debug $spec_args
    exit $?
fi

# Use get-specs.sh to expand/imply sources
TMP_SPECS=$(mktemp get-specs-XXXXX)
trap 'rm -f "$TMP_SPECS"' EXIT

if ! ${GET_SPECS} $spec_args > "$TMP_SPECS"; then
    echo "ERROR: get-specs.sh failed" >&2
    exit 1
fi

while read pkg spec; do
    spec=${spec%% #*}
    if [ "$spec" == "SKIP" ]; then
        echo "Skipping implied $pkg!" >&2
    else
        [ "$pkg" == "infrastructure" ] && pkg="infra" # preserving old behavior
        SRCS[$pkg]="$spec"
        LOCKID[$pkg]=$((LOCK_FD++))
    fi
done < "$TMP_SPECS"

# Some perl called by git stuff is annoyed by this :-(
unset LC_IDENTIFICATION

FILES=
for pkg in "${!SRCS[@]}"
do
    SPEC=(${SRCS[$pkg]})
    # echo "PKG: $pkg SPEC: ${SPEC[@]}" >&2

    if [ $SPEC == 'PKGS' ]
    then
        ls ${SPEC[@]:1} >/dev/null
        FILES+=" ${SPEC[@]:1}"
    else
        FILES+=" $TARGET/$pkg.gitinfo $TARGET/$pkg.tgz"
        if [ -n "$DRY_RUN" ]
        then
            echo "fetchSources $pkg $TARGET ${SPEC[@]}"
        else
            fetchSources $pkg $TARGET ${SPEC[@]} || echo "Failed to get sources for $pkg : ${SPEC[@]}" | tee -a $FAIL >&2 &
        fi
    fi
done
wait
[ -s $FAIL ] && exit 1
[ -n "$DRY_RUN" ] && exit 0

[[ -v SRCS[nvmeshum] ]] && [[ ! "${SRCS[nvmeshum]}" =~ ^PKGS ]] && FILES+=" $(ls $TARGET/nvmeshum_submodules.txt $TARGET/nvmeshum_info 2>/dev/null || :)"
[ -n "$JUST_SRC$DRY_RUN" ] && { [ ! -s $FAIL ] && exit 0 || exit 1; }

set -o pipefail

FILES=$(ls -1 $FILES 2>/dev/null | sort -u | paste -sd' ' -)
echo $FILES
exit 0
