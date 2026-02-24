# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from xlro.tools.nvmesh_diag.util import DiagModule, MsgLvl

class System(DiagModule):
    description = "System"

    def validate(self):
        actual_freq = None
        max_freq = None

        if "CPU MHz" in self.node.lscpu_info:
            actual_freq = float(self.node.lscpu_info["CPU MHz"])
            for freq in self.node.dmi_info["processor-frequency"].split('MHz'):
                freq = freq.strip()
                if freq.isdigit():
                    max_freq = float(freq)

        if actual_freq and max_freq:
            if max_freq - actual_freq >= float(100):
                self.add_message(
                    "Actual running CPU frequency is lower than the maximum CPU frequency. This might impact performance! "
                    "Check BIOS settings and verify System Tuning settings as below.", MsgLvl.WARNING)
            else:
                self.add_message("CPU frequency settings OK.", MsgLvl.SUCCESS)

    def details(self):
        mem, _, _ = self.run_cmd("free -h | grep Mem | awk '{print $2}'")
        self.add_message(f'Installed Memory: {mem.strip()}')
        for k, v in self.node.lscpu_info.items():
            self.add_message(f'{k}: {v}')
        for k, v in self.node.dmi_info.items():
            self.add_message(f'{k}: {v}')

        pass
