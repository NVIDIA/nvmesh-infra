# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from xlro.tools.nvmesh_diag.util import DiagModule, MsgLvl

class NVMe(DiagModule):
    description = "NVMe Storage Device"

    def diagnose(self):
        if self.node.execute("which nvme")[2] == 0:
            # self.add_message("NVMe SSD device information")
            nvme_list_output = self.run_cmd("nvme list")[0].splitlines()
            nvme_numa_output = self.run_cmd("lspci -vv | grep -A 10 Volatile | grep -e Volatile -e NUMA")
            if len(nvme_list_output) > 2:
                for line in nvme_list_output:
                    self.add_message(line)
                self.add_message(nvme_numa_output)
            else:
                self.add_message(
                    "No NVMe SSD found on this server! This server can only be configured as a NVMesh Client.", MsgLvl.WARNING)
                return
        else:
            self.add_message("The nvme-cli tool seems to be missing!", MsgLvl.WARNING)

            self.suggest_fix("Do you want to install the nvme-cli?", False,
                        lambda: self.add_message(f"sudo {self.get_package_manager()} install nvme-cli -y"))
