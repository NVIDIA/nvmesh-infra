# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from xlro.tools.nvmesh_diag.util import DiagModule, DiagManager, MsgLvl

class Software(DiagModule):
    description = "Installed Software Packages"

    def discover(self):
        platform = DiagManager.diag_instance('platform', self.node).info.platform
        if not platform:
            self.add_message("Skipping: Cannot get platform", MsgLvl.WARNING)
            return False
        self.add_message(self.run_cmd("dpkg -l" if platform == "ubuntu" else "rpm -qa")[0])
        self.add_message("Saved the software package information in the output file")
