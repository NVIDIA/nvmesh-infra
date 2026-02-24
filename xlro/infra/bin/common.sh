# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

# Common stuff for the infra scripts
# TODO: Merge InfraShellHelper.sh with this file
GITURL=ssh://git@gitlab-master.nvidia.com:12051/
GITAPI=https://gitlab-master.nvidia.com/api/v4/
GITGROUP=excelero
INFRATMP=/tmp/infraClient
# Persistency for Infra
INFRALIB=/usr/local/lib/infra

CACHE_VER=v2 # This allows us to ignore old caches if we find a bug or change something
PKGCACHE=${PKGCACHE:-/var/cache/excelero}
MAXCACHE=10 # id's per type (not # of packages)

log() {
    echo -e "$(hostname -s) $(date +%T) PKGCACHE: $*"
}

error() {
    log "ERROR: $*" >&2
}

checkPkgCache() { # Usage: type cache-id [filename ...]
    # Cache ID must be based on repo commit and other unique compilation flags
    [ -n "$2" ] || { error "$1: missing cache-id" >&2; return 1; }
    log "Checking KEY=$1/$2, NO-CACHE=$NO_PKG_CACHE"
    [ -z "$NO_PKG_CACHE" ] || return 1
    CACHE=$PKGCACHE/$1/$2-$CACHE_VER
    shift 2
    [ -d $CACHE ] || { error "No dir: $CACHE"; return 1; }
    # Directory must exist and contain at least one package file
    if [ ! -e $CACHE/*.$PKG_TYPE ]; then
        error "Empty dir: $CACHE (removing so it can be repopulated)"
        sudo rm -vrf "$CACHE"
        return 1
    fi
    log "Using $CACHE"
    sudo touch $CACHE # for LRU
    ln -fsv $CACHE/* .
    rm -f *src.$PKG_TYPE # We inadvertently cached the *.src.rpm files, which can't be installed
    return 0
}

updatePkgCache() { # Usage: type cache-id [filename ...]
    # Cache ID must be based on repo commit and other unique compilation flags
    [ -n "$2" ] || { error "$1: missing cache-id" >&2; return 1; }
    # Always write to cache - NO_PKG_CACHE only controls reading, not writing
    # This allows parallel build+install flow: build with --no-pkg-cache still caches for install phase
    CACHE=$PKGCACHE/$1/$2-$CACHE_VER
    shift 2
    log "Updating $CACHE"
    NAMES=""
    for name in "${@:-*.$PKG_TYPE}"; do NAMES+="${NAMES:+ -o }-name '$name'"; done
    FOUND=$(eval find . $NAMES 2>/dev/null)
    if [ -z "$FOUND" ]; then
        error "No files found for $CACHE (cwd=$(pwd), pattern=$NAMES); not overwriting cache" >&2
        return 1
    fi
    # Make update atomic by creating tempdir and moving after
    sudo mkdir -p $CACHE.$$
    echo "$FOUND" | sudo xargs -I {} cp -v {} $CACHE.$$
    sudo rm -f $CACHE.$$/*src.$PKG_TYPE
    sudo rm -vrf "$CACHE"
    sudo mv $CACHE.$$ $CACHE
    # LRU - Clear all but N latest cache-ids
    log "Clearing old cache-ids: $(ls -dt $(dirname $CACHE)/*/ | tail -n +$(($MAXCACHE+1))) | paste -sd, -"
    ls -dt $(dirname $CACHE)/*/ | tail -n +$(($MAXCACHE+1)) | sudo xargs -rt rm -vrf --
    ( cd $CACHE && log "Cached: $(ls -m)" )
    return 0
}

handleNduInfo() { # Args: pkg-name
    # Relies on GIT_DESCRIBE and NDU_DESCRIBE and NDU_INFO being set in arg parsing.
    # Sets NDU_COUNT and NDU_RE
    local PKG_NAME=$1
    if [ -n "$NDU_DESCRIBE" ]; then
        [ -z "$NDU_INFO" ] && echo "NDU_DESCRIBE: '$NDU_DESCRIBE' NDU_REL='$NDU_REL' NDU_COUNT='$NDU_COUNT'"
        # NDU_DESCRIBE can override just commitcount or release-commitcount describe (i.e., 3000 or v3.3.0-3000)
        [[ "$NDU_DESCRIBE" == *-* ]] && NDU_REL=${NDU_DESCRIBE%%-*} || NDU_REL=${GIT_DESCRIBE%%-*}
        NDU_COUNT=${NDU_DESCRIBE#*-}
        GIT_DESCRIBE=v${NDU_REL#v}-$NDU_COUNT-${GIT_DESCRIBE##*-}
        [ -z "$NDU_INFO" ] && echo "Override describe: $GIT_DESCRIBE"
    fi

    if [ -n "$NDU_INFO" ]; then
        if [ -n "$RPM" ]; then
            V=$(echo $RPM | grep -Po "$PKG_NAME.v?\\K[0-9.]*")
        else
            D="${GIT_DESCRIBE:-$(git describe 2>/dev/null)}"
            V=${D#v}
        fi
        echo ${V%%-*}
        exit 0
    fi
}

# ============= METRICS COLLECTION =============
declare -A METRICS_TIMERS
declare -A METRICS_DATA
METRICS_VERSION="1.0"

metrics_init() {
    METRICS_START_EPOCH=$(date +%s)
    metrics_set "metrics_version" "$METRICS_VERSION"
    metrics_set "start_time" "$(date -Iseconds)"
}

metrics_set() {
    METRICS_DATA["$1"]="$2"
    echo "METRIC: $1=$2"
}

metrics_phase_start() {
    METRICS_TIMERS["$1_start"]=$SECONDS
}

metrics_phase_end() {
    local phase=$1
    local start=${METRICS_TIMERS["${phase}_start"]:-$SECONDS}
    local duration=$((SECONDS - start))
    local status=${2:-success}
    METRICS_TIMERS["${phase}_duration"]=$duration
    METRICS_TIMERS["${phase}_status"]=$status
    echo "METRIC: PHASE=$phase duration=${duration}s status=$status"
}

metrics_collect_system_info() {
    # Collect info from machine (where script runs)
    metrics_set "install_machine_hostname" "$(hostname -s)"
    metrics_set "install_machine_fqdn" "$(hostname -f 2>/dev/null || hostname)"
}

metrics_collect_versions() {
    local logd=${1:-.}
    # Parse gitinfo files - format: -c 'commit' -g 'change' -d 'describe' -b 'branch' -f 'full'
    for component in management nvmesh upgrader interop-db; do
        local gitinfo="$logd/${component}.gitinfo"
        if [ -f "$gitinfo" ]; then
            # Extract -d 'describe' value (git describe version)
            local ver=$(sed -n "s/.*-d '\([^']*\)'.*/\1/p" "$gitinfo" 2>/dev/null)
            # Extract -b 'branch' value
            local branch=$(sed -n "s/.*-b '\([^']*\)'.*/\1/p" "$gitinfo" 2>/dev/null)
            [ -n "$ver" ] && metrics_set "${component//-/_}_version" "$ver"
            [ -n "$branch" ] && metrics_set "${component//-/_}_branch" "$branch"
        fi
    done
    # Infra version
    local infra_ver=$(cd ${INFRABIN:-/tmp} 2>/dev/null && git describe 2>/dev/null || echo "unknown")
    metrics_set "infra_version" "$infra_ver"
}

# ============= NODE-LEVEL METRICS (for sub-scripts) =============
# These run on each node and output METRIC lines to logs

node_metrics_init() {
    NODE_METRICS_START=$SECONDS
    local hostname=$(hostname -s)
    echo "NODE_METRIC: node=$hostname"
    echo "NODE_METRIC: script=$(basename $0)"
    echo "NODE_METRIC: start_time=$(date -Iseconds)"
    # Collect system info
    source /etc/os-release 2>/dev/null || true
    echo "NODE_METRIC: os_name=${ID:-unknown}"
    echo "NODE_METRIC: os_version=${VERSION_ID:-unknown}"
    echo "NODE_METRIC: kernel=$(uname -r)"
    echo "NODE_METRIC: cpu_count=$(nproc 2>/dev/null || echo 1)"
    local mem_kb=$(awk '/MemTotal/{print $2}' /proc/meminfo 2>/dev/null || echo 0)
    echo "NODE_METRIC: memory_gb=$((mem_kb / 1024 / 1024))"
    echo "NODE_METRIC: pkg_type=${PKG_TYPE:-$([ -f /etc/debian_version ] && echo deb || echo rpm)}"
}

# use only when timed step is not possible
node_step_start() {
    eval "NODE_STEP_${1}_START=$SECONDS"
}

# use only when timed step is not possible
node_step_end() {
    local step=$1
    local status=${2:-success}
    local start_var="NODE_STEP_${step}_START"
    local start=${!start_var:-$SECONDS}
    local duration=$((SECONDS - start))
    echo "NODE_METRIC: STEP=$step duration=${duration}s status=$status"
}

# timed_step: Execute a function and automatically capture timing
# Usage: timed_step STEP_NAME function_name [args...]
# Example: timed_step BUILD buildManagementRPM
# Use this most of the time instead of node_step_start/end
timed_step() {
    local step_name=$1
    shift
    node_step_start "$step_name"

    # Execute the function
    local rc=0
    "$@" || rc=$?

    local status="success"
    [ $rc -ne 0 ] && status="failed"
    node_step_end "$step_name" "$status"
    return $rc
}

node_metrics_cache() {  # cache_type hit|miss
    echo "NODE_METRIC: cache_$1=$2"
}

#========= END NODE-LEVEL METRICS =============
