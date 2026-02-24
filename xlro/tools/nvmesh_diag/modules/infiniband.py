# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from xlro.tools.nvmesh_diag.util import DiagModule, MsgLvl


class Infiniband(DiagModule):
    description = "Infiniband"

    def validate(self):
        if not self.node.ofed_support or all(p.protocol.lower() != "infiniband" for n in self.node.nics for p in n.ports):
            self.add_message("IB is not configured on this node", MsgLvl.WARNING)
        else:
            self.run_cmd('sudo ibhosts', 'IB hosts:', print_out=True)
            self.run_cmd('sudo ibswitches', 'IB switches:', print_out=True)
