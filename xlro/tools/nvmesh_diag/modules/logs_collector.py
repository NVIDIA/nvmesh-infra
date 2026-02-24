# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from xlro.tools.nvmesh_diag.util import DiagModule

class LogsCollector(DiagModule):
    description = "NVMesh Logs Collector"

    def diagnose(self):
        self.run_cmd("sudo nvmesh_logs_collector", "Collecting logs. Please wait ...", is_sudo=True)
