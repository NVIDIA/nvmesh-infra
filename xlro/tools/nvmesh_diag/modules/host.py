# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from xlro.tools.nvmesh_diag.util import DiagModule, MsgLvl
from xlro.core.util.general_utils import host_name, host_aliases
from xlro.core.util.ssh import Connection

class HostDiag(DiagModule):
    description = "Host Check"

    def discover(self):
        self.diag_info.has_shell = None
        self.diag_info.has_root = None
        hostname = host_name(self.node.name)
        self.diag_info.hostname = hostname
        self.diag_info.aliases = host_aliases(hostname)
        try:
            conn = Connection(hostname, fail_is_error=False)
            out, err, code = conn.execute('sudo echo', timeout=15, desc='try-ssh')
            if code:
                self.add_message(f'SSH OK, but no root access', MsgLvl.WARNING)
            self.diag_info.has_shell = True
            self.diag_info.has_root = code == 0
        except Exception as e:
            self.add_message(f'SSH to {self.node.name} ({hostname}) failed', MsgLvl.WARNING)
            self.diag_info.has_shell = False
            self.diag_info.has_root = False
