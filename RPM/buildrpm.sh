#!/bin/bash -x

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

set -e

print_help() {
    cat <<EOF
usage: buildrpm.sh [options]

-h, --help                prints this help
-b, --branch BRANCH       branch name (default: git rev-parse --abbrev-ref HEAD)
-c, --commit-id COMMIT    commit id   (default: git log -n1 --format=%h)
-g, --change-id  CHANGE   gerrit change-id (default: parsed from HEAD message)
-d, --git-describe DESC   git describe string (default: git describe)
-S, --sign-rpm            sign the rpm with rpmmacros %_gpg_name
    --dist-tag TAG        distribution tag to include in the RPM name
    --ubuntu              also build a deb (alien + dpkg-buildpackage)
    --build-number N      build number suffix (default: buildnumber)
    --output DIR          copy resulting .rpm/.deb to DIR (default: cwd)
EOF
}

validate_buildrpm_dependencies_installed() {
    if [ -z "$BUILDRPM_DEPENDENCIES" ]; then
        echo "Error: BUILDRPM_DEPENDENCIES environment variable is not set or empty"
        return 1
    fi

    local missing_packages=()

    if [ -e /etc/redhat-release ]; then
        check_cmd="rpm -q"
    elif [[ "$(cat /etc/os-release 2>/dev/null)" =~ Ubuntu ]]; then
        check_cmd="dpkg -l"
    else
        echo "Error: Unable to determine OS type (neither RedHat nor Ubuntu detected)"
        return 1
    fi

    for package in $BUILDRPM_DEPENDENCIES; do
        if ! $check_cmd "$package" >/dev/null 2>&1; then
            missing_packages+=("$package")
        fi
    done

    if [ ${#missing_packages[@]} -gt 0 ]; then
        echo "Error: The following packages are not installed:"
        printf '%s\n' "${missing_packages[@]}"
        return 1
    fi

    return 0
}

buildRPMSetupTree() {
    if test -e /etc/redhat-release; then
        echo "Building RPM on Redhat-based distro. Running rpmdev-setuptree"
        rpmdev-setuptree
    else
        echo "Building RPM on non Redhat-based distro. Assuming Ubuntu. Manually setting up $rpm_build_dir"
        mkdir -p "$rpm_build_dir"/{SPECS,BUILD,BUILDROOT,SOURCES,RPMS/$ARCH,SRPMS}
    fi
}

pkgDeb() {
    if $isUbuntu || [[ "$DISTRIBUTION_INFO" =~ "Ubuntu" ]]; then
        echo "Building Ubuntu deb package..."
        ubuntu_dir="ubuntu_deb_build"
        rm -rf "$ubuntu_dir"
        mkdir "$ubuntu_dir"
        cd "$ubuntu_dir"

        if [ "$ARCH" = aarch64 ]; then
            ATARGET=--target=arm64
        else
            ATARGET=
        fi

        start_alien=$(date +%s)
        alien --generate -k --scripts ~/"rpmbuild/RPMS/$ARCH/$packagePrefix"* $ATARGET
        end_alien=$(date +%s)
        echo "Alien generation took $((end_alien - start_alien)) seconds."

        packDir=$packagePrefix-$rpm_version

        if [ -e "$packDir/debian" ]; then
            echo "Configuring deb dependencies..."
            sed -i -E "s/^[ ]*Depends:\s.*$/&, $requiresUbuntu/g" "$packDir/debian/control"

            echo "Setting gzip compression..."
            sed -i -E "s/dh_builddeb$/dh_builddeb -- -Zgzip/g" "$packDir/debian/rules"

            cd "$packDir"

            start_dpkg=$(date +%s)
            dpkg-buildpackage -uc -us
            end_dpkg=$(date +%s)
            echo "dpkg-buildpackage took $((end_dpkg - start_dpkg)) seconds."

            cd ..
            rm -rf "$packDir"
            cp *.deb ../
        fi

        cd ..
        rm -rf "$ubuntu_dir"
    fi
}

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RPM_DIR="$REPO_ROOT/RPM"

isUbuntu=false
buildNum="buildnumber"
outputDir="$(pwd)"

while [[ $# -gt 0 ]]; do
key="$1"
case $key in
    -h|--help)
        print_help; exit 0 ;;
    -b|--branch)
        branchName="$2"; shift ;;
    -c|--commit-id)
        commitID="$2"; shift ;;
    -g|--change-id)
        changeID="$2"; shift ;;
    -d|--git-describe)
        describe="$2"; shift ;;
    -S|--sign-rpm)
        signRPM=true ;;
    --dist-tag)
        distTag="$2"; shift ;;
    --ubuntu)
        isUbuntu=true ;;
    --build-number)
        buildNum="$2"; shift ;;
    --output)
        outputDir="$2"; shift ;;
    *)
        echo "Unknown option $key"; print_help; exit 1 ;;
esac
shift
done

. "$RPM_DIR/buildrpm_dependencies.env"
if ! validate_buildrpm_dependencies_installed; then
    echo "Package validation failed"
    exit 1
fi

if [ -z "$DISTRIBUTION_INFO" ]; then
    DISTRIBUTION_INFO=$(cat /etc/*release)
fi

cd "$REPO_ROOT"

if [ -z "$commitID" ]; then
    commitID=$(git log -n1 --format=%h 2>/dev/null || echo unknown)
fi

if [ -z "$changeID" ]; then
    changeID=$(git log -n1 --format=%b 2>/dev/null | awk '/^Change-Id: / {print $2}')
fi

[ -z "$changeID" ] && changeID="none"

if [ -z "$branchName" ]; then
    branchName=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)
fi

if [ -z "$distTag" ]; then
    if [ -e /etc/redhat-release ]; then
        redhat_rel_content=$(cat /etc/redhat-release)
        if [[ "$redhat_rel_content" =~ ([0-9]+).([0-9]+) ]]; then
            minor_ver=${BASH_REMATCH[2]}
            rpm_dist_tag=$(rpm --eval='%{?dist}')
            if [ -n "$minor_ver" ] && [ -n "$rpm_dist_tag" ]; then
                IFS='.' read -r -a rpm_dist_tag_arr <<<"$rpm_dist_tag"
                if [ ${#rpm_dist_tag_arr[@]} -eq 3 ]; then
                    distTag=".${rpm_dist_tag_arr[1]}"_"$minor_ver"
                else
                    distTag="$rpm_dist_tag"_"$minor_ver"
                fi
            fi
        fi
    else
        os_version_id=$(grep -oP '^VERSION_ID="*\K[^"]*' /etc/os-release)
        if [ -n "$os_version_id" ]; then
            short_version_id=$(echo "$os_version_id" | tr -d .)
            os_name=$(grep -oP '^NAME="*\K[^."]*' /etc/os-release)
            if [ -n "$os_name" ]; then
                lower_os_name=$(echo "$os_name" | awk '{print tolower($0)}')
                lower_os_name=${lower_os_name//[[:blank:]]/}
                distTag=".$lower_os_name$short_version_id"
            fi
        fi
    fi

    if [ -z "$distTag" ]; then
        echo "ERROR: could not find the distribution name and version to append to the RPM name"
    fi
fi

if [ -z "$describe" ]; then
    describe=$(git describe 2>/dev/null | cut -c 2-)
elif [[ $describe == v* ]]; then
    describe=$(echo "$describe" | cut -c 2-)
fi

IFS='-' read -ra gitDescribe <<<"$describe"
rpm_version="${gitDescribe[0]:-0.0.0}"
rpm_release_num="${gitDescribe[1]:-0}"
rpm_release="${rpm_release_num}$distTag.$buildNum"

echo "VERSION: $rpm_version RELEASE: $rpm_release"

rpm_build_dir=$(readlink -f ~/rpmbuild)
ARCH=$(uname -m)

buildRPMSetupTree

cp "$RPM_DIR/rpmmacros" "$HOME/.rpmmacros"

packagePrefix="nvmesh-utils"
cp "$RPM_DIR/nvmesh-utils.spec" ~/rpmbuild/SPECS/
rm -f ~/rpmbuild/RPMS/$ARCH/$packagePrefix*$ARCH.rpm

# Stage the source tree into ~/rpmbuild/SOURCES/nvmesh-utils. The spec then
# cp's it into BUILD/. We exclude .git, .venv, *.pyc, ubuntu_deb_build to keep
# the source archive deterministic and small.
commonRequirements="coreutils, grep"
requires="$commonRequirements, rpm"
requiresUbuntu="$commonRequirements, dpkg"
rsync -aRPq \
    --exclude='*.pyc' \
    --exclude='__pycache__' \
    --exclude='.git' \
    --exclude='.venv' \
    --exclude='ubuntu_deb_build' \
    --exclude='RPM/*.rpm' \
    --exclude='RPM/*.deb' \
    ./ ~/"rpmbuild/SOURCES/$packagePrefix/"

rm -fr ~/"rpmbuild/RPMS/$ARCH/$packagePrefix"*

echo "Starting rpmbuild for nvmesh-utils..."
start_rpmbuild=$(date +%s)
rpmbuild -ba ~/rpmbuild/SPECS/nvmesh-utils.spec \
    --define "commit_id $commitID" \
    --define "change_id $changeID" \
    --define "branch $branchName" \
    --define "version $rpm_version" \
    --define "release $rpm_release" \
    --define "requires $requires"
rpm_creation_retval=$?
end_rpmbuild=$(date +%s)
echo "rpmbuild took $((end_rpmbuild - start_rpmbuild)) seconds."

mkdir -p "$outputDir"
cp ~/"rpmbuild/RPMS/$ARCH/$packagePrefix"* "$outputDir/" 2>/dev/null || true
# stage rpm next to spec dir as well so existing mv_pkg() consumers keep finding it
cp ~/"rpmbuild/RPMS/$ARCH/$packagePrefix"* "$RPM_DIR/" 2>/dev/null || true

rm -rf ~/rpmbuild/SPECS/nvmesh-utils.spec ~/"rpmbuild/SOURCES/$packagePrefix" ~/"rpmbuild/BUILD/"* ~/"rpmbuild/BUILDROOT/"*

if [ "$rpm_creation_retval" -eq "0" ] && [ "$signRPM" = true ]; then
    new_rpm="$packagePrefix*$rpm_version-$rpm_release*.rpm"
    echo "Signing RPM..."
    (cd "$outputDir" && rpm --addsign $new_rpm) || echo "Failed to sign RPM!"
fi

cd "$RPM_DIR"
pkgDeb

if $isUbuntu || [[ "$DISTRIBUTION_INFO" =~ "Ubuntu" ]]; then
    cp "$RPM_DIR"/*.deb "$outputDir/" 2>/dev/null || true
fi

exit "$rpm_creation_retval"
