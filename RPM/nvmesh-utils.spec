# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

Name:                           nvmesh-utils
Version:                        %{version}
Release:                        %{release}
Group:                          System Environment/Kernel
Summary:                        "nvmesh-utils" by NVIDIA

License:                        Apache-2.0
URL:                            http://www.nvidia.com
Source0:                        %{name}
Requires:                       %{requires}
AutoReqProv:                    no

%global __python %{__python2.7}

# RHEL 8+ rpm tooling fails the build on `#!/usr/bin/env python` shebangs.
# Our pyinstaller binary is the entrypoint; the source-tree shebangs are
# inert. Skip the check to keep parity with Ubuntu rpm (which doesn't run it).
%undefine __brp_mangle_shebangs

%define _build_id_links none

%description

Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.

"NVIDIA nvmesh-cli" components.
        Branch: %{branch}
        Commit: %{commit_id}

%prep
cp -rf %{_sourcedir}/%{name} %{_builddir}/

INFRA_PATH="%{_builddir}/%{name}/dist/infra/infra"
if [ ! -f "$INFRA_PATH" ]; then
        mkdir -p $(dirname $INFRA_PATH) && printf "#!/usr/bin/env bash \necho infra not installed" > $INFRA_PATH
        chmod 755 $INFRA_PATH
fi

%build
# Turn off default stripping
%define __strip /bin/true

%install
mkdir -pv %{buildroot}/opt/nvmesh/cli
mkdir -pv %{buildroot}/usr/bin

cp -rf %{_builddir}/%{name}/* %{buildroot}/opt/nvmesh/cli/
cp -rf %{_builddir}/%{name}/dist/infra %{buildroot}/opt/nvmesh/infra
ln -s /opt/nvmesh/infra/infra %{buildroot}/usr/bin/nvmesh
ln -s /opt/nvmesh/infra/infra %{buildroot}/usr/bin/nvmesh_check
cp -rf %{_builddir}/%{name}/xlro/tools/nvmesh_logs_collector %{buildroot}/usr/bin

echo "version=\"%{version}-%{release}\"" > %{buildroot}/opt/nvmesh/cli/version
echo "commit=\"%{commit_id}\"" >> %{buildroot}/opt/nvmesh/cli/version
echo "branch=\"%{branch}\"" >> %{buildroot}/opt/nvmesh/cli/version

# Strip everything BUT gnureadline manually
find %{buildroot} -type f -name "*.so" -not -name "gnureadline*.so" -exec strip {} \;

%post

%postun
MDIR="/opt/nvmesh"
if [ -d "$MDIR" ] && [ -z "$(ls -A $MDIR)" ]; then
        rm -rf $MDIR
else
        exit 0
fi

%files
/opt/nvmesh
/opt/nvmesh/cli
/opt/nvmesh/infra
/usr/bin/nvmesh_logs_collector
%attr(0755, -, -)/opt/nvmesh/infra/infra
%attr(0755, -, -)/usr/bin/nvmesh_logs_collector
/usr/bin/nvmesh
/usr/bin/nvmesh_check

%changelog
* Wed Oct 7 2015 Nvidia
- Installing Nvidia nvmesh-utils
