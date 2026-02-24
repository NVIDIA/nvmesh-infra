# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from xlro.tools.nvmesh_diag.util import DiagModule

class IP(DiagModule):
    description = "IP Interface and Address"

    def diagnose(self):
        if not self.get_info('host').has_exec:
            self.skip('No remote access.')
        self.run_cmd("ip -4 a s", print_out=True)
        self.run_cmd("ip -s link", print_out=True)
