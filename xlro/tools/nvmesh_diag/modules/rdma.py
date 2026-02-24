# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from xlro.tools.nvmesh_diag.util import DiagModule, MsgLvl, DiagArgs

class RDMA(DiagModule):
    description = "RDMA"

    def validate(self):
        if not self.get_info('host').has_root:
            self.skip('No root execution access.')
        for port in [p for n in self.node.nics for p in n.ports if p.name.startswith('mlx')]:
            if int(port.name[3]) != 5:
                self.add_message(f"Unsupported mlx nic: {port.name} with version {port.name[4]}. Skipping check.", MsgLvl.WARNING)
        if DiagArgs.verbose:
            self.run_cmd("sudo ibdev2netdev -v", print_out=True)
