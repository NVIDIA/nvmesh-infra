# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from xlro.tools.nvmesh_diag.util import DiagModule, DiagManager, StopDiag, MsgLvl

class Mellanox(DiagModule):
    description = "Mellanox Driver"

    def check_for_inbox_driver_packages(self):
        # TODO: these are for compilation, check with Omri/Jared which are needed to run inbox
        UB_DEPS = ["rdma-core", "librdmacm-dev", "ibacm", "infiniband-diags", "libopensm-dev", "libibverbs-dev",
                   "libudev-dev", "libibumad-dev"]
        EL_DEPS = ["autoconf", "gcc", "dapl", "ibacm", "ibutils", "ibutils-devel", "infiniband-diags", "iwpmd",
                   "json-c-devel", "libaio-devel", "libcurl-devel", "libibcm-devel", "libibmad-devel", "libibumad",
                   "libibverbs", "libibverbs1", "libibverbs-devel", "libibverbs-utils", "libmlx4", "libmlx5", "librdmacm",
                   "librdmacm-devel", "librdmacm-utils", "libverbs-devel", "libudev-devel", "mstflint",
                   "opa-address-resolution", "opa-fastfabric", "opa-libopamgt", "opensm", "opensm-devel", "perftest",
                   "qperf", "rdma-core", "rdma-core-devel", "srp_daemon"]

        platform = DiagManager.diag_instance('platform', self.node).info.platform
        if not platform:
            self.add_message("Skipping: Cannot get platform", MsgLvl.WARNING)
            raise StopDiag()

        if platform == "ubuntu":
            check_cmd = "dpkg -l"
            check_packages = UB_DEPS
        else:
            check_cmd = "rpm -q"
            check_packages = EL_DEPS

        return [pkg for pkg in check_packages if self.run_cmd(f'{check_cmd} {pkg}', print_err=False)[2]]

    def diagnose(self):
        ofed_version = self.node.ofed_version
        if not ofed_version:
            self.add_message("OFED not installed! Checking for inbox drivers now.")
            missing_inbox_drivers = self.check_for_inbox_driver_packages()
            if len(missing_inbox_drivers) != 0:
                self.add_message(f"OFED is not installed and the following Inbox driver packages are missing: {missing_inbox_drivers} ", MsgLvl.ERROR)

                def install_missing_inbox_pkgs():
                    for package in missing_inbox_drivers:
                        self.run_cmd(f"sudo {self.get_package_manager()} install -y {package}", f"Installing package {package}")
                self.suggest_fix("Do you want to install the Mellanox inbox drivers now?", True, install_missing_inbox_pkgs)
        else:
            # TODO: add comparison with support matrix
            self.add_message(f"OFED {ofed_version} installed")
